"""PumpSwap lookup paths: canonical PDA, V2 pagination, legacy gPA. No network."""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import httpx

from pumpgrok.replay.clock import FixtureClock
from pumpgrok.tools.onchain.lp_status import LpStatusIn, run_lp_status
from pumpgrok.tools.onchain.pumpswap_layout import PUMPSWAP_PROGRAM
from pumpgrok.tools.onchain.pumpswap_pool import V2_PAGE_LIMIT, lookup_pumpswap_pool
from pumpgrok.tools.onchain.rpc import RpcClient
from pumpgrok.tools.safety.checks.readers import check_lp_status

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = json.loads((ROOT / "fixtures/rpc/pumpswap/togo_pool.json").read_text(encoding="utf-8"))
START = 1469918176.385
MINT = FIXTURE["mint"]
CURVE = "8" * 44
POOL = FIXTURE["address"]
OTHER = "B" * 44


def _program_item(data: str = FIXTURE["data_base64"], pubkey: str = POOL) -> dict[str, object]:
    return {
        "pubkey": pubkey,
        "account": {
            "owner": PUMPSWAP_PROGRAM,
            "lamports": 1,
            "data": [data, "base64"],
            "executable": False,
        },
    }


def _pool_value() -> dict[str, object]:
    return _program_item()["account"]  # type: ignore[return-value]


def _rpc(handler: httpx.MockTransport) -> RpcClient:
    client = httpx.AsyncClient(transport=handler)
    return RpcClient(["https://rpc.invalid"], client, FixtureClock(start=START), random.Random(1))


def _ok(result: object) -> httpx.Response:
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})


def _err(code: int) -> httpx.Response:
    return httpx.Response(
        200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": code, "message": "rpc"}}
    )


class _Script:
    """Scripted RPC. The first pool read is the canonical PDA attempt."""

    def __init__(
        self,
        *,
        canonical: dict[str, object] | None,
        v2: list[dict[str, Any]] | int | None = None,
        gpa: list[dict[str, object]] | int | None = None,
        confirm: dict[str, object] | None = None,
    ) -> None:
        self.canonical = canonical
        self.v2 = v2
        self.gpa = gpa
        self.confirm = confirm if confirm is not None else _pool_value()
        self.pool_reads = 0
        self.methods: list[str] = []
        self.v2_params: list[object] = []

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            method, params = body["method"], body["params"]
            self.methods.append(method)
            if method == "getAccountInfo":
                if params[0] == POOL and _base64(params):
                    self.pool_reads += 1
                    value = self.canonical if self.pool_reads == 1 else self.confirm
                    return _ok({"context": {"slot": FIXTURE["slot"]}, "value": value})
                return _ok({"context": {"slot": 1}, "value": None})
            if method == "getProgramAccountsV2":
                self.v2_params.append(params)
                if isinstance(self.v2, int):
                    return _err(self.v2)
                page = self._page(params)
                return _ok(page)
            if method == "getProgramAccounts":
                if isinstance(self.gpa, int):
                    return _err(self.gpa)
                return _ok(self.gpa or [])
            raise AssertionError(method)

        return httpx.MockTransport(handler)

    def _page(self, params: list[object]) -> dict[str, object]:
        pages = self.v2 if isinstance(self.v2, list) else []
        key = params[1].get("paginationKey") if isinstance(params[1], dict) else None
        index = 0 if not key else 1
        if index >= len(pages):
            return {"accounts": [], "paginationKey": None}
        return pages[index]


def _base64(params: list[object]) -> bool:
    config = params[1] if len(params) > 1 else None
    return isinstance(config, dict) and config.get("encoding") == "base64"


def _two_pages() -> list[dict[str, Any]]:
    decoy = _program_item("AAAA")
    return [
        {"accounts": [decoy], "paginationKey": "page-2"},
        {"accounts": [_program_item()], "paginationKey": None},
    ]


async def test_canonical_pda_hit_does_not_scan() -> None:
    script = _Script(canonical=_pool_value())
    lookup = await lookup_pumpswap_pool(_rpc(script.transport()), MINT)
    assert lookup.pool is not None
    assert lookup.pool.address == POOL
    assert lookup.reason == "lookup=canonical_pda"
    assert script.methods == ["getAccountInfo"]
    assert "getProgramAccounts" not in script.methods
    assert "getProgramAccountsV2" not in script.methods


async def test_pda_miss_falls_back_to_v2_one_result() -> None:
    script = _Script(canonical=None, v2=_two_pages())
    lookup = await lookup_pumpswap_pool(_rpc(script.transport()), MINT)
    assert lookup.pool is not None
    assert lookup.pool.address == POOL
    assert lookup.pool.slot == FIXTURE["slot"]
    assert lookup.reason == "lookup=getProgramAccountsV2"
    first = script.v2_params[0]
    assert first[0] == PUMPSWAP_PROGRAM
    config = first[1]
    assert config["limit"] == V2_PAGE_LIMIT
    assert config["filters"] == [{"memcmp": {"offset": 43, "bytes": MINT}}]
    assert "paginationKey" not in config
    assert script.v2_params[1][1]["paginationKey"] == "page-2"
    assert "getProgramAccounts" not in script.methods


async def test_v2_multiple_wsol_pools_are_blind() -> None:
    second = {"pubkey": OTHER, "account": _pool_value()}
    pages = [{"accounts": [_program_item(), second], "paginationKey": None}]
    script = _Script(canonical=None, v2=pages)
    lookup = await lookup_pumpswap_pool(_rpc(script.transport()), MINT)
    assert lookup.pool is None
    assert "not choosing one" in lookup.reason
    assert "lookup=getProgramAccountsV2" in lookup.reason
    fresh = _Script(canonical=None, v2=pages)
    lp = await run_lp_status(LpStatusIn(mint=MINT, curve_account=CURVE), _rpc(fresh.transport()))
    row = check_lp_status(lp)
    assert row.status == "blind"
    assert "lookup=getProgramAccountsV2" in row.reason


async def test_legacy_gpa_too_many_accounts_is_blind_not_a_crash() -> None:
    script = _Script(canonical=None, v2=-32601, gpa=-32600)
    lookup = await lookup_pumpswap_pool(_rpc(script.transport()), MINT)
    assert lookup.pool is None
    assert lookup.reason == "PumpSwap pool scan failed; lookup=getProgramAccounts"
    fresh = _Script(canonical=None, v2=-32601, gpa=-32600)
    lp = await run_lp_status(LpStatusIn(mint=MINT, curve_account=CURVE), _rpc(fresh.transport()))
    row = check_lp_status(lp)
    assert row.status == "blind"
    assert "rpc.invalid" not in row.reason
    assert "-32600" not in row.reason


async def test_v2_unavailable_uses_one_legacy_pool() -> None:
    script = _Script(canonical=None, v2=-32601, gpa=[_program_item()])
    lookup = await lookup_pumpswap_pool(_rpc(script.transport()), MINT)
    assert lookup.pool is not None
    assert lookup.reason == "lookup=getProgramAccounts"


async def test_bad_mint_is_blind() -> None:
    script = _Script(canonical=_pool_value())
    lookup = await lookup_pumpswap_pool(_rpc(script.transport()), "not-a-mint")
    assert lookup.pool is None
    assert "lookup=canonical_pda" in lookup.reason
    assert script.methods == []
