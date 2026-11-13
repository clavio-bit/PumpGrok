"""Honeypot is a required, quote-only Jupiter sell probe (amendment 2026-11-13).

MockTransport only. The probe may call ``/swap/v2/order`` and nothing else,
and it never sends a taker or user public key.
"""

from __future__ import annotations

import json
import random
from collections.abc import Callable
from decimal import Decimal

import httpx
import pytest

from pumpgrok.replay.clock import FixtureClock
from pumpgrok.tools.market.jupiter import QUOTE_URL
from pumpgrok.tools.onchain.rpc import RpcClient
from pumpgrok.tools.safety.checks.honeypot import (
    WSOL_MINT,
    HoneypotProbe,
    mint_decimals,
    order_quote_url_ok,
    raw_probe_units,
    run_honeypot_check,
)
from pumpgrok.tools.safety.score_report import ScoreReportIn, run_score_report

START = 1469918176.385
MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
CURVE = "8" * 44
_ALLOWED_PARAMS = frozenset({"inputMint", "outputMint", "amount", "slippageBps"})
_BANNED_PARAMS = frozenset({"taker", "user", "userPublicKey", "wallet", "publicKey", "payer"})
_BANNED_PATHS = ("/execute", "/build", "/submit")


def _assert_quote_only(request: httpx.Request) -> None:
    url = str(request.url)
    assert request.method == "GET"
    assert request.url.path.rstrip("/") == "/swap/v2/order"
    for banned in _BANNED_PATHS:
        assert banned not in url
    params = set(request.url.params)
    assert params == _ALLOWED_PARAMS
    assert not params & _BANNED_PARAMS
    assert request.url.params["inputMint"] == MINT
    assert request.url.params["outputMint"] == WSOL_MINT
    assert request.url.params["amount"] == "100000"
    assert "taker" not in request.url.params
    assert request.headers.get("x-api-key") is None


def _probe(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    ui: str = "1",
    quote_url: str = QUOTE_URL,
) -> HoneypotProbe:
    return HoneypotProbe(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        probe_ui_amount=Decimal(ui),
        quote_url=quote_url,
    )


def _meta(decimals: object = 5) -> dict[str, object]:
    return {"decimals": decimals}


def _pass_body(*, out: str = "33", impact: str = "0.4") -> dict[str, object]:
    return {
        "inputMint": MINT,
        "outputMint": WSOL_MINT,
        "inAmount": "100000",
        "outAmount": out,
        "priceImpact": impact,
        "routePlan": [
            {"swapInfo": {"label": "Whirlpool"}, "percent": 100},
        ],
    }


async def test_pass_records_out_amount_impact_and_route_labels() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        _assert_quote_only(request)
        return httpx.Response(200, json=_pass_body())

    outcome = await run_honeypot_check(mint=MINT, metadata=_meta(), probe=_probe(handler))
    assert outcome.result.status == "pass"
    assert outcome.result.value == "33"
    assert "outAmount=33" in outcome.result.reason
    assert "priceImpact=" in outcome.result.reason
    assert "routes=Whirlpool" in outcome.result.reason
    assert "warning" not in outcome.result.reason
    assert outcome.source == QUOTE_URL


async def test_high_price_impact_is_a_warning_and_still_passes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        _assert_quote_only(request)
        return httpx.Response(200, json=_pass_body(impact="25"))

    outcome = await run_honeypot_check(mint=MINT, metadata=_meta(), probe=_probe(handler))
    assert outcome.result.status == "pass"
    assert "warning: high price impact is not a honeypot" in outcome.result.reason


async def test_http_200_no_route_fails() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        _assert_quote_only(request)
        return httpx.Response(200, json={"error": "Could not find any route"})

    outcome = await run_honeypot_check(mint=MINT, metadata=_meta(), probe=_probe(handler))
    assert outcome.result.status == "fail"
    assert "route not found" in outcome.result.reason


async def test_no_route_fails() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        _assert_quote_only(request)
        return httpx.Response(400, json={"error": "No routes found"})

    outcome = await run_honeypot_check(mint=MINT, metadata=_meta(), probe=_probe(handler))
    assert outcome.result.status == "fail"
    assert "route not found" in outcome.result.reason


async def test_out_amount_zero_fails() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        _assert_quote_only(request)
        return httpx.Response(200, json=_pass_body(out="0"))

    outcome = await run_honeypot_check(mint=MINT, metadata=_meta(), probe=_probe(handler))
    assert outcome.result.status == "fail"
    assert outcome.result.value == "0"
    assert "outAmount is 0" in outcome.result.reason


async def test_out_amount_missing_fails() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        _assert_quote_only(request)
        body = _pass_body()
        body["outAmount"] = None
        return httpx.Response(200, json=body)

    outcome = await run_honeypot_check(mint=MINT, metadata=_meta(), probe=_probe(handler))
    assert outcome.result.status == "fail"
    assert "outAmount missing" in outcome.result.reason


async def test_timeout_is_blind() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        _assert_quote_only(request)
        raise httpx.ReadTimeout("timed out")

    outcome = await run_honeypot_check(mint=MINT, metadata=_meta(), probe=_probe(handler))
    assert outcome.result.status == "blind"
    assert "unavailable" in outcome.result.reason


async def test_http_500_is_blind() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        _assert_quote_only(request)
        return httpx.Response(500, json={"error": "unavailable"})

    outcome = await run_honeypot_check(mint=MINT, metadata=_meta(), probe=_probe(handler))
    assert outcome.result.status == "blind"
    assert "unavailable" in outcome.result.reason


async def test_ok_false_is_blind() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        _assert_quote_only(request)
        return httpx.Response(200, json={"ok": False, "error": "rejected"})

    outcome = await run_honeypot_check(mint=MINT, metadata=_meta(), probe=_probe(handler))
    assert outcome.result.status == "blind"
    assert "unavailable" in outcome.result.reason


async def test_invalid_mint_http_400_without_no_route_is_blind() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        _assert_quote_only(request)
        return httpx.Response(
            400,
            json={"error": "inputMint or outputMint is not a valid token mint"},
        )

    outcome = await run_honeypot_check(mint=MINT, metadata=_meta(), probe=_probe(handler))
    assert outcome.result.status == "blind"


@pytest.mark.parametrize("metadata", [None, {"decimals": "nope"}, {"decimals": True}, {}])
async def test_undecodable_decimals_are_blind_and_do_not_call_jupiter(
    metadata: dict[str, object] | None,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"jupiter was called for {request.url}")

    outcome = await run_honeypot_check(mint=MINT, metadata=metadata, probe=_probe(handler))
    assert outcome.result.status == "blind"
    assert outcome.result.reason == "mint decimals unreadable"
    assert outcome.source == "solana_rpc"


async def test_malformed_out_amount_is_blind() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        _assert_quote_only(request)
        body = _pass_body()
        body["outAmount"] = "not-a-number"
        return httpx.Response(200, json=body)

    outcome = await run_honeypot_check(mint=MINT, metadata=_meta(), probe=_probe(handler))
    assert outcome.result.status == "blind"
    assert "malformed" in outcome.result.reason


async def test_unwired_client_is_blind_when_decimals_are_known() -> None:
    outcome = await run_honeypot_check(mint=MINT, metadata=_meta(), probe=None)
    assert outcome.result.status == "blind"
    assert outcome.result.reason == "jupiter quote client unwired"
    assert outcome.source == "unwired"


async def test_fractional_probe_is_blind_and_does_not_call_jupiter() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"jupiter was called for {request.url}")

    outcome = await run_honeypot_check(
        mint=MINT,
        metadata=_meta(0),
        probe=_probe(handler, ui="1.5"),
    )
    assert outcome.result.status == "blind"
    assert "not a positive raw amount" in outcome.result.reason


async def test_execution_url_is_refused_before_any_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"refused url was called: {request.url}")

    outcome = await run_honeypot_check(
        mint=MINT,
        metadata=_meta(),
        probe=_probe(handler, quote_url="https://api.jup.ag/swap/v2/execute"),
    )
    assert outcome.result.status == "blind"
    assert "order endpoint" in outcome.result.reason
    assert not order_quote_url_ok("https://api.jup.ag/swap/v1/swap")
    assert not order_quote_url_ok("https://api.jup.ag/swap/v2/order?taker=abc")
    assert order_quote_url_ok(QUOTE_URL)


def test_probe_raw_units_follow_mint_decimals() -> None:
    assert raw_probe_units(5, Decimal("1")) == 100_000
    assert raw_probe_units(6, Decimal("1")) == 1_000_000
    assert raw_probe_units(0, Decimal("1.5")) is None
    assert mint_decimals({"decimals": 9}) == 9
    assert mint_decimals({"decimals": True}) is None


def _rpc(handler: Callable[[httpx.Request], httpx.Response]) -> RpcClient:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return RpcClient(["https://rpc.invalid"], client, FixtureClock(start=START), random.Random(1))


def _mint_rpc(decimals: int | None) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert body["method"] == "getAccountInfo"
        if decimals is None:
            result: dict[str, object] | None = {"context": {"slot": 1}, "value": None}
        else:
            result = {
                "context": {"slot": 1},
                "value": {
                    "data": {
                        "parsed": {
                            "info": {
                                "mintAuthority": None,
                                "freezeAuthority": None,
                                "decimals": decimals,
                                "supply": "1",
                                "isInitialized": True,
                                "extensions": [],
                            }
                        }
                    }
                },
            }
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    return handler


async def test_score_report_honeypot_fail_kills_even_when_the_score_would_clear() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        _assert_quote_only(request)
        return httpx.Response(200, json=_pass_body(out="0"))

    out = await run_score_report(
        ScoreReportIn(mint=MINT, curve_account=CURVE, checks=["honeypot"]),
        _rpc(_mint_rpc(5)),
        honeypot=_probe(handler),
    )
    assert out["score"] == 75
    assert out["verdict_recommendation"] == "KILL"
    assert out["hard_veto"] is False
    assert out["checks"][0]["id"] == "honeypot"
    assert out["checks"][0]["status"] == "fail"


async def test_score_report_honeypot_blind_kills() -> None:
    out = await run_score_report(
        ScoreReportIn(mint=MINT, curve_account=CURVE, checks=["honeypot"]),
        _rpc(_mint_rpc(None)),
        honeypot=None,
    )
    assert out["verdict_recommendation"] == "KILL"
    assert out["blind_checks"] == ["honeypot"]
    assert out["advisory_blind_checks"] == []
    assert out["checks"][0]["status"] == "blind"
    assert out["checks"][0]["reason"] == "mint decimals unreadable"


async def test_score_report_honeypot_pass_can_clear() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        _assert_quote_only(request)
        return httpx.Response(200, json=_pass_body())

    out = await run_score_report(
        ScoreReportIn(mint=MINT, curve_account=CURVE, checks=["honeypot"]),
        _rpc(_mint_rpc(5)),
        honeypot=_probe(handler),
    )
    assert out["verdict_recommendation"] == "CLEAR"
    assert out["score"] == 100
    assert out["blind_checks"] == []
    assert "Whirlpool" in out["checks"][0]["reason"]
