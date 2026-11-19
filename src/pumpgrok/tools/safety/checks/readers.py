"""Concentration, deployer, LP, extension and depth checks (TOOL-003).

Pure functions over the typed reader payloads; thresholds come from
``SafetyThresholds`` (config), never from literals.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pumpgrok.config.settings import SafetyThresholds
from pumpgrok.tools.safety.checks import CheckResult

_BURN_OWNERS = frozenset(
    {
        "1nc1nerator11111111111111111111111111111111",
        "11111111111111111111111111111111",
    }
)
_LP_WEIGHT = Decimal("0.15")


def _lp_row(
    status: Literal["pass", "warn", "fail", "blind"],
    *,
    value: str | None,
    reason: str,
) -> CheckResult:
    return CheckResult(
        id="lp_status",
        status=status,
        value=value,
        threshold="burned or locked",
        weight=_LP_WEIGHT,
        reason=reason,
    )


def _bucket(owner: str, amount: int, creators: set[str], pool: object) -> str:
    if owner in _BURN_OWNERS:
        return "burn"
    if owner in creators:
        return "creator"
    if owner == pool:
        return "pool"
    if amount > 0:
        return "other"
    return "zero"


def _one_holder(holder: object) -> tuple[str, int] | None:
    if not isinstance(holder, dict):
        return None
    owner = holder.get("owner")
    amount = holder.get("amount")
    if not isinstance(owner, str) or isinstance(amount, bool) or not isinstance(amount, int):
        return None
    if amount < 0:
        return None
    return owner, amount


def _holder_amounts(lp: dict[str, Any], supply: int) -> tuple[int, int, int, list[str]] | None:
    """Creator, burn, and pool amounts, plus unrecognized owners. None if the sample is bad."""
    holders = lp.get("holders")
    if not isinstance(holders, list):
        return None
    creators = {
        item for item in (lp.get("creator"), lp.get("coin_creator")) if isinstance(item, str)
    }
    creator_amt = 0
    burn_amt = 0
    pool_amt = 0
    other: list[str] = []
    total = 0
    for holder in holders:
        parsed = _one_holder(holder)
        if parsed is None:
            return None
        owner, amount = parsed
        total += amount
        kind = _bucket(owner, amount, creators, lp.get("pool"))
        if kind == "burn":
            burn_amt += amount
        elif kind == "creator":
            creator_amt += amount
        elif kind == "pool":
            pool_amt += amount
        elif kind == "other":
            other.append(owner)
    if total != supply:
        return None
    return creator_amt, burn_amt, pool_amt, other


def _supply_row(supply: int, recorded: int, detail: str) -> CheckResult | None:
    if supply == 0 and recorded > 0:
        return _lp_row(
            "pass",
            value="burned",
            reason=(
                f"LP mint supply is 0 and the pool records lp_supply={recorded}; "
                f"burned, so it meets lp_lock_min_days. {detail}"
            ),
        )
    if supply == 0:
        return _lp_row(
            "blind",
            value=None,
            reason=f"LP mint supply and pool lp_supply are 0; {detail}",
        )
    return None


def _holder_row(lp: dict[str, Any], supply: int, detail: str) -> CheckResult:
    judged = _holder_amounts(lp, supply)
    if judged is None:
        return _lp_row(
            "blind",
            value=None,
            reason=f"LP holder sample does not cover supply; {detail}",
        )
    creator_amt, burn_amt, pool_amt, other = judged
    if creator_amt > 0:
        status, value, reason = (
            "fail",
            "creator-held",
            f"LP held by the creator ({creator_amt} of {supply}). {detail}",
        )
    elif other:
        status, value, reason = (
            "blind",
            None,
            f"LP held by an unrecognized wallet {other[0]}; {detail}",
        )
    elif burn_amt == supply:
        status, value, reason = ("pass", "burned", f"LP supply sits at a burn address. {detail}")
    elif pool_amt:
        status, value, reason = (
            "blind",
            None,
            f"LP is pool-owned and no lock expiry was read. {detail}",
        )
    else:
        status, value, reason = (
            "blind",
            None,
            f"LP location is not a burn or a known lock. {detail}",
        )
    return _lp_row(status, value=value, reason=reason)  # type: ignore[arg-type]


def _pumpswap_lp(lp: dict[str, Any]) -> CheckResult:
    """Burned LP passes. Creator-held LP fails. Anything else stays blind."""
    detail = str(lp.get("detail") or "PumpSwap pool")
    supply = lp.get("lp_mint_supply")
    recorded = lp.get("pool_lp_supply")
    if isinstance(supply, bool) or not isinstance(supply, int):
        return _lp_row("blind", value=None, reason=f"LP mint supply unreadable; {detail}")
    if isinstance(recorded, bool) or not isinstance(recorded, int):
        return _lp_row("blind", value=None, reason=f"pool lp_supply unreadable; {detail}")
    zero = _supply_row(supply, recorded, detail)
    if zero is not None:
        return zero
    return _holder_row(lp, supply, detail)


def check_lp_status(lp: dict[str, Any]) -> CheckResult:
    """Pass an open curve or burned PumpSwap LP. Never pass an unknown holder."""
    stage = str(lp.get("stage", "unknown"))
    if stage == "bonding_curve":
        progress = lp.get("bonding_progress_pct")
        if lp.get("quote_is_sol") is False:
            progress_text = "quote mint is not SOL, so progress is not a SOL percent"
        elif progress is None:
            progress_text = "bonding progress unread"
        else:
            progress_text = f"bonding progress {progress}%"
        return _lp_row(
            "pass",
            value="not_migrated",
            reason=f"bonding curve not migrated; {progress_text}; no AMM LP to lock",
        )
    if stage == "pumpswap":
        return _pumpswap_lp(lp)
    detail = lp.get("detail")
    if isinstance(detail, str) and detail:
        reason = detail
    else:
        reason = "liquidity stage could not be determined"
    return _lp_row("blind", value=stage, reason=reason)


def _joined(parts: list[str]) -> str:
    if not parts:
        return ""
    return "; " + "; ".join(parts)


def _excluded_phrase(top: dict[str, Any]) -> str:
    exclusions = top.get("exclusions")
    if not isinstance(exclusions, list):
        return ""
    rendered: list[str] = []
    for item in exclusions:
        if not isinstance(item, dict):
            continue
        address = item.get("address")
        why = item.get("reason")
        if isinstance(address, str) and address:
            because = why if isinstance(why, str) and why else "liquidity account"
            rendered.append(f"{address} ({because})")
    if not rendered:
        return ""
    return "excluded " + ", ".join(rendered)


def _pool_phrase(top: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("pool", "base_vault", "quote_vault"):
        value = top.get(key)
        if isinstance(value, str) and value:
            parts.append(f"{key}={value}")
    lookup = top.get("pool_lookup")
    if isinstance(lookup, str) and lookup:
        parts.append(lookup)
    return "; ".join(parts)


def _top10_detail(top: dict[str, Any]) -> str:
    return _joined([part for part in (_excluded_phrase(top), _pool_phrase(top)) if part])


def check_top10_concentration(top: dict[str, Any], thresholds: SafetyThresholds) -> CheckResult:
    """Fail/warn/pass the top-10 supply share. A blind reason is not a score."""
    blind = top.get("blind_reason")
    if isinstance(blind, str) and blind.strip():
        return CheckResult(
            id="top10_concentration",
            status="blind",
            value=None,
            threshold=None,
            weight=Decimal("0.15"),
            reason=blind.strip(),
        )
    share = Decimal(str(top.get("top10_share", "0"))) * Decimal(100)
    pct = float(share)
    status: Literal["fail", "warn", "pass"]
    if pct >= thresholds.top10_concentration_fail_pct:
        status = "fail"
    elif pct >= thresholds.top10_concentration_warn_pct:
        status = "warn"
    else:
        status = "pass"
    return CheckResult(
        id="top10_concentration",
        status=status,
        value=pct,
        threshold=thresholds.top10_concentration_fail_pct,
        weight=Decimal("0.15"),
        reason=f"top-10 holders own {pct:.2f}% of supply{_top10_detail(top)}",
    )


def check_deployer_history(dep: dict[str, Any]) -> CheckResult:
    """Fail adjudicated-bad creators; warn prolific ones; pass the rest."""
    if bool(dep.get("is_known_bad")) or int(dep.get("rugs", 0)) > 0:
        return CheckResult(
            id="deployer_history",
            status="fail",
            value=True,
            threshold="no known rugs",
            weight=Decimal("0.10"),
            reason="creator record shows known-bad history",
        )
    seen = int(dep.get("tokens_seen", 0))
    truncated = bool(dep.get("truncated"))
    if truncated:
        return CheckResult(
            id="deployer_history",
            status="warn",
            value=seen,
            threshold=3,
            weight=Decimal("0.10"),
            reason=(
                f"at least {seen} signatures in the window; "
                "scan budget exhausted before the lookback ended"
            ),
        )
    if seen >= 3:
        return CheckResult(
            id="deployer_history",
            status="warn",
            value=seen,
            threshold=3,
            weight=Decimal("0.10"),
            reason=(f"creator has {seen} signatures in the window; serial-launcher caution"),
        )
    return CheckResult(
        id="deployer_history",
        status="pass",
        value=seen,
        threshold=3,
        weight=Decimal("0.10"),
        reason="no known-bad creator history in the window",
    )


def check_token2022_extensions(meta: dict[str, Any]) -> CheckResult:
    """Warn when transfer-hook-style extensions are present."""
    extensions = meta.get("extensions") or []
    count = len(extensions)
    return CheckResult(
        id="token2022_extensions",
        status="warn" if count else "pass",
        value=float(count),
        threshold="none",
        weight=Decimal("0.05"),
        reason=(
            f"{count} token-2022 extensions present; review transfer hooks"
            if count
            else "no token-2022 extensions"
        ),
    )


def check_liquidity_depth(
    liq: dict[str, Any], intended_size_lamports: int, thresholds: SafetyThresholds
) -> CheckResult:
    """Fail/warn/pass the liquidity multiple against configured floors."""
    if liq.get("unscored"):
        detail = liq.get("detail")
        reason = str(detail) if isinstance(detail, str) and detail else "liquidity unreadable"
        return CheckResult(
            id="liquidity_depth",
            status="blind",
            value=None,
            threshold=thresholds.liquidity_multiple_min,
            weight=Decimal("0.15"),
            reason=reason,
        )
    liquidity = int(liq.get("liquidity_lamports", 0))
    detail = liq.get("detail")
    suffix = f"; {detail}" if isinstance(detail, str) and detail else ""
    if liquidity <= 0:
        return CheckResult(
            id="liquidity_depth",
            status="fail",
            value=0.0,
            threshold=thresholds.liquidity_multiple_min,
            weight=Decimal("0.15"),
            reason=f"no measurable liquidity for the intended size{suffix}",
        )
    if intended_size_lamports <= 0:
        return CheckResult(
            id="liquidity_depth",
            status="pass",
            value=float("inf"),
            threshold=thresholds.liquidity_multiple_min,
            weight=Decimal("0.15"),
            reason=f"no intended size; depth check vacuous{suffix}",
        )
    multiple = liquidity / intended_size_lamports
    status: Literal["fail", "warn", "pass"]
    if multiple < thresholds.liquidity_multiple_min:
        status = "fail"
    elif multiple < thresholds.liquidity_multiple_recommended:
        status = "warn"
    else:
        status = "pass"
    return CheckResult(
        id="liquidity_depth",
        status=status,
        value=multiple,
        threshold=thresholds.liquidity_multiple_min,
        weight=Decimal("0.15"),
        reason=f"liquidity is {multiple:.1f}x the intended size{suffix}",
    )
