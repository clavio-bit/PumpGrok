"""Deployer history paginates and flags a budget hit instead of a silent 100."""

from __future__ import annotations

import json

import httpx

from pumpgrok.replay.clock import FixtureClock
from pumpgrok.tools.onchain.deployer_history import (
    _MAX_SIGNATURES,
    _PAGE_LIMIT,
    DeployerHistoryIn,
    run_deployer_history,
)
from pumpgrok.tools.onchain.rpc import RpcClient

CREATOR = "H3D1X1mushAjMYTSHp447hgqkfPCLrRKAzUMEg3CmmUs"
START = 1_700_000_000.0


def _client(pages: dict[str | None, list[dict[str, object]]]) -> RpcClient:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert body["method"] == "getSignaturesForAddress"
        options = body["params"][1]
        assert options["limit"] == _PAGE_LIMIT
        before = options.get("before")
        result = pages[before]
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    return RpcClient(
        ["https://rpc.invalid"],
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        FixtureClock(start=START),
        rng=__import__("random").Random(1),
    )


def _row(signature: str, age: int = 10) -> dict[str, object]:
    return {"signature": signature, "blockTime": int(START) - age}


async def test_two_short_pages_are_a_complete_count() -> None:
    first = [_row(f"s{i}") for i in range(3)]
    # A short page ends the scan. Build one page under the limit.
    out = await run_deployer_history(DeployerHistoryIn(creator=CREATOR), _client({None: first}))
    assert out["tokens_seen"] == 3
    assert out["truncated"] is False


async def test_full_pages_until_the_budget_are_at_least_n() -> None:
    pages: dict[str | None, list[dict[str, object]]] = {}
    cursor: str | None = None
    # Five full pages, which is the budget, all inside the lookback.
    for page in range(5):
        rows = [_row(f"p{page}-{i}") for i in range(_PAGE_LIMIT)]
        pages[cursor] = rows
        cursor = str(rows[-1]["signature"])
    out = await run_deployer_history(DeployerHistoryIn(creator=CREATOR), _client(pages))
    assert out["truncated"] is True
    assert out["tokens_seen"] == _MAX_SIGNATURES
    assert out["tokens_seen"] != 100


async def test_signatures_older_than_the_window_stop_the_scan() -> None:
    rows = [_row("new", 10), _row("old", 40 * 86_400)]
    out = await run_deployer_history(
        DeployerHistoryIn(creator=CREATOR, lookback_days=30), _client({None: rows})
    )
    assert out["tokens_seen"] == 1
    assert out["truncated"] is False
    assert out["recent_signatures"] == ["new"]
