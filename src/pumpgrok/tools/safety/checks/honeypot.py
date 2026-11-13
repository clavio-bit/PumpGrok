"""Required honeypot check: quote-only Jupiter sell probe (amendment 2026-11-13).

Paper only. The probe asks Jupiter Swap V2 for a route that sells the mint
into WSOL. It never sends a taker or user public key, and it never calls a
build, execute, submit, or signing endpoint. A blind result on this required
check forces KILL in ``safety.score_report``. A missing route or a zero
``outAmount`` is a fail, which also forces KILL. High price impact is
recorded and does not, by itself, fail the check.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx

from pumpgrok.tools.market.jupiter import QUOTE_URL, get_quote
from pumpgrok.tools.safety.checks import CheckResult

WSOL_MINT = "So11111111111111111111111111111111111111112"
ORDER_PATH = "/swap/v2/order"
MAX_MINT_DECIMALS = 18
DEFAULT_SLIPPAGE_BPS = 50
DEFAULT_PROBE_UI = Decimal("1")
HIGH_PRICE_IMPACT_PCT = Decimal("5")
_WEIGHT = Decimal("0.15")
_BANNED_URL_MARKERS: tuple[str, ...] = (
    "/execute",
    "/build",
    "/submit",
    "taker=",
    "userpublickey=",
    "publickey=",
)


@dataclass(frozen=True, slots=True)
class HoneypotProbe:
    """Quote client and the whole-token size converted to raw units per mint."""

    client: httpx.AsyncClient
    probe_ui_amount: Decimal = DEFAULT_PROBE_UI
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


def raw_probe_units(decimals: int, probe_ui: Decimal) -> int | None:
    """Whole-token probe size times ``10**decimals``. Fractional raw units are refused."""
    if probe_ui <= 0:
        return None
    try:
        scaled = probe_ui * (Decimal(10) ** decimals)
        integral = scaled.to_integral_value()
    except (ArithmeticError, InvalidOperation):
        return None
    if scaled != integral:
        return None
    amount = int(integral)
    if amount <= 0:
        return None
    return amount


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
        threshold="outAmount>0",
        weight=_WEIGHT,
        reason=reason,
    )


def _blind(reason: str) -> CheckResult:
    return _row("blind", value=None, reason=reason)


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


def _impact_text(quote: dict[str, Any]) -> tuple[str, bool]:
    """Percentage-point price impact, and whether it is high enough to warn."""
    ratio = quote.get("priceImpactRatio")
    if not isinstance(ratio, (int, float)) or isinstance(ratio, bool):
        return "unknown", False
    try:
        points = Decimal(str(ratio)) * Decimal(100)
    except (ArithmeticError, InvalidOperation, ValueError):
        return "unknown", False
    return format(points, "f"), abs(points) >= HIGH_PRICE_IMPACT_PCT


def _evidence_bits(quote: dict[str, Any]) -> str:
    impact, high = _impact_text(quote)
    text = f"priceImpact={impact} routes={_route_labels(quote.get('routePlan'))}"
    if high:
        return f"{text} warning: high price impact is not a honeypot"
    return text


def _parse_out_amount(value: Any) -> int | None:
    """Non-negative integer raw units, or ``None`` when the field is malformed."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return None


def classify_sell_quote(quote: dict[str, Any], *, probe_raw: int) -> CheckResult:
    """Map one ``get_quote`` envelope onto pass, fail, or blind."""
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
    evidence = _evidence_bits(quote)
    if out <= 0:
        reason = f"jupiter sell outAmount is 0; honeypot suspected probeRaw={probe_raw} {evidence}"
        return _row("fail", value="0", reason=reason)
    return _row(
        "pass",
        value=str(out),
        reason=(f"jupiter sell route outAmount={out} probeRaw={probe_raw} {evidence}"),
    )


def _outcome(result: CheckResult, source: str) -> HoneypotOutcome:
    return HoneypotOutcome(result=result, source=source)


async def run_honeypot_check(
    *,
    mint: str,
    metadata: dict[str, Any] | None,
    probe: HoneypotProbe | None,
) -> HoneypotOutcome:
    """Read decimals, quote a sell to WSOL, and return the honeypot row."""
    decimals = mint_decimals(metadata)
    if decimals is None:
        return _outcome(_blind("mint decimals unreadable"), "solana_rpc")
    ui_amount = probe.probe_ui_amount if probe is not None else DEFAULT_PROBE_UI
    amount = raw_probe_units(decimals, ui_amount)
    if amount is None:
        return _outcome(
            _blind("honeypot probe size is not a positive raw amount"),
            "config",
        )
    if probe is None:
        return _outcome(_blind("jupiter quote client unwired"), "unwired")
    if not order_quote_url_ok(probe.quote_url):
        return _outcome(_blind("jupiter quote url is not the order endpoint"), "unwired")
    quote = await get_quote(
        mint,
        WSOL_MINT,
        amount,
        probe.slippage_bps,
        probe.client,
        quote_url=probe.quote_url,
        timeout_seconds=probe.timeout_seconds,
    )
    return _outcome(classify_sell_quote(quote, probe_raw=amount), probe.quote_url)
