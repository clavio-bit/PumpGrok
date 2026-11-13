"""Jupiter Swap V2 quote, informational only (STRUCT-002, R44, R38).

``GET`` ``https://api.jup.ag/swap/v2/order`` with no ``taker`` so all routers
compete. Never ``/execute``, ``/build`` or ``/submit``: this module cannot
place an order. The optional ``JUPITER_API_KEY`` is read once in the
composition root (``resolve_api_key``), carried as ``SecretStr`` and sent as
``x-api-key``; the keyless state is logged, never the key (Pitfall 3).

v1 unit trap (Pitfall 2): v1 ``priceImpactPct`` was a decimal ratio while v2
``priceImpact`` is percentage points. This module maps
``price_impact_ratio = priceImpact / 100`` and never reads ``priceImpactPct``.
``inAmount``/``outAmount`` handling follows the hand-authored fixture
(ASSUMED A2): both are mapped defensively with ``.get``.

A JSON body that says no route is returned with ``ok`` true and
``noRoute`` true, including on a non-200 status, so a sell-safety check
can fail closed on a missing route. Every other transport, HTTP-status
and decode failure still returns ``{"ok": False}``. A body whose own
``ok`` field is false is that same failure. This module still never
calls ``/execute``, ``/build`` or ``/submit``.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx
from pydantic import SecretStr

logger = logging.getLogger(__name__)

QUOTE_URL = "https://api.jup.ag/swap/v2/order"

# Phrases Jupiter uses when a sell (or any swap) has no route. Matched only
# against error-shaped fields, never against route labels or mint text.
_NO_ROUTE_MARKERS: tuple[str, ...] = (
    "no route",
    "no routes",
    "route not found",
    "routes not found",
    "could not find any route",
)
_ERROR_TEXT_KEYS: tuple[str, ...] = ("error", "message", "errorCode", "code")


def _failed() -> dict[str, Any]:
    """The pinned failure envelope. Callers compare it by equality."""
    return {"ok": False, "error": "jupiter quote failed"}


def response_says_no_route(payload: dict[str, Any]) -> bool:
    """True when error-shaped fields say the router found no route."""
    parts: list[str] = []
    for key in _ERROR_TEXT_KEYS:
        value = payload.get(key)
        if isinstance(value, str):
            parts.append(value.lower())
    text = " ".join(parts)
    return any(marker in text for marker in _NO_ROUTE_MARKERS)


def _json_object(response: httpx.Response) -> dict[str, Any] | None:
    """Decode a JSON object, or ``None`` when the body is not one."""
    try:
        data = response.json()
    except ValueError:
        return None
    if isinstance(data, dict):
        return data
    return None


def _price_impact_ratio(price_impact: Any) -> float | None:
    """Map v2 percentage-point ``priceImpact`` to a ratio. Malformed stays ``None``."""
    if price_impact is None:
        return None
    try:
        return float(price_impact) / 100
    except (ArithmeticError, TypeError, ValueError):
        return None


def _route_plan(route: Any) -> list[dict[str, Any]] | None:
    """Copy hop labels. A non-list plan is malformed and returns ``None``."""
    if not isinstance(route, list):
        return None
    return [
        {"swapInfo": hop.get("swapInfo", {}), "percent": hop.get("percent")}
        for hop in route
        if isinstance(hop, dict)
    ]


def _no_route_quote(data: dict[str, Any]) -> dict[str, Any]:
    """A decoded no-route body. ``ok`` stays true so callers can tell it from downtime."""
    return {
        "ok": True,
        "noRoute": True,
        "inputMint": data.get("inputMint"),
        "outputMint": data.get("outputMint"),
        "inAmount": data.get("inAmount"),
        "outAmount": data.get("outAmount"),
        "otherAmountThreshold": data.get("otherAmountThreshold"),
        "slippageBps": data.get("slippageBps"),
        "priceImpactRatio": None,
        "routePlan": [],
        "router": data.get("router"),
        "requestId": data.get("requestId"),
        "error": "no route",
        "raw": data,
    }


def _success_quote(data: dict[str, Any], route: list[dict[str, Any]]) -> dict[str, Any]:
    """Map a route-bearing body. ``raw`` is kept for callers and never submitted."""
    return {
        "ok": True,
        "inputMint": data.get("inputMint"),
        "outputMint": data.get("outputMint"),
        "inAmount": data.get("inAmount"),
        "outAmount": data.get("outAmount"),
        "otherAmountThreshold": data.get("otherAmountThreshold"),
        "slippageBps": data.get("slippageBps"),
        "priceImpactRatio": _price_impact_ratio(data.get("priceImpact")),
        "routePlan": route,
        "router": data.get("router"),
        "requestId": data.get("requestId"),
        "raw": data,
    }


def _quote_from_body(data: Any) -> dict[str, Any]:
    """Classify a decoded JSON body. Programming errors are not swallowed here."""
    if not isinstance(data, dict):
        return _failed()
    if response_says_no_route(data):
        return _no_route_quote(data)
    if data.get("ok") is False:
        return _failed()
    route = _route_plan(data.get("routePlan") or [])
    if route is None:
        return _failed()
    return _success_quote(data, route)


def _quote_from_response(response: httpx.Response) -> dict[str, Any]:
    """Classify one HTTP response. Non-200 is failure unless the body says no route."""
    if response.status_code != 200:
        data = _json_object(response)
        if data is not None and response_says_no_route(data):
            return _no_route_quote(data)
        logger.warning("jupiter quote failed error=%s", "HTTPStatusError")
        return _failed()
    try:
        data = response.json()
    except ValueError:
        logger.warning("jupiter quote failed error=%s", "ValueError")
        return _failed()
    return _quote_from_body(data)


def resolve_api_key() -> SecretStr:
    """Read ``JUPITER_API_KEY`` once in the composition root (Open Q6).

    This is deliberately not a ``PUMPGROK_`` Settings field: TOML must never
    hold secrets. Empty means keyless.
    """
    return SecretStr(os.environ.get("JUPITER_API_KEY", ""))


async def get_quote(
    input_mint: str,
    output_mint: str,
    amount: int,
    slippage_bps: int,
    client: httpx.AsyncClient,
    *,
    quote_url: str = QUOTE_URL,
    api_key: SecretStr | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    """Fetch a quote-only order.

    Transport failures, non-200 statuses that do not say no route, decode
    failures, and a body with ``ok: false`` return ``{"ok": False}`` and log
    the error class. A body that says no route returns ``noRoute: true``.
    Programming errors propagate; they are never swallowed.
    """
    key = api_key if api_key is not None else SecretStr("")
    keyless = not key.get_secret_value()
    logger.info("jupiter quote keyless=%s", keyless)
    headers: dict[str, str] = {}
    if not keyless:
        headers["x-api-key"] = key.get_secret_value()
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount),
        "slippageBps": str(slippage_bps),
    }
    try:
        resp = await client.get(quote_url, params=params, headers=headers, timeout=timeout_seconds)
    except httpx.HTTPError as exc:
        logger.warning("jupiter quote failed error=%s", type(exc).__name__)
        return _failed()
    return _quote_from_response(resp)
