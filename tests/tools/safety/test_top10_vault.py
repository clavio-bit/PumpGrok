"""top10_concentration excludes the PumpSwap base vault and the curve account.

Fixtures only. A graduated mint drops the base vault (and any holder owned by
the pool). An open curve with no pool drops the curve account. A failed pool
lookup is BLIND, never a fail and never a pass. A remaining whale still fails,
and that fail still forces KILL.
"""

from __future__ import annotations

import base64
import json
import random
from pathlib import Path

import httpx
import pytest

from pumpgrok.config.settings import SafetyThresholds
from pumpgrok.replay.clock import FixtureClock
from pumpgrok.tools.onchain.curve_layout import encode_curve_fixture
from pumpgrok.tools.onchain.holder_exclusions import run_concentration
from pumpgrok.tools.onchain.pumpswap_layout import PUMPSWAP_PROGRAM
from pumpgrok.tools.onchain.pumpswap_pda import canonical_pool_address
from pumpgrok.tools.onchain.rpc import RpcClient
from pumpgrok.tools.safety.checks.readers import check_top10_concentration
from pumpgrok.tools.safety.score_report import ScoreReportIn, run_score_report

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = json.loads((ROOT / "fixtures/rpc/pumpswap/togo_pool.json").read_text(encoding="utf-8"))
START = 1469918176.385
TOGO = FIXTURE["mint"]
POOL = FIXTURE["address"]
VAULT = FIXTURE["base_vault"]
QUOTE = FIXTURE["quote_vault"]
CURVE = "8" * 44
WALLET = "W" * 44
ATA = "A" * 44
DUST = "D" * 44
SUPPLY = "1000000"
_BLIND = "cannot tell whether the dominant holder is the PumpSwap pool"


def _open_curve() -> str:
    raw = encode_curve_fixture(
        virtual_sol_reserves_lamports=30_000_000_000,
        virtual_token_reserves_units=1_000_000_000_000_000,
        real_sol_reserves_lamports=1_000_000_000,
        real_token_reserves_units=800_000_000_000_000,
        total_supply_units=1_000_000_000_000_000,
        complete=False,
    )
    return base64.b64encode(raw).decode("ascii")


def _complete_curve() -> str:
    raw = encode_curve_fixture(
        virtual_sol_reserves_lamports=30_000_000_000,
        virtual_token_reserves_units=1_000_000_000_000_000,
        real_sol_reserves_lamports=85_000_000_000,
        real_token_reserves_units=1_000_000_000_000,
        total_supply_units=1_000_000_000_000_000,
        complete=True,
    )
    return base64.b64encode(raw).decode("ascii")


def _ok(result: object) -> httpx.Response:
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})


def _err() -> httpx.Response:
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32005}})


def _holder(address: str, amount: int) -> dict[str, object]:
    return {"address": address, "uiAmount": amount, "decimals": 0}


def _rpc(handler: httpx.MockTransport | object) -> RpcClient:
    client = httpx.AsyncClient(transport=handler)  # type: ignore[arg-type]
    return RpcClient(["https://rpc.invalid"], client, FixtureClock(start=START), random.Random(1))


def _handler(
    *,
    mint: str,
    curve_data: str | None,
    pool_data: str | None,
    owners: dict[str, str],
    holders: list[dict[str, object]],
    canonical_error: bool = False,
    scan_error: bool = False,
    holders_error: bool = False,
) -> httpx.MockTransport:
    pool_address = canonical_pool_address(mint)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        method, params = body["method"], body["params"]
        if method == "getTokenLargestAccounts":
            if holders_error:
                return _err()
            return _ok({"value": holders})
        if method == "getTokenSupply":
            return _ok({"context": {"slot": 9}, "value": {"amount": SUPPLY, "decimals": 0}})
        if method == "getAccountInfo":
            return _account(
                params,
                curve_data=curve_data,
                pool_address=pool_address,
                pool_data=pool_data,
                owners=owners,
                canonical_error=canonical_error,
            )
        if method in {"getProgramAccounts", "getProgramAccountsV2"}:
            if scan_error:
                return _err()
            raise AssertionError(method)
        raise AssertionError(method)

    return httpx.MockTransport(handler)


def _account(
    params: list[object],
    *,
    curve_data: str | None,
    pool_address: str,
    pool_data: str | None,
    owners: dict[str, str],
    canonical_error: bool,
) -> httpx.Response:
    address = params[0]
    options = params[1] if isinstance(params[1], dict) else {}
    if options.get("encoding") == "jsonParsed":
        owner = owners.get(str(address))
        if owner is None:
            return _ok({"context": {"slot": 1}, "value": None})
        info = {"parsed": {"info": {"owner": owner}}}
        return _ok({"context": {"slot": 4}, "value": {"data": info}})
    if address == CURVE:
        if curve_data is None:
            return _ok({"context": {"slot": 1}, "value": None})
        return _ok(
            {
                "context": {"slot": 2},
                "value": {"owner": "pump", "lamports": 1, "data": [curve_data, "base64"]},
            }
        )
    if address == pool_address:
        if canonical_error:
            return _err()
        if pool_data is None:
            return _ok({"context": {"slot": 1}, "value": None})
        return _ok(
            {
                "context": {"slot": FIXTURE["slot"]},
                "value": {
                    "owner": PUMPSWAP_PROGRAM,
                    "lamports": 1,
                    "data": [pool_data, "base64"],
                    "executable": False,
                },
            }
        )
    return _ok({"context": {"slot": 1}, "value": None})


def _row(top: dict[str, object]) -> object:
    return check_top10_concentration(top, SafetyThresholds())


def _graduated_holders() -> list[dict[str, object]]:
    return [
        _holder(VAULT, 990_000),
        _holder(POOL, 10_000),
        _holder(DUST, 1_000),
        _holder(WALLET, 4_000),
    ]


def _graduated_owners() -> dict[str, str]:
    return {VAULT: POOL, POOL: POOL, DUST: POOL, WALLET: WALLET}


async def _concentration(
    *,
    mint: str,
    curve_data: str | None,
    pool_data: str | None,
    owners: dict[str, str],
    holders: list[dict[str, object]],
    canonical_error: bool = False,
    scan_error: bool = False,
    holders_error: bool = False,
) -> dict[str, object]:
    rpc = _rpc(
        _handler(
            mint=mint,
            curve_data=curve_data,
            pool_data=pool_data,
            owners=owners,
            holders=holders,
            canonical_error=canonical_error,
            scan_error=scan_error,
            holders_error=holders_error,
        )
    )
    return await run_concentration(mint, CURVE, rpc)


def test_canonical_pool_matches_the_togo_fixture() -> None:
    assert canonical_pool_address(TOGO) == POOL


def test_blind_reason_beats_a_high_share() -> None:
    row = _row(
        {
            "top10_share": "0.99",
            "blind_reason": f"{_BLIND}; rpc down; concentration not scored",
        }
    )
    assert row.status == "blind"
    assert row.value is None
    assert "not scored" in row.reason


def test_reason_without_exclusions_stays_the_percentage() -> None:
    row = _row({"top10_share": "0.10"})
    assert row.status == "pass"
    assert row.reason == "top-10 holders own 10.00% of supply"


async def test_graduated_vault_is_excluded_when_the_curve_is_unreadable() -> None:
    top = await _concentration(
        mint=TOGO,
        curve_data=None,
        pool_data=FIXTURE["data_base64"],
        owners=_graduated_owners(),
        holders=_graduated_holders(),
    )
    row = _row(top)
    assert row.status == "pass"
    assert row.value == pytest.approx(0.4)
    assert VAULT in row.reason
    assert "pumpswap base vault" in row.reason
    assert "owner is the pumpswap pool" in row.reason
    assert "pumpswap pool account" in row.reason
    assert f"quote_vault={QUOTE}" in row.reason
    assert "lookup=canonical_pda" in row.reason
    assert QUOTE not in top["excluded"]
    assert set(top["excluded"]) == {VAULT, POOL, DUST}


async def test_vault_is_excluded_when_the_curve_still_decodes_open() -> None:
    top = await _concentration(
        mint=TOGO,
        curve_data=_open_curve(),
        pool_data=FIXTURE["data_base64"],
        owners=_graduated_owners(),
        holders=_graduated_holders(),
    )
    row = _row(top)
    assert row.status == "pass"
    assert row.value == pytest.approx(0.4)
    assert VAULT in set(top["excluded"])


async def test_open_curve_excludes_the_curve_account_and_its_owner() -> None:
    top = await _concentration(
        mint="So11111111111111111111111111111111111111112",
        curve_data=_open_curve(),
        pool_data=None,
        owners={ATA: CURVE, CURVE: WALLET, WALLET: WALLET},
        holders=[_holder(ATA, 600_000), _holder(CURVE, 300_000), _holder(WALLET, 20_000)],
    )
    row = _row(top)
    assert row.status == "pass"
    assert row.value == pytest.approx(2.0)
    assert set(top["excluded"]) == {ATA, CURVE}
    assert "bonding-curve account" in row.reason
    assert "pool=" not in row.reason


async def test_pool_lookup_failure_is_blind_and_not_a_fail() -> None:
    top = await _concentration(
        mint=TOGO,
        curve_data=_complete_curve(),
        pool_data=None,
        owners={},
        holders=[_holder(VAULT, 990_000)],
        scan_error=True,
    )
    row = _row(top)
    assert row.status == "blind"
    assert row.value is None
    assert row.status != "pass"
    assert _BLIND in row.reason
    assert "concentration not scored" in row.reason
    assert top["top10_share"] == 0


async def test_canonical_read_failure_on_an_open_curve_is_blind() -> None:
    top = await _concentration(
        mint=TOGO,
        curve_data=_open_curve(),
        pool_data=FIXTURE["data_base64"],
        owners={},
        holders=[_holder(VAULT, 990_000)],
        canonical_error=True,
    )
    row = _row(top)
    assert row.status == "blind"
    assert "unreadable" in row.reason
    assert top["top5_share"] == 0


async def test_known_pool_with_unreadable_holders_names_the_vault_and_stays_blind() -> None:
    top = await _concentration(
        mint=TOGO,
        curve_data=None,
        pool_data=FIXTURE["data_base64"],
        owners={},
        holders=[_holder(VAULT, 990_000)],
        holders_error=True,
    )
    row = _row(top)
    assert row.status == "blind"
    assert row.value is None
    assert VAULT in row.reason
    assert QUOTE in row.reason
    assert "holder sample unreadable" in row.reason
    assert "concentration not scored" in row.reason
    assert top["top10_share"] == 0


async def test_whale_still_fails_and_forces_kill() -> None:
    holders = [_holder(VAULT, 500_000), _holder(DUST, 1_000), _holder(WALLET, 300_000)]
    owners = {VAULT: POOL, DUST: POOL, WALLET: WALLET}
    rpc = _rpc(
        _handler(
            mint=TOGO,
            curve_data=None,
            pool_data=FIXTURE["data_base64"],
            owners=owners,
            holders=holders,
        )
    )
    out = await run_score_report(
        ScoreReportIn(mint=TOGO, curve_account=CURVE, checks=["top10_concentration"]),
        rpc,
    )
    row = next(item for item in out["checks"] if item["id"] == "top10_concentration")
    assert row["status"] == "fail"
    assert row["value"] == pytest.approx(30.0)
    assert VAULT in row["reason"]
    assert "pumpswap base vault" in row["reason"]
    assert out["score"] == 75
    assert out["verdict_recommendation"] == "KILL"
    assert out["hard_veto"] is False
    assert out["failed_required_checks"] == ["top10_concentration"]


async def test_lookup_failure_stays_advisory_without_a_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = tmp_path / ".planning" / "STATE.md"
    record.parent.mkdir(parents=True)
    record.write_text("paid_rpc_provider: none\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    rpc = _rpc(
        _handler(
            mint=TOGO,
            curve_data=_complete_curve(),
            pool_data=None,
            owners={},
            holders=[_holder(VAULT, 990_000)],
            scan_error=True,
        )
    )
    out = await run_score_report(
        ScoreReportIn(mint=TOGO, curve_account=CURVE, checks=["top10_concentration"]),
        rpc,
    )
    row = out["checks"][0]
    assert row["status"] == "blind"
    assert row["value"] is None
    assert out["failed_required_checks"] == []
    assert out["advisory_blind_checks"] == ["top10_concentration"]
    assert out["verdict_recommendation"] == "CLEAR"


async def test_lookup_failure_blind_still_kills_when_a_provider_is_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = tmp_path / ".planning" / "STATE.md"
    record.parent.mkdir(parents=True)
    record.write_text("paid_rpc_provider: test-provider\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    rpc = _rpc(
        _handler(
            mint=TOGO,
            curve_data=_complete_curve(),
            pool_data=None,
            owners={},
            holders=[_holder(VAULT, 990_000)],
            scan_error=True,
        )
    )
    out = await run_score_report(
        ScoreReportIn(mint=TOGO, curve_account=CURVE, checks=["top10_concentration"]),
        rpc,
    )
    assert out["checks"][0]["status"] == "blind"
    assert out["failed_required_checks"] == []
    assert "top10_concentration" in out["blind_checks"]
    assert out["advisory_blind_checks"] == []
    assert out["verdict_recommendation"] == "KILL"
    assert out["hard_veto"] is False
