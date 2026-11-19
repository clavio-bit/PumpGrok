"""Paper-only watch for an open position. Never signs and never sends.

The rug seat calls :func:`watch_open_position` on a mint it already holds in
paper. A trigger is written to the paper ledger as ``Escalation``
``kind=rug_alert`` and returned as a sell signal. Nothing here imports a
live adapter or builds a transaction.

Watched now:

- creator token balance falls, or a recent parsed balance of this mint
  for the creator drops inside the bounded signature sample
- LP status fails (creator-held LP) or the stage leaves ``not_migrated``
  into a non-passing LP read
- mint or freeze authority is set
- real SOL liquidity drops by half versus the snapshot, or is zero while
  the curve is still open
- a honeypot re-probe fails or is blind

An unreadable source is listed on the report and does not by itself emit
an exit. The supervisor tick does not poll this yet, so replay stays
unchanged. A caller that wants the exit must pass ``close`` (the paper
adapter) or submit the returned signal through the paper path.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, cast

from pumpgrok.core.errors import DataStale
from pumpgrok.replay.clock import Clock, format_ts_utc
from pumpgrok.tools.onchain.authorities import check_mint
from pumpgrok.tools.onchain.curve_layout import CurveDecodeError, decode_curve_account_base64
from pumpgrok.tools.onchain.lp_status import LpStatusIn, run_lp_status
from pumpgrok.tools.onchain.rpc import RpcClient, RpcError
from pumpgrok.tools.safety.checks.honeypot import HoneypotProbe, run_honeypot_check
from pumpgrok.tools.safety.checks.readers import check_lp_status

_SIGNATURES = 5
_LIQUIDITY_DROP = Decimal("0.50")


@dataclass(frozen=True, slots=True)
class WatchSnapshot:
    """Previous paper reading. Absent fields skip the delta checks."""

    creator_token_units: int | None = None
    real_sol_lamports: int | None = None
    lp_value: str | None = None


@dataclass(frozen=True, slots=True)
class WatchTrigger:
    """One reason to flatten the paper position."""

    kind: str
    detail: str


@dataclass(frozen=True, slots=True)
class PaperExitSignal:
    """A paper sell. ``reason`` is the ledger exit reason, never a live send."""

    mint: str
    reason: str
    side: str = "sell"


@dataclass(frozen=True, slots=True)
class WatchReport:
    """Triggers that should exit, plus sources that could not be read."""

    triggers: list[WatchTrigger] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)
    signal: PaperExitSignal | None = None


def _amount(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return None


async def _creator_balance(rpc: RpcClient, creator: str, mint: str) -> int | None:
    try:
        result = await rpc.call(
            "getTokenAccountsByOwner",
            [creator, {"mint": mint}, {"encoding": "jsonParsed"}],
        )
    except RpcError:
        return None
    rows = result.get("value") if isinstance(result, dict) else None
    if not isinstance(rows, list):
        return None
    total = 0
    for row in rows:
        if not isinstance(row, dict):
            return None
        data = row.get("account", {})
        parsed = data.get("data", {}) if isinstance(data, dict) else {}
        info = parsed.get("parsed", {}).get("info", {}) if isinstance(parsed, dict) else {}
        token = info.get("tokenAmount", {}) if isinstance(info, dict) else {}
        amount = _amount(token.get("amount") if isinstance(token, dict) else None)
        if amount is None:
            return None
        total += amount
    return total


def _balance_drop(meta: object, creator: str, mint: str) -> bool:
    """True when the creator's balance of ``mint`` fell inside one transaction."""
    if not isinstance(meta, dict):
        return False
    pre = meta.get("preTokenBalances")
    post = meta.get("postTokenBalances")
    if not isinstance(pre, list) or not isinstance(post, list):
        return False

    def _owned(rows: list[object]) -> int | None:
        total = 0
        seen = False
        for row in rows:
            if not isinstance(row, dict) or row.get("mint") != mint or row.get("owner") != creator:
                continue
            amount = _amount((row.get("uiTokenAmount") or {}).get("amount"))
            if amount is None:
                return None
            total += amount
            seen = True
        return total if seen else 0

    before = _owned(pre)
    after = _owned(post)
    if before is None or after is None:
        return False
    return after < before


async def _creator_sold_recently(rpc: RpcClient, creator: str, mint: str) -> bool | None:
    try:
        sigs = await rpc.call("getSignaturesForAddress", [creator, {"limit": _SIGNATURES}])
    except RpcError:
        return None
    if not isinstance(sigs, list):
        return None
    for entry in sigs:
        if not isinstance(entry, dict) or not isinstance(entry.get("signature"), str):
            continue
        try:
            tx = await rpc.call(
                "getTransaction",
                [
                    entry["signature"],
                    {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0},
                ],
            )
        except RpcError:
            return None
        meta = tx.get("meta") if isinstance(tx, dict) else None
        if _balance_drop(meta, creator, mint):
            return True
    return False


async def _curve_real_sol(rpc: RpcClient, curve_account: str) -> tuple[int | None, bool | None]:
    try:
        result = await rpc.call("getAccountInfo", [curve_account, {"encoding": "base64"}])
    except RpcError:
        return None, None
    value = result.get("value") if isinstance(result, dict) else None
    if not isinstance(value, dict):
        return None, None
    try:
        state = decode_curve_account_base64(value.get("data"))
    except CurveDecodeError:
        return None, None
    if not state.quote_is_sol:
        return None, state.complete
    return state.real_sol_reserves_lamports, state.complete


async def _creator_triggers(
    rpc: RpcClient, mint: str, creator: str | None, snapshot: WatchSnapshot
) -> tuple[list[WatchTrigger], list[str]]:
    if creator is None:
        return [], ["creator"]
    balance = await _creator_balance(rpc, creator, mint)
    triggers: list[WatchTrigger] = []
    unread: list[str] = []
    if balance is None:
        unread.append("creator_balance")
    elif snapshot.creator_token_units is not None and balance < snapshot.creator_token_units:
        triggers.append(
            WatchTrigger(
                "creator_sold",
                f"creator token balance fell from {snapshot.creator_token_units} to {balance}",
            )
        )
    recent = await _creator_sold_recently(rpc, creator, mint)
    if recent is None:
        unread.append("creator_signatures")
    elif recent:
        triggers.append(
            WatchTrigger(
                "creator_transfer",
                "creator token balance of this mint fell in a recent transaction",
            )
        )
    return triggers, unread


async def _lp_triggers(
    rpc: RpcClient, mint: str, curve_account: str, snapshot: WatchSnapshot
) -> tuple[list[WatchTrigger], list[str]]:
    try:
        lp = await run_lp_status(LpStatusIn(mint=mint, curve_account=curve_account), rpc)
    except DataStale:
        return [], ["lp_status"]
    row = check_lp_status(lp)
    triggers: list[WatchTrigger] = []
    left_curve = (
        snapshot.lp_value == "not_migrated" and row.value != "not_migrated" and row.status != "pass"
    )
    if row.status == "fail":
        triggers.append(WatchTrigger("lp_change", row.reason))
    elif left_curve:
        triggers.append(WatchTrigger("lp_change", f"LP left not_migrated: {row.reason}"))
    return triggers, []


async def _authority_triggers(rpc: RpcClient, mint: str) -> tuple[list[WatchTrigger], list[str]]:
    report = await check_mint(mint, rpc)
    if not report.get("ok"):
        return [], ["authorities"]
    triggers: list[WatchTrigger] = []
    if report.get("hasMintAuthority"):
        triggers.append(WatchTrigger("mint_authority", "mint authority is set"))
    if report.get("hasFreezeAuthority"):
        triggers.append(WatchTrigger("freeze_authority", "freeze authority is set"))
    return triggers, []


async def _liquidity_triggers(
    rpc: RpcClient, curve_account: str, snapshot: WatchSnapshot
) -> tuple[list[WatchTrigger], list[str]]:
    real, complete = await _curve_real_sol(rpc, curve_account)
    if complete or (real is None and complete is not None):
        return [], [] if complete else ["liquidity"]
    if real is None:
        return [], ["liquidity"]
    previous = snapshot.real_sol_lamports
    dropped = (
        previous is not None
        and previous > 0
        and Decimal(real) < Decimal(previous) * (Decimal(1) - _LIQUIDITY_DROP)
    )
    if real <= 0 or dropped:
        detail = (
            "open curve real SOL is 0" if real <= 0 else f"real SOL fell from {previous} to {real}"
        )
        return [WatchTrigger("liquidity_drop", detail)], []
    return [], []


async def _honeypot_triggers(
    mint: str, probe: HoneypotProbe | None, metadata: dict[str, Any] | None
) -> tuple[list[WatchTrigger], list[str]]:
    if probe is None:
        return [], []
    outcome = await run_honeypot_check(mint=mint, metadata=metadata, probe=probe)
    if outcome.result.status in ("fail", "blind"):
        return [WatchTrigger("honeypot_reprobe", outcome.result.reason)], []
    return [], []


async def watch_open_position(
    *,
    mint: str,
    curve_account: str,
    rpc: RpcClient,
    creator: str | None = None,
    snapshot: WatchSnapshot | None = None,
    honeypot: HoneypotProbe | None = None,
    metadata: dict[str, Any] | None = None,
) -> WatchReport:
    """Read the open paper position and list flatten triggers. No send."""
    prior = snapshot if snapshot is not None else WatchSnapshot()
    triggers: list[WatchTrigger] = []
    unread: list[str] = []
    for batch in (
        await _creator_triggers(rpc, mint, creator, prior),
        await _lp_triggers(rpc, mint, curve_account, prior),
        await _authority_triggers(rpc, mint),
        await _liquidity_triggers(rpc, curve_account, prior),
        await _honeypot_triggers(mint, honeypot, metadata),
    ):
        found, missing = batch
        triggers.extend(found)
        unread.extend(missing)
    signal = PaperExitSignal(mint=mint, reason="rug_alert") if triggers else None
    return WatchReport(triggers=triggers, unreadable=unread, signal=signal)


def append_paper_watch(
    store: Any,
    clock: Clock,
    report: WatchReport,
    *,
    correlation_id: str = "desk",
) -> dict[str, Any] | None:
    """Append one paper ``rug_alert`` escalation. A repeat in the same minute is a no-op.

    The store is typed loosely so this module does not pull the execution
    stack. It must be a paper ``LedgerStore``.
    """
    if report.signal is None or not report.triggers:
        return None
    kinds = ",".join(trigger.kind for trigger in report.triggers)
    detail = f"mint={report.signal.mint} triggers={kinds}"
    minute = int(clock.now()) // 60
    digest = hashlib.sha256(f"{report.signal.mint}|{kinds}|{minute}".encode()).hexdigest()
    try:
        return cast(
            dict[str, Any],
            store.append(
                ts_utc=format_ts_utc(clock.now()),
                source="paper",
                correlation_id=correlation_id,
                idempotency_key=digest,
                type="Escalation",
                payload={
                    "source": "paper",
                    "kind": "rug_alert",
                    "seat": "bot-05-rug",
                    "detail": detail,
                },
            ),
        )
    except sqlite3.IntegrityError:
        return None


async def emit_paper_watch(
    store: Any,
    clock: Clock,
    report: WatchReport,
    *,
    close: Callable[[str, str], Awaitable[None]] | None = None,
    correlation_id: str = "desk",
) -> dict[str, Any] | None:
    """Write the paper escalation and, when given, ask the paper closer to sell.

    ``close`` is the paper adapter (mint, reason). It must not be a live send.
    """
    row = append_paper_watch(store, clock, report, correlation_id=correlation_id)
    if report.signal is not None and close is not None:
        await close(report.signal.mint, report.signal.reason)
    return row
