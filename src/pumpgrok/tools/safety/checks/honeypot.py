"""Required honeypot check: quote-only Jupiter round trip (amendment 2026-11-13).

Paper only. The probe asks Jupiter Swap V2 for two order quotes and never
sends a taker or user public key. It never calls a build, execute, submit,
or signing endpoint.

The size is the intended position in lamports: ``paper_equity_sol *
max_trade_fraction`` unless ``safety.honeypot_probe_sol`` overrides it.
Leg one buys the mint with that much WSOL. Leg two sells the token
``outAmount`` back to WSOL.

A blind result on this required check forces KILL in ``safety.score_report``.
No sell route, or a sell ``outAmount`` that is missing or 0, is a fail, which
also forces KILL. No buy route is blind (the position cannot be entered).
Sell price impact above ``max_slippage_bps`` is a warning and does not fail
the check.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx

from pumpgrok.core.units import sol_to_lamports
from pumpgrok.tools.market.jupiter import QUOTE_URL, get_quote
from pumpgrok.tools.safety.checks import CheckResult

WSOL_MINT = "So11111111111111111111111111111111111111112"
ORDER_PATH = "/swap/v2/order"
_BANNED_URL_MARKERS: tuple[str, ...] = (
    "/execute",
    "/build",
    "/submit",
    "taker=",
    "userpublickey=",
    "publickey=",
)
MAX_MINT_DECIMALS = 18
DEFAULT_SLIPPAGE_BPS = 50
DEFAULT_MAX_SLIPPAGE_BPS = 300
_RETAINED_QUANTUM = Decimal("0.00000001")
_WEIGHT = Decimal("0.15")
_IMPACT_WARNING = "warning: position-size sell impact exceeds max slippage"


@dataclass(frozen=True, slots=True)
class HoneypotProbe:
    """Quote client and the intended-position size in lamports."""

    client: httpx.AsyncClient
    probe_lamports: int
    max_slippage_bps: int = DEFAULT_MAX_SLIPPAGE_BPS
    slippage_bps: int = DEFAULT_SLIPPAGE_BPS
    quote_url: str = QUOTE_URL
    timeout_seconds: float = 12.0


@dataclass(frozen=True, slots=True)
class HoneypotOutcome:
    """The check row plus the evidence source the report should record."""

    result: CheckResult
    source: str


def order_quote_url_ok(url: str) -> bool:
    """True only for the HTTPS order endpoint, with no extra query or banned path."""
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.query or parts.fragment:
        return False
    path = parts.path.rstrip("/") or "/"
    if path != ORDER_PATH:
        return False
    lowered = url.lower()
    return not any(marker in lowered for marker in _BANNED_URL_MARKERS)


def intended_probe_lamports(
    *,
    paper_equity_sol: Decimal,
    max_trade_fraction: Decimal,
    override_sol: Decimal | None = None,
) -> int:
    """Lamports for the honeypot buy.

    ``override_sol`` wins when it is set. Otherwise the size is
    ``paper_equity_sol * max_trade_fraction``. A non-positive result is 0,
    which the check treats as blind.
    """
    sol = override_sol if override_sol is not None else paper_equity_sol * max_trade_fraction
    if sol <= 0:
        return 0
    return sol_to_lamports(sol)


def mint_decimals(metadata: dict[str, Any] | None) -> int | None:
    """Decimals from a mint-metadata payload, or ``None`` when they cannot be read."""
    if not metadata:
        return None
    decimals = metadata.get("decimals")
    if isinstance(decimals, bool) or not isinstance(decimals, int):
        return None
    if not 0 <= decimals <= MAX_MINT_DECIMALS:
        return None
    return decimals


def _row(
    status: Literal["pass", "warn", "fail", "blind"],
    *,
    value: str | None,
    reason: str,
) -> CheckResult:
    return CheckResult(
        id="honeypot",
        status=status,
        value=value,
        threshold="sell outAmount>0",
        weight=_WEIGHT,
        reason=reason,
    )


def _blind(reason: str) -> CheckResult:
    return _row("blind", value=None, reason=reason)


def _outcome(result: CheckResult, source: str) -> HoneypotOutcome:
    return HoneypotOutcome(result=result, source=source)


def _route_labels(route_plan: Any) -> str:
    labels: list[str] = []
    if isinstance(route_plan, list):
        for hop in route_plan:
            if not isinstance(hop, dict):
                continue
            info = hop.get("swapInfo")
            if isinstance(info, dict):
                label = info.get("label")
                if isinstance(label, str) and label:
                    labels.append(label)
    return ",".join(labels) if labels else "none"


def _parse_out_amount(value: Any) -> int | None:
    """Non-negative integer raw units, or ``None`` when the field is malformed."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return None


def _impact_view(quote: dict[str, Any]) -> tuple[str, Decimal | None]:
    """Percentage-point text and absolute impact in basis points.

    ``get_quote`` stores v2 ``priceImpact`` (percentage points) as
    ``priceImpactRatio = priceImpact / 100``. Basis points are that ratio
    times 10_000.
    """
    ratio = quote.get("priceImpactRatio")
    if not isinstance(ratio, (int, float)) or isinstance(ratio, bool):
        return "unknown", None
    try:
        points = Decimal(str(ratio)) * Decimal(100)
        bps = abs(points) * Decimal(100)
    except (ArithmeticError, InvalidOperation, ValueError):
        return "unknown", None
    return format(points, "f"), bps


def _retained(sell_out: int, buy_in: int) -> str:
    """``sell_out / buy_in``, quantized to 8 decimal places."""
    if buy_in <= 0:
        return "undefined"
    ratio = (Decimal(sell_out) / Decimal(buy_in)).quantize(
        _RETAINED_QUANTUM, rounding=ROUND_HALF_UP
    )
    return format(ratio, "f")


def _evidence(
    *,
    buy_out: int,
    sell_out: int,
    buy_in: int,
    buy_quote: dict[str, Any],
    sell_quote: dict[str, Any],
    max_slippage_bps: int,
) -> str:
    impact, bps = _impact_view(sell_quote)
    text = (
        f"buyOut={buy_out} sellOut={sell_out} buyIn={buy_in} priceImpact={impact} "
        f"retained={_retained(sell_out, buy_in)} "
        f"buyRoutes={_route_labels(buy_quote.get('routePlan'))} "
        f"sellRoutes={_route_labels(sell_quote.get('routePlan'))}"
    )
    if bps is not None and bps > Decimal(max_slippage_bps):
        return f"{text} {_IMPACT_WARNING}"
    return text


def _buy_token_out(quote: dict[str, Any]) -> int | CheckResult:
    """Raw token units from the buy leg, or a blind row when the leg cannot size a sell."""
    if quote.get("ok") is not True:
        return _blind("jupiter buy quote unavailable; required check is blind")
    if quote.get("noRoute") is True:
        return _blind("no buy route")
    if "outAmount" not in quote or quote.get("outAmount") is None:
        return _blind("jupiter buy quote outAmount is missing; required check is blind")
    out = _parse_out_amount(quote.get("outAmount"))
    if out is None:
        return _blind("jupiter buy quote outAmount is malformed; required check is blind")
    if out <= 0:
        return _blind("jupiter buy quote outAmount is 0; required check is blind")
    return out


def _sell_row(
    quote: dict[str, Any],
    *,
    buy_out: int,
    buy_in: int,
    buy_quote: dict[str, Any],
    max_slippage_bps: int,
) -> CheckResult:
    """Map the sell leg onto pass, fail, or blind."""
    if quote.get("ok") is not True:
        return _blind("jupiter sell quote unavailable; required check is blind")
    if quote.get("noRoute") is True:
        return _row(
            "fail",
            value=None,
            reason="jupiter sell route not found; honeypot suspected",
        )
    if "outAmount" not in quote or quote.get("outAmount") is None:
        return _row(
            "fail",
            value=None,
            reason="jupiter sell outAmount missing; honeypot suspected",
        )
    out = _parse_out_amount(quote.get("outAmount"))
    if out is None:
        return _blind("jupiter sell quote outAmount is malformed; required check is blind")
    evidence = _evidence(
        buy_out=buy_out,
        sell_out=out,
        buy_in=buy_in,
        buy_quote=buy_quote,
        sell_quote=quote,
        max_slippage_bps=max_slippage_bps,
    )
    if out <= 0:
        return _row(
            "fail",
            value="0",
            reason=f"jupiter sell outAmount is 0; honeypot suspected {evidence}",
        )
    return _row("pass", value=str(out), reason=f"jupiter sell route outAmount={out} {evidence}")


def _blocked(
    metadata: dict[str, Any] | None, probe: HoneypotProbe | None
) -> HoneypotOutcome | None:
    """Blind rows that are known before either quote is sent."""
    if mint_decimals(metadata) is None:
        return _outcome(_blind("mint decimals unreadable"), "solana_rpc")
    if probe is None:
        return _outcome(_blind("jupiter quote client unwired"), "unwired")
    if not order_quote_url_ok(probe.quote_url):
        return _outcome(_blind("jupiter quote url is not the order endpoint"), "unwired")
    if probe.probe_lamports <= 0:
        return _outcome(_blind("honeypot probe size is not a positive lamport amount"), "config")
    return None


async def _quote(
    probe: HoneypotProbe,
    input_mint: str,
    output_mint: str,
    amount: int,
) -> dict[str, Any]:
    return await get_quote(
        input_mint,
        output_mint,
        amount,
        probe.slippage_bps,
        probe.client,
        quote_url=probe.quote_url,
        timeout_seconds=probe.timeout_seconds,
    )


async def run_honeypot_check(
    *,
    mint: str,
    metadata: dict[str, Any] | None,
    probe: HoneypotProbe | None,
) -> HoneypotOutcome:
    """Quote a buy of the intended position, then a sell of the tokens received."""
    blocked = _blocked(metadata, probe)
    if blocked is not None:
        return blocked
    assert probe is not None
    buy = await _quote(probe, WSOL_MINT, mint, probe.probe_lamports)
    sized = _buy_token_out(buy)
    if isinstance(sized, CheckResult):
        return _outcome(sized, probe.quote_url)
    sell = await _quote(probe, mint, WSOL_MINT, sized)
    return _outcome(
        _sell_row(
            sell,
            buy_out=sized,
            buy_in=probe.probe_lamports,
            buy_quote=buy,
            max_slippage_bps=probe.max_slippage_bps,
        ),
        probe.quote_url,
    )
