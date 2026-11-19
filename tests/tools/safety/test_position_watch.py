"""Paper position watch emits a ledger signal and never a live send."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx

from pumpgrok.ledger.store import LedgerStore
from pumpgrok.replay.clock import FixtureClock
from pumpgrok.tools.onchain.curve_layout import encode_curve_fixture
from pumpgrok.tools.onchain.rpc import RpcClient
from pumpgrok.tools.safety.position_watch import (
    WatchSnapshot,
    append_paper_watch,
    watch_open_position,
)

MINT = "G9FFfstBYuh2N6Us5iybQQGDQXG8m9D7grYoMqefpump"
CURVE = "Eid76LG4gKdDfYmtEywjKPvVQD34zq3WU64UrmWuPj1b"
CREATOR = "H3D1X1mushAjMYTSHp447hgqkfPCLrRKAzUMEg3CmmUs"
START = 1_700_000_000.0


def _curve() -> str:
    raw = encode_curve_fixture(
        virtual_sol_reserves_lamports=30_000_000_000,
        virtual_token_reserves_units=1_000_000_000_000_000,
        real_sol_reserves_lamports=1_000_000_000,
        real_token_reserves_units=800_000_000_000_000,
        total_supply_units=1_000_000_000_000_000,
        complete=False,
    )
    return base64.b64encode(raw).decode("ascii")


def _rpc() -> RpcClient:
    curve = _curve()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        method = body["method"]
        if method == "getAccountInfo":
            account = body["params"][0]
            if account == CURVE:
                result = {
                    "value": {
                        "data": [curve, "base64"],
                        "owner": "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
                    }
                }
            elif account == MINT:
                result = {
                    "value": {
                        "data": {
                            "parsed": {
                                "info": {
                                    "mintAuthority": "Auth1111111111111111111111111111111111111",
                                    "freezeAuthority": None,
                                    "decimals": 6,
                                    "supply": "1",
                                    "isInitialized": True,
                                    "extensions": [],
                                }
                            }
                        }
                    }
                }
            else:
                result = {"value": None}
        elif method == "getTokenAccountsByOwner":
            result = {"value": []}
        elif method == "getSignaturesForAddress":
            result = []
        elif method == "getTokenLargestAccounts":
            result = {"value": []}
        else:
            result = {"value": None}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    return RpcClient(
        ["https://rpc.invalid"],
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        FixtureClock(start=START),
        rng=__import__("random").Random(1),
    )


def test_module_does_not_name_a_live_send() -> None:
    text = Path("src/pumpgrok/tools/safety/position_watch.py").read_text(encoding="utf-8")
    for token in ("sendTransaction", "signTransaction", "LiveAdapter", "requestAirdrop"):
        assert token not in text


async def test_authority_and_liquidity_drop_emit_a_paper_alert(tmp_path: Path) -> None:
    report = await watch_open_position(
        mint=MINT,
        curve_account=CURVE,
        rpc=_rpc(),
        creator=CREATOR,
        snapshot=WatchSnapshot(real_sol_lamports=10_000_000_000, creator_token_units=0),
    )
    kinds = {trigger.kind for trigger in report.triggers}
    assert "mint_authority" in kinds
    assert "liquidity_drop" in kinds
    assert report.signal is not None
    assert report.signal.reason == "rug_alert"
    store = LedgerStore(str(tmp_path / "ledger.sqlite"))
    store.set_meta("source", "paper")
    row = append_paper_watch(store, FixtureClock(start=START), report)
    assert row is not None
    assert row["type"] == "Escalation"
    body = json.loads(row["payload_json"])
    assert body["kind"] == "rug_alert"
    assert row["source"] == "paper"
    store.close()
