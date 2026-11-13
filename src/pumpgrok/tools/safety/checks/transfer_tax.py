"""Required transfer-tax check from the mint account (amendment 2026-11-13).

Paper only. The check reads the mint with ``getAccountInfo`` (``jsonParsed``).
A classic SPL Token mint has no transfer fee. A Token-2022 mint without a
``transferFeeConfig`` extension has no transfer fee. Either case passes with
value 0.

When ``transferFeeConfig`` is present, the effective rate is the newer
schedule if ``newer.epoch <= current epoch``, otherwise the older schedule.
That is the rule in spl-token-2022
``TransferFeeConfig::get_epoch_fee``:

https://github.com/solana-program/token-2022/blob/v8.0.0/program/src/extension/transfer_fee/mod.rs

Parsed field names (``olderTransferFee``, ``newerTransferFee``,
``transferFeeBasisPoints``, ``maximumFee``, ``epoch``) follow the Solana
``jsonParsed`` token-extension decoder. The current epoch comes from
``getEpochInfo``.

``transfer_tax_warn_pct`` (5) and ``transfer_tax_hard_fail_pct`` (10) are
percent, so 100 basis points is 1 percent. An unreadable mint, an
undecodable extension, or an unreadable epoch is BLIND. A required BLIND
forces KILL. This module never signs and never sends a transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from pumpgrok.config.settings import SafetyThresholds
from pumpgrok.tools._common import SOLANA_RPC_SOURCE
from pumpgrok.tools.onchain.rpc import RpcClient, RpcError
from pumpgrok.tools.safety.checks import CheckResult

SPL_TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"  # noqa: S105
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"  # noqa: S105
_FEE_EXTENSION = "transferFeeConfig"
_MAX_FEE_BPS = 10_000
_WEIGHT = Decimal("0.10")
_BPS_PER_PERCENT = Decimal(100)


class _Undecodable(Exception):
    """The mint was readable, but a transfer-fee extension could not be decoded."""


@dataclass(frozen=True, slots=True)
class _Fee:
    epoch: int
    maximum_fee: int
    bps: int


@dataclass(frozen=True, slots=True)
class _MintView:
    kind: Literal["spl-token", "token-2022", "unknown"]
    extensions: list[Any]
    slot: int | None


@dataclass(frozen=True, slots=True)
class TransferTaxOutcome:
    """The check row plus the RPC evidence the report should record."""

    result: CheckResult
    source: str
    slot: int | None


def _row(
    status: Literal["pass", "warn", "fail", "blind"],
    *,
    value: float | None,
    reason: str,
    threshold: str | None,
) -> CheckResult:
    return CheckResult(
        id="transfer_tax",
        status=status,
        value=value,
        threshold=threshold,
        weight=_WEIGHT,
        reason=reason,
    )


def _blind(reason: str) -> CheckResult:
    return _row("blind", value=None, reason=reason, threshold=None)


def _outcome(result: CheckResult, slot: int | None) -> TransferTaxOutcome:
    return TransferTaxOutcome(result=result, source=SOLANA_RPC_SOURCE, slot=slot)


def _threshold_text(thresholds: SafetyThresholds) -> str:
    return (
        f"warn>={thresholds.transfer_tax_warn_pct:g} "
        f"fail>={thresholds.transfer_tax_hard_fail_pct:g}"
    )


def _parse_u64(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return None


def _parse_fee(raw: Any) -> _Fee:
    if not isinstance(raw, dict):
        raise _Undecodable
    epoch = _parse_u64(raw.get("epoch"))
    maximum = _parse_u64(raw.get("maximumFee"))
    bps = _parse_u64(raw.get("transferFeeBasisPoints"))
    if epoch is None or maximum is None or bps is None or bps > _MAX_FEE_BPS:
        raise _Undecodable
    return _Fee(epoch=epoch, maximum_fee=maximum, bps=bps)


def _slot_of(result: dict[str, Any]) -> int | None:
    context = result.get("context")
    if not isinstance(context, dict):
        return None
    slot = context.get("slot")
    if isinstance(slot, bool) or not isinstance(slot, int):
        return None
    return slot


def _kind(owner: str | None, program: str | None) -> Literal["spl-token", "token-2022", "unknown"]:
    token22 = owner == TOKEN_2022_PROGRAM or program == "spl-token-2022"
    classic = owner == SPL_TOKEN_PROGRAM or program == "spl-token"
    if token22 and not classic:
        return "token-2022"
    if classic and not token22:
        return "spl-token"
    if token22 and classic:
        raise _Undecodable
    return "unknown"


def _view(result: Any) -> _MintView:
    """Parse a ``getAccountInfo`` result into a mint view, or raise ``_Undecodable``."""
    if not isinstance(result, dict) or result.get("value") is None:
        raise _Undecodable
    value = result["value"]
    if not isinstance(value, dict):
        raise _Undecodable
    data = value.get("data")
    if not isinstance(data, dict):
        raise _Undecodable
    parsed = data.get("parsed")
    if not isinstance(parsed, dict):
        raise _Undecodable
    if parsed.get("type") not in (None, "mint"):
        raise _Undecodable
    info = parsed.get("info")
    if not isinstance(info, dict) or info.get("decimals") is None:
        raise _Undecodable
    extensions = info.get("extensions")
    if extensions is None:
        extensions = []
    if not isinstance(extensions, list):
        raise _Undecodable
    owner = value.get("owner")
    program = data.get("program")
    owner_text = owner if isinstance(owner, str) else None
    program_text = program if isinstance(program, str) else None
    return _MintView(
        kind=_kind(owner_text, program_text),
        extensions=extensions,
        slot=_slot_of(result),
    )


def _fee_state(extensions: list[Any]) -> dict[str, Any] | None:
    """The ``transferFeeConfig`` state, or ``None`` when that extension is absent."""
    found: dict[str, Any] | None = None
    for item in extensions:
        if not isinstance(item, dict):
            raise _Undecodable
        if item.get("extension") != _FEE_EXTENSION:
            continue
        state = item.get("state")
        if not isinstance(state, dict):
            raise _Undecodable
        found = state
    return found


def _zero_reason(kind: Literal["spl-token", "token-2022", "unknown"]) -> str:
    if kind == "spl-token":
        return "classic SPL mint has no transfer fee; effective bps 0"
    if kind == "token-2022":
        return "token-2022 mint has no TransferFeeConfig; effective bps 0"
    return "readable mint has no TransferFeeConfig; effective bps 0"


def _status_for(pct: Decimal, thresholds: SafetyThresholds) -> Literal["pass", "warn", "fail"]:
    if pct >= Decimal(str(thresholds.transfer_tax_hard_fail_pct)):
        return "fail"
    if pct >= Decimal(str(thresholds.transfer_tax_warn_pct)):
        return "warn"
    return "pass"


def _fee_reason(older: _Fee, newer: _Fee, active: _Fee, epoch: int) -> str:
    which = "newer" if active is newer else "older"
    pct = Decimal(active.bps) / _BPS_PER_PERCENT
    return (
        f"effective_bps={active.bps} pct={format(pct, 'f')} epoch={epoch} active={which} "
        f"older_bps={older.bps} older_maximum_fee={older.maximum_fee} older_epoch={older.epoch} "
        f"newer_bps={newer.bps} newer_maximum_fee={newer.maximum_fee} newer_epoch={newer.epoch}"
    )


def _active_fee(older: _Fee, newer: _Fee, epoch: int) -> _Fee:
    """Newer schedule once its epoch has arrived; otherwise the older schedule."""
    if newer.epoch <= epoch:
        return newer
    return older


async def _current_epoch(rpc: RpcClient) -> int:
    try:
        info = await rpc.call("getEpochInfo", [])
    except RpcError as exc:
        raise _Undecodable from exc
    if not isinstance(info, dict):
        raise _Undecodable
    epoch = _parse_u64(info.get("epoch"))
    if epoch is None:
        raise _Undecodable
    return epoch


def _scored(older: _Fee, newer: _Fee, epoch: int, thresholds: SafetyThresholds) -> CheckResult:
    active = _active_fee(older, newer, epoch)
    pct = Decimal(active.bps) / _BPS_PER_PERCENT
    return _row(
        _status_for(pct, thresholds),
        value=float(pct),
        reason=_fee_reason(older, newer, active, epoch),
        threshold=_threshold_text(thresholds),
    )


async def run_transfer_tax_check(
    *,
    mint: str,
    rpc: RpcClient,
    thresholds: SafetyThresholds,
) -> TransferTaxOutcome:
    """Read ``mint`` and score its transfer fee against the configured percents."""
    try:
        result = await rpc.call("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
        view = _view(result)
    except (RpcError, _Undecodable):
        return _outcome(_blind("mint unreadable or extension data undecodable"), None)
    if view.kind == "spl-token":
        row = _row(
            "pass",
            value=0.0,
            reason=_zero_reason(view.kind),
            threshold=_threshold_text(thresholds),
        )
        return _outcome(row, view.slot)
    try:
        state = _fee_state(view.extensions)
        if state is None:
            return _outcome(
                _row(
                    "pass",
                    value=0.0,
                    reason=_zero_reason(view.kind),
                    threshold=_threshold_text(thresholds),
                ),
                view.slot,
            )
        older = _parse_fee(state.get("olderTransferFee"))
        newer = _parse_fee(state.get("newerTransferFee"))
        epoch = await _current_epoch(rpc)
        scored = _scored(older, newer, epoch, thresholds)
    except _Undecodable:
        return _outcome(_blind("mint unreadable or extension data undecodable"), view.slot)
    return _outcome(scored, view.slot)
