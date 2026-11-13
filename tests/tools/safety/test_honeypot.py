"""Honeypot is a required, quote-only Jupiter round trip (amendment 2026-11-13).

MockTransport only. Each leg may call ``/swap/v2/order`` and nothing else,
and neither leg sends a taker or user public key.
"""

from __future__ import annotations

import json
import random
from collections.abc import Callable
from decimal import Decimal

import httpx
import pytest

from pumpgrok.core.risk_budget import RiskBudget
from pumpgrok.replay.clock import FixtureClock
from pumpgrok.tools.market.jupiter import QUOTE_URL
from pumpgrok.tools.onchain.rpc import RpcClient
from pumpgrok.tools.safety.checks.honeypot import (
    WSOL_MINT,
    HoneypotProbe,
    intended_probe_lamports,
    mint_decimals,
    order_quote_url_ok,
    run_honeypot_check,
)
from pumpgrok.tools.safety.score_report import ScoreReportIn, run_score_report

START = 1469918176.385
MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
CURVE = "8" * 44
PROBE_LAMPORTS = 200_000_000
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
    assert "taker" not in request.url.params
    assert request.headers.get("x-api-key") is None
    assert request.url.params["slippageBps"] == "50"


def _probe(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    lamports: int = PROBE_LAMPORTS,
    max_slippage_bps: int = 300,
    quote_url: str = QUOTE_URL,
) -> HoneypotProbe:
    return HoneypotProbe(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        probe_lamports=lamports,
        max_slippage_bps=max_slippage_bps,
        quote_url=quote_url,
    )


def _meta(decimals: object = 5) -> dict[str, object]:
    return {"decimals": decimals}


def _body(
    *,
    input_mint: str,
    output_mint: str,
    amount_in: str,
    out: str,
    impact: str = "0.4",
    label: str,
) -> dict[str, object]:
    return {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "inAmount": amount_in,
        "outAmount": out,
        "priceImpact": impact,
        "routePlan": [{"swapInfo": {"label": label}, "percent": 100}],
    }


def _legs(
    *,
    buy_out: str = "1000000000",
    sell_out: str = "150000000",
    sell_impact: str = "0.4",
    buy_status: int = 200,
    sell_status: int = 200,
    buy_body: dict[str, object] | None = None,
    sell_body: dict[str, object] | None = None,
    buy_error: BaseException | None = None,
    sell_error: BaseException | None = None,
) -> tuple[Callable[[httpx.Request], httpx.Response], dict[str, int]]:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        _assert_quote_only(request)
        calls["n"] += 1
        if calls["n"] == 1:
            assert request.url.params["inputMint"] == WSOL_MINT
            assert request.url.params["outputMint"] == MINT
            assert request.url.params["amount"] == str(PROBE_LAMPORTS)
            if buy_error is not None:
                raise buy_error
            body = buy_body
            if body is None:
                body = _body(
                    input_mint=WSOL_MINT,
                    output_mint=MINT,
                    amount_in=str(PROBE_LAMPORTS),
                    out=buy_out,
                    label="Pump.fun Amm",
                )
            return httpx.Response(buy_status, json=body)
        assert request.url.params["inputMint"] == MINT
        assert request.url.params["outputMint"] == WSOL_MINT
        assert request.url.params["amount"] == buy_out
        if sell_error is not None:
            raise sell_error
        body = sell_body
        if body is None:
            body = _body(
                input_mint=MINT,
                output_mint=WSOL_MINT,
                amount_in=buy_out,
                out=sell_out,
                impact=sell_impact,
                label="Whirlpool",
            )
        return httpx.Response(sell_status, json=body)

    return handler, calls


async def test_pass_records_both_legs_impact_retained_and_routes() -> None:
    handler, calls = _legs()
    outcome = await run_honeypot_check(mint=MINT, metadata=_meta(), probe=_probe(handler))
    assert calls["n"] == 2
    assert outcome.result.status == "pass"
    assert outcome.result.value == "150000000"
    reason = outcome.result.reason
    assert "buyOut=1000000000" in reason
    assert "sellOut=150000000" in reason
    assert "buyIn=200000000" in reason
    assert "priceImpact=0.4" in reason
    assert "retained=0.75000000" in reason
    assert "buyRoutes=Pump.fun Amm" in reason
    assert "sellRoutes=Whirlpool" in reason
    assert "warning" not in reason
    assert outcome.source == QUOTE_URL


async def test_sell_impact_above_max_slippage_warns_and_still_passes() -> None:
    handler, _calls = _legs(sell_impact="3.01")
    outcome = await run_honeypot_check(mint=MINT, metadata=_meta(), probe=_probe(handler))
    assert outcome.result.status == "pass"
    assert "position-size sell impact exceeds max slippage" in outcome.result.reason


async def test_sell_impact_at_max_slippage_does_not_warn() -> None:
    handler, _calls = _legs(sell_impact="3")
    outcome = await run_honeypot_check(mint=MINT, metadata=_meta(), probe=_probe(handler))
    assert outcome.result.status == "pass"
    assert "warning" not in outcome.result.reason


async def test_sell_no_route_fails() -> None:
    handler, calls = _legs(sell_status=400, sell_body={"error": "No routes found"})
    outcome = await run_honeypot_check(mint=MINT, metadata=_meta(), probe=_probe(handler))
    assert calls["n"] == 2
    assert outcome.result.status == "fail"
    assert "route not found" in outcome.result.reason


async def test_sell_out_amount_zero_fails() -> None:
    handler, _calls = _legs(sell_out="0")
    outcome = await run_honeypot_check(mint=MINT, metadata=_meta(), probe=_probe(handler))
    assert outcome.result.status == "fail"
    assert outcome.result.value == "0"
    assert "outAmount is 0" in outcome.result.reason
    assert "buyOut=1000000000" in outcome.result.reason


async def test_sell_out_amount_missing_fails() -> None:
    missing = {"inAmount": "1000000000", "outAmount": None, "routePlan": []}
    handler, _calls = _legs(sell_body=missing)
    outcome = await run_honeypot_check(mint=MINT, metadata=_meta(), probe=_probe(handler))
    assert outcome.result.status == "fail"
    assert "outAmount missing" in outcome.result.reason


async def test_buy_no_route_is_blind_and_does_not_sell() -> None:
    handler, calls = _legs(buy_status=400, buy_body={"error": "No routes found"})
    outcome = await run_honeypot_check(mint=MINT, metadata=_meta(), probe=_probe(handler))
    assert calls["n"] == 1
    assert outcome.result.status == "blind"
    assert outcome.result.reason == "no buy route"


async def test_buy_timeout_is_blind() -> None:
    handler, calls = _legs(buy_error=httpx.ReadTimeout("timed out"))
    outcome = await run_honeypot_check(mint=MINT, metadata=_meta(), probe=_probe(handler))
    assert calls["n"] == 1
    assert outcome.result.status == "blind"
    assert "buy quote unavailable" in outcome.result.reason


async def test_sell_timeout_is_blind() -> None:
    handler, calls = _legs(sell_error=httpx.ReadTimeout("timed out"))
    outcome = await run_honeypot_check(mint=MINT, metadata=_meta(), probe=_probe(handler))
    assert calls["n"] == 2
    assert outcome.result.status == "blind"
    assert "sell quote unavailable" in outcome.result.reason


async def test_buy_http_500_is_blind() -> None:
    handler, calls = _legs(buy_status=500, buy_body={"error": "unavailable"})
    outcome = await run_honeypot_check(mint=MINT, metadata=_meta(), probe=_probe(handler))
    assert calls["n"] == 1
    assert outcome.result.status == "blind"
    assert "buy quote unavailable" in outcome.result.reason


async def test_sell_http_500_is_blind() -> None:
    handler, _calls = _legs(sell_status=500, sell_body={"error": "unavailable"})
    outcome = await run_honeypot_check(mint=MINT, metadata=_meta(), probe=_probe(handler))
    assert outcome.result.status == "blind"
    assert "sell quote unavailable" in outcome.result.reason


async def test_buy_out_amount_zero_is_blind() -> None:
    handler, calls = _legs(buy_out="0")
    outcome = await run_honeypot_check(mint=MINT, metadata=_meta(), probe=_probe(handler))
    assert calls["n"] == 1
    assert outcome.result.status == "blind"
    assert "outAmount is 0" in outcome.result.reason


async def test_malformed_sell_out_amount_is_blind() -> None:
    handler, _calls = _legs(
        sell_body={
            "inAmount": "1000000000",
            "outAmount": "not-a-number",
            "priceImpact": "0.1",
            "routePlan": [],
        }
    )
    outcome = await run_honeypot_check(mint=MINT, metadata=_meta(), probe=_probe(handler))
    assert outcome.result.status == "blind"
    assert "malformed" in outcome.result.reason


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


async def test_unwired_client_is_blind_when_decimals_are_known() -> None:
    outcome = await run_honeypot_check(mint=MINT, metadata=_meta(), probe=None)
    assert outcome.result.status == "blind"
    assert outcome.result.reason == "jupiter quote client unwired"
    assert outcome.source == "unwired"


async def test_non_positive_probe_is_blind_and_does_not_call_jupiter() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"jupiter was called for {request.url}")

    outcome = await run_honeypot_check(
        mint=MINT,
        metadata=_meta(),
        probe=_probe(handler, lamports=0),
    )
    assert outcome.result.status == "blind"
    assert "not a positive lamport amount" in outcome.result.reason


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


def test_intended_position_defaults_to_two_tenths_of_a_sol() -> None:
    budget = RiskBudget()
    lamports = intended_probe_lamports(
        paper_equity_sol=budget.paper_equity_sol,
        max_trade_fraction=budget.max_trade_fraction,
        override_sol=None,
    )
    assert budget.paper_equity_sol == Decimal("10")
    assert budget.max_trade_fraction == Decimal("0.02")
    assert lamports == 200_000_000


def test_explicit_probe_sol_overrides_the_derived_size() -> None:
    lamports = intended_probe_lamports(
        paper_equity_sol=Decimal("10"),
        max_trade_fraction=Decimal("0.02"),
        override_sol=Decimal("0.5"),
    )
    assert lamports == 500_000_000


def test_mint_decimals_rejects_a_bool() -> None:
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
                    "owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                    "data": {
                        "program": "spl-token",
                        "parsed": {
                            "type": "mint",
                            "info": {
                                "mintAuthority": None,
                                "freezeAuthority": None,
                                "decimals": decimals,
                                "supply": "1",
                                "isInitialized": True,
                                "extensions": [],
                            },
                        },
                    },
                },
            }
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    return handler


async def test_score_report_honeypot_fail_kills_even_when_the_score_would_clear() -> None:
    handler, _calls = _legs(sell_out="0")
    out = await run_score_report(
        ScoreReportIn(mint=MINT, curve_account=CURVE, checks=["honeypot"]),
        _rpc(_mint_rpc(5)),
        honeypot=_probe(handler),
    )
    assert out["score"] == 75
    assert out["verdict_recommendation"] == "KILL"
    assert out["hard_veto"] is False
    assert out["failed_required_checks"] == ["honeypot"]
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
    handler, _calls = _legs()
    out = await run_score_report(
        ScoreReportIn(mint=MINT, curve_account=CURVE, checks=["honeypot"]),
        _rpc(_mint_rpc(5)),
        honeypot=_probe(handler),
    )
    assert out["verdict_recommendation"] == "CLEAR"
    assert out["score"] == 100
    assert out["blind_checks"] == []
    assert "Whirlpool" in out["checks"][0]["reason"]
    assert "retained=0.75000000" in out["checks"][0]["reason"]
