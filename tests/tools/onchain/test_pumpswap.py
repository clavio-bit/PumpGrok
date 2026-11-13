"""PumpSwap LP and liquidity from a real mainnet pool fixture.

The pool bytes are tests/fixtures/rpc/pumpswap/togo_pool.json, captured with
getAccountInfo. MockTransport serves those bytes. No network.
"""

from __future__ import annotations

import base64
import json
import random
from pathlib import Path

import httpx

from pumpgrok.config.settings import SafetyThresholds
from pumpgrok.core.errors import DataStale
from pumpgrok.replay.clock import FixtureClock
from pumpgrok.tools.market.liquidity import LiquidityIn, run_liquidity
from pumpgrok.tools.onchain.lp_status import LpStatusIn, run_lp_status
from pumpgrok.tools.onchain.pumpswap_layout import (
    PUMPSWAP_PROGRAM,
    WSOL_MINT,
    PumpSwapPool,
    decode_pool_account_base64,
)
from pumpgrok.tools.onchain.rpc import RpcClient
from pumpgrok.tools.safety.checks.readers import check_liquidity_depth, check_lp_status
from pumpgrok.tools.safety.score_report import ScoreReportIn, run_score_report

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = json.loads((ROOT / "fixtures/rpc/pumpswap/togo_pool.json").read_text(encoding="utf-8"))
START = 1469918176.385
MINT = FIXTURE["mint"]
CURVE = "8" * 44
POOL = FIXTURE["address"]
INTENDED = 200_000_000


def _pool() -> PumpSwapPool:
    return decode_pool_account_base64(
        [FIXTURE["data_base64"], "base64"],
        address=POOL,
        slot=FIXTURE["slot"],
    )


def test_a_zero_coin_creator_still_decodes() -> None:
    raw = bytearray(base64.b64decode(FIXTURE["data_base64"]))
    raw[211:243] = b"\x00" * 32
    encoded = base64.b64encode(bytes(raw)).decode("ascii")
    pool = decode_pool_account_base64([encoded, "base64"], address=POOL, slot=FIXTURE["slot"])
    assert pool.coin_creator == "1" * 32
    assert pool.base_mint == MINT


def test_fixture_prefix_matches_the_published_pool_fields() -> None:
    pool = _pool()
    assert pool.slot == FIXTURE["slot"]
    assert pool.base_mint == MINT
    assert pool.quote_mint == WSOL_MINT
    assert pool.lp_mint == FIXTURE["lp_mint"]
    assert pool.base_vault == FIXTURE["base_vault"]
    assert pool.quote_vault == FIXTURE["quote_vault"]
    assert pool.lp_supply == FIXTURE["pool_lp_supply"]
    assert pool.data_len == 301


def _rpc(
    handler: httpx.MockTransport | object,
) -> RpcClient:
    client = httpx.AsyncClient(transport=handler)  # type: ignore[arg-type]
    return RpcClient(["https://rpc.invalid"], client, FixtureClock(start=START), random.Random(1))


def _account(
    data: str, *, owner: str = PUMPSWAP_PROGRAM, slot: int | None = None
) -> dict[str, object]:
    return {
        "context": {"slot": FIXTURE["slot"] if slot is None else slot},
        "value": {
            "owner": owner,
            "lamports": 1,
            "data": [data, "base64"],
            "executable": False,
        },
    }


def _program_item(data: str = FIXTURE["data_base64"]) -> dict[str, object]:
    return {"pubkey": POOL, "account": _account(data)["value"]}


def _mint_parsed(supply: str) -> dict[str, object]:
    return {
        "context": {"slot": FIXTURE["slot"]},
        "value": {
            "owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
            "data": {
                "program": "spl-token",
                "parsed": {
                    "type": "mint",
                    "info": {"supply": supply, "decimals": 9, "isInitialized": True},
                },
            },
        },
    }


def _token_owner(owner: str) -> dict[str, object]:
    return {
        "context": {"slot": FIXTURE["slot"]},
        "value": {
            "data": {
                "parsed": {"type": "account", "info": {"owner": owner, "mint": FIXTURE["lp_mint"]}}
            }
        },
    }


def _rpc_ok(result: object) -> httpx.Response:
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})


def _program_response(
    request_params: list[object], items: list[dict[str, object]]
) -> httpx.Response:
    filters = request_params[1]["filters"]  # type: ignore[index]
    assert filters[0]["memcmp"]["offset"] == 43
    assert filters[0]["memcmp"]["bytes"] == MINT
    assert request_params[0] == PUMPSWAP_PROGRAM
    return _rpc_ok(items)


def _account_response(
    params: list[object],
    *,
    pool_data: str,
    lp_supply: str,
    holder_owner: str | None,
) -> httpx.Response:
    address = params[0]
    encoding = params[1].get("encoding")  # type: ignore[union-attr]
    if address == POOL and encoding == "base64":
        result: object = _account(pool_data)
    elif address == FIXTURE["lp_mint"] and encoding == "jsonParsed":
        result = _mint_parsed(lp_supply)
    elif holder_owner is not None and encoding == "jsonParsed":
        result = _token_owner(holder_owner)
    else:
        result = {"context": {"slot": 1}, "value": None}
    return _rpc_ok(result)


def _balance_response(address: object) -> httpx.Response:
    if address == FIXTURE["quote_vault"]:
        amount = FIXTURE["quote_vault_amount"]
    elif address == FIXTURE["base_vault"]:
        amount = FIXTURE["base_vault_amount"]
    else:
        amount = "0"
    value = {"amount": amount, "decimals": 9}
    return _rpc_ok({"context": {"slot": FIXTURE["slot"]}, "value": value})


def _handler(
    *,
    program_items: list[dict[str, object]] | None = None,
    pool_data: str = FIXTURE["data_base64"],
    lp_supply: str = "0",
    holders: list[dict[str, object]] | None = None,
    holder_owner: str | None = None,
    canonical: bool = True,
) -> httpx.MockTransport:
    items = [_program_item()] if program_items is None else program_items

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        method, params = body["method"], body["params"]
        if method == "getProgramAccountsV2":
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "method"}},
            )
        if method == "getProgramAccounts":
            return _program_response(params, items)
        if method == "getAccountInfo" and not canonical and params[0] == POOL:
            encoding = params[1].get("encoding") if isinstance(params[1], dict) else None
            if encoding == "base64":
                return _rpc_ok({"context": {"slot": 1}, "value": None})
        if method == "getAccountInfo":
            return _account_response(
                params, pool_data=pool_data, lp_supply=lp_supply, holder_owner=holder_owner
            )
        if method == "getTokenAccountBalance":
            return _balance_response(params[0])
        if method == "getTokenLargestAccounts":
            return _rpc_ok({"value": holders or []})
        raise AssertionError(method)

    return httpx.MockTransport(handler)


async def test_pool_not_found_is_blind() -> None:
    rpc = _rpc(_handler(program_items=[], canonical=False))
    lp = await run_lp_status(LpStatusIn(mint=MINT, curve_account=CURVE), rpc)
    assert lp["stage"] == "unknown"
    assert "other AMMs are not scored" in lp["detail"]
    row = check_lp_status(lp)
    assert row.status == "blind"
    assert "other AMMs are not scored" in row.reason


async def test_decode_failure_is_blind() -> None:
    short = _program_item("AAAA")
    rpc = _rpc(_handler(program_items=[short], canonical=False))
    lp = await run_lp_status(LpStatusIn(mint=MINT, curve_account=CURVE), rpc)
    row = check_lp_status(lp)
    assert row.status == "blind"
    assert "did not decode" in row.reason


async def test_confirmed_account_that_does_not_decode_is_blind() -> None:
    rpc = _rpc(_handler(pool_data=FIXTURE["data_base64"][:8]))
    lp = await run_lp_status(LpStatusIn(mint=MINT, curve_account=CURVE), rpc)
    row = check_lp_status(lp)
    assert row.status == "blind"
    assert "did not decode" in row.reason


async def test_burned_lp_from_the_real_pool_passes() -> None:
    rpc = _rpc(_handler())
    lp = await run_lp_status(LpStatusIn(mint=MINT, curve_account=CURVE), rpc)
    assert lp["pool"] == POOL
    assert lp["slot"] == FIXTURE["slot"]
    assert lp["base_vault"] == FIXTURE["base_vault"]
    assert lp["quote_vault"] == FIXTURE["quote_vault"]
    assert lp["lp_mint_supply"] == 0
    assert lp["pool_lp_supply"] == FIXTURE["pool_lp_supply"]
    row = check_lp_status(lp)
    assert row.status == "pass"
    assert row.value == "burned"
    assert "lookup=canonical_pda" in row.reason
    assert f"pool={POOL}" in row.reason
    assert f"slot={FIXTURE['slot']}" in row.reason
    assert "source=solana_rpc" in row.reason


async def test_creator_held_lp_fails() -> None:
    pool = _pool()
    supply = str(pool.lp_supply)
    holders = [{"address": "HolderTokenAccount11111111111111111111111", "amount": supply}]
    rpc = _rpc(_handler(lp_supply=supply, holders=holders, holder_owner=pool.coin_creator))
    lp = await run_lp_status(LpStatusIn(mint=MINT, curve_account=CURVE), rpc)
    row = check_lp_status(lp)
    assert row.status == "fail"
    assert row.value == "creator-held"
    assert "creator" in row.reason


async def test_liquidity_below_the_minimum_fails() -> None:
    rpc = _rpc(_handler())
    liq = await run_liquidity(
        LiquidityIn(mint=MINT, curve_account=CURVE, order_size_lamports=INTENDED),
        rpc,
    )
    assert liq["liquidity_lamports"] == int(FIXTURE["quote_vault_amount"])
    assert liq["slot"] == FIXTURE["slot"]
    assert liq["pool"] == POOL
    row = check_liquidity_depth(liq, INTENDED, SafetyThresholds())
    assert row.status == "fail"
    assert "intended size" in row.reason
    assert f"quote_vault={FIXTURE['quote_vault']}" in row.reason
    multiple = int(FIXTURE["quote_vault_amount"]) / INTENDED
    assert multiple < SafetyThresholds().liquidity_multiple_min


async def test_score_report_records_pool_evidence_and_a_low_liquidity_fail() -> None:
    rpc = _rpc(_handler())
    try:
        out = await run_score_report(
            ScoreReportIn(
                mint=MINT,
                curve_account=CURVE,
                intended_size_lamports=INTENDED,
                checks=["lp_status", "liquidity_depth"],
            ),
            rpc,
        )
    except DataStale as exc:
        raise AssertionError(exc) from exc
    statuses = {row["id"]: row for row in out["checks"]}
    assert statuses["lp_status"]["status"] == "pass"
    assert statuses["liquidity_depth"]["status"] == "fail"
    evidence = {item["check"]: item for item in out["evidence"]}
    assert evidence["lp_status"]["slot"] == FIXTURE["slot"]
    assert evidence["liquidity_depth"]["slot"] == FIXTURE["slot"]
    assert evidence["lp_status"]["source"] == "solana_rpc"
    assert POOL in statuses["lp_status"]["reason"]
    assert POOL in statuses["liquidity_depth"]["reason"]
