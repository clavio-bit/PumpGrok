"""safety.score_report over RPC fixtures (Phase 3 plan 03-02, task 3).

Pins BLIND-not-exception semantics, config-driven thresholds, the 45s
spec, and the CLEAR/CONDITIONAL/KILL ladder. MockTransport only.
"""

from __future__ import annotations

import base64
import json
import random
import sys
import time
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

import httpx
from pydantic import ValidationError

from pumpgrok.core.bot import BotManifest, ToolGrant
from pumpgrok.core.errors import DataStale
from pumpgrok.core.ids import CorrelationId, IdFactory, bind_correlation
from pumpgrok.core.side_effect import SideEffect
from pumpgrok.replay.clock import FixtureClock
from pumpgrok.tools import build_registry
from pumpgrok.tools.onchain.curve_layout import CURVE_ACCOUNT_SIZE, encode_curve_fixture
from pumpgrok.tools.onchain.rpc import RpcClient
from pumpgrok.tools.safety.checks import CheckResult
from pumpgrok.tools.safety.checks.honeypot import HoneypotProbe
from pumpgrok.tools.safety.score_report import (
    SCORE_SPEC,
    ScoreReportIn,
    _advisory_rows,
    build_score_report_tool,
)

START = 1469918176.385
MINT = "So11111111111111111111111111111111111111112"
CURVE = "8" * 44
CREATOR = "9" * 44
FREEZE = "F" * 44

_NOW = int(time.time())


def _curve_b64() -> str:
    raw = encode_curve_fixture(
        virtual_sol_reserves_lamports=30_000_000_000,
        virtual_token_reserves_units=1_000_000_000_000_000,
        real_sol_reserves_lamports=30_000_000_000,
        real_token_reserves_units=970_000_000_000_000,
        total_supply_units=1_000_000_000_000_000,
        complete=False,
    )
    assert len(raw) == CURVE_ACCOUNT_SIZE
    return base64.b64encode(raw).decode("ascii")


def _handler(*, freeze_live: bool, supply: str, holders_ok: bool):
    curve = _curve_b64()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        method, params = body["method"], body["params"]
        if method == "getAccountInfo":
            if params[1].get("encoding") == "jsonParsed":
                result = {
                    "context": {"slot": 11},
                    "value": {
                        "data": {
                            "parsed": {
                                "info": {
                                    "mintAuthority": None,
                                    "freezeAuthority": FREEZE if freeze_live else None,
                                    "decimals": 6,
                                    "supply": supply,
                                    "isInitialized": True,
                                    "extensions": [],
                                }
                            }
                        }
                    },
                }
            else:
                result = {
                    "context": {"slot": 7},
                    "value": {
                        "data": [curve, "base64"],
                        "executable": False,
                        "lamports": 1,
                        "owner": "o",
                        "rentEpoch": 1,
                    },
                }
        elif method == "getTokenLargestAccounts":
            if not holders_ok:
                return httpx.Response(
                    200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32005}}
                )
            result = {
                "value": [
                    {"address": "A" * 44, "uiAmount": 50.0, "decimals": 6},
                    {"address": "B" * 44, "uiAmount": 30.0, "decimals": 6},
                    {"address": "C" * 44, "uiAmount": 20.0, "decimals": 6},
                ]
            }
        elif method == "getTokenSupply":
            result = {"context": {"slot": 9}, "value": {"amount": supply, "decimals": 6}}
        elif method == "getSignaturesForAddress":
            result = [{"signature": "d1", "slot": 1, "blockTime": _NOW - 100}]
        else:
            raise AssertionError(f"unexpected method {method}")
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    return handler


def _jupiter_pass(request: httpx.Request) -> httpx.Response:
    assert "taker" not in request.url.params
    return httpx.Response(
        200,
        json={
            "inAmount": "1000000",
            "outAmount": "1000",
            "priceImpact": "0.1",
            "routePlan": [{"swapInfo": {"label": "mock-amm"}, "percent": 100}],
        },
    )


def _run_with(handler, payload: ScoreReportIn):
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    client = httpx.AsyncClient(transport=transport)
    rpc = RpcClient(
        ["https://rpc.invalid"], client, FixtureClock(start=START), rng=random.Random(9)
    )
    jupiter = httpx.AsyncClient(transport=httpx.MockTransport(_jupiter_pass))
    reg, _ = build_registry(clock=FixtureClock(start=START), rng=random.Random(11))
    reg.register(
        build_score_report_tool(
            rpc,
            honeypot=HoneypotProbe(client=jupiter, probe_lamports=200_000_000),
        )
    )
    bot = BotManifest(
        id="bot-05-rug",
        display="rug",
        channel="DM",
        purpose="test seat",
        inputs=[],
        outputs=[],
        skills=[],
        allowed_tools=[ToolGrant(name="safety.score_report", side_effect=SideEffect.READ)],
        risk_budget={},
        forbidden=[],
        fail_safe="halt",
        schedule=[],
        escalation=[],
        owned_paths=[],
        receipts_dir="var/receipts",
        prompt_source="test",
    )
    return reg, bot


async def _call(reg, bot, payload: ScoreReportIn) -> object:
    factory = IdFactory(FixtureClock(start=START), random.Random(12))
    with bind_correlation(CorrelationId.new(factory)):
        resolved = reg.find("safety.score_report", max_side_effect=SideEffect.READ, bot=bot)
        return await resolved(payload)


def _clear_in() -> ScoreReportIn:
    return ScoreReportIn(mint=MINT, curve_account=CURVE, creator=CREATOR)


async def test_clear_report_on_clean_data() -> None:
    reg, bot = _run_with(
        _handler(freeze_live=False, supply="1000000000000", holders_ok=True), _clear_in()
    )
    out = await _call(reg, bot, _clear_in())
    assert out.score == 100
    assert out.verdict_recommendation == "CLEAR"
    assert out.hard_veto is False
    assert out.failed_required_checks == []
    assert out.blind_checks == ["bundler_cluster"]
    transfer_tax = next(c for c in out.checks if c.id == "transfer_tax")
    assert transfer_tax.status == "pass"
    assert transfer_tax.value == 0
    honeypot = next(c for c in out.checks if c.id == "honeypot")
    assert honeypot.status == "pass"
    assert out.meta.source == "solana_rpc"
    assert out.meta.freshness_s == 0.0
    assert out.thresholds_used["kill_below_score"] == 40.0
    assert all(isinstance(c.reason, str) and c.reason for c in out.checks)


async def test_required_fails_force_kill_inside_the_conditional_band() -> None:
    reg, bot = _run_with(
        _handler(freeze_live=True, supply="200000000", holders_ok=True), _clear_in()
    )
    out = await _call(reg, bot, _clear_in())
    assert out.score == 50
    assert out.verdict_recommendation == "KILL"
    assert out.hard_veto is False
    statuses = {c.id: c.status for c in out.checks}
    assert statuses["freeze_authority"] == "fail"
    assert statuses["top10_concentration"] == "fail"
    assert out.failed_required_checks == ["freeze_authority", "top10_concentration"]


async def test_kill_on_top5_hard_veto() -> None:
    reg, bot = _run_with(
        _handler(freeze_live=False, supply="100000000", holders_ok=True), _clear_in()
    )
    out = await _call(reg, bot, _clear_in())
    assert out.hard_veto is True
    assert out.verdict_recommendation == "KILL"
    assert "top10_concentration" in out.failed_required_checks


async def test_blind_never_raises_and_forces_kill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = tmp_path / ".planning" / "STATE.md"
    record.parent.mkdir(parents=True)
    record.write_text("paid_rpc_provider: test-provider\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    reg, bot = _run_with(
        _handler(freeze_live=False, supply="1000000000000", holders_ok=False), _clear_in()
    )
    out = await _call(reg, bot, _clear_in())
    statuses = {c.id: c.status for c in out.checks}
    assert statuses["top10_concentration"] == "blind"
    assert "top10_concentration" in out.blind_checks
    assert out.verdict_recommendation == "KILL"


async def test_blind_holder_pair_is_advisory_without_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = tmp_path / ".planning" / "STATE.md"
    record.parent.mkdir(parents=True)
    record.write_text("paid_rpc_provider: none\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    reg, bot = _run_with(
        _handler(freeze_live=False, supply="1000000000000", holders_ok=False), _clear_in()
    )
    out = await _call(reg, bot, _clear_in())
    statuses = {c.id: c.status for c in out.checks}
    assert statuses["top10_concentration"] == "blind"
    assert "top10_concentration" in out.blind_checks
    assert out.failed_required_checks == []
    assert out.verdict_recommendation == "CLEAR"


async def test_unknown_check_and_threshold_ids_are_refused() -> None:
    reg, bot = _run_with(
        _handler(freeze_live=False, supply="1000000000000", holders_ok=True), _clear_in()
    )
    with pytest.raises(DataStale):
        await _call(reg, bot, ScoreReportIn(mint=MINT, curve_account=CURVE, checks=["nope"]))
    with pytest.raises(DataStale):
        await _call(
            reg, bot, ScoreReportIn(mint=MINT, curve_account=CURVE, thresholds={"nope": 1.0})
        )


async def test_threshold_override_moves_verdict() -> None:
    reg, bot = _run_with(
        _handler(freeze_live=False, supply="450000000", holders_ok=True), _clear_in()
    )
    base = await _call(reg, bot, _clear_in())
    assert base.score == 90
    assert base.verdict_recommendation == "CLEAR"
    assert base.failed_required_checks == []
    statuses = {c.id: c.status for c in base.checks}
    assert statuses["top10_concentration"] == "warn"
    over = await _call(
        reg,
        bot,
        ScoreReportIn(
            mint=MINT, curve_account=CURVE, creator=CREATOR, thresholds={"kill_below_score": 95.0}
        ),
    )
    assert over.score == base.score
    assert over.verdict_recommendation == "KILL"
    assert over.failed_required_checks == []
    assert over.hard_veto is False


async def test_score_ladder_still_returns_conditional() -> None:
    reg, bot = _run_with(
        _handler(freeze_live=False, supply="450000000", holders_ok=True), _clear_in()
    )
    out = await _call(
        reg,
        bot,
        ScoreReportIn(
            mint=MINT,
            curve_account=CURVE,
            creator=CREATOR,
            thresholds={"conditional_below_score": 95.0},
        ),
    )
    statuses = {c.id: c.status for c in out.checks}
    assert statuses["top10_concentration"] == "warn"
    assert out.score == 90
    assert out.failed_required_checks == []
    assert out.hard_veto is False
    assert out.verdict_recommendation == "CONDITIONAL"


async def test_checks_subset_limits_runnable() -> None:
    reg, bot = _run_with(
        _handler(freeze_live=False, supply="1000000000000", holders_ok=True), _clear_in()
    )
    out = await _call(
        reg,
        bot,
        ScoreReportIn(mint=MINT, curve_account=CURVE, creator=CREATOR, checks=["mint_authority"]),
    )
    assert [c.id for c in out.checks] == ["mint_authority"]


def test_score_spec_contract() -> None:
    assert SCORE_SPEC.name == "safety.score_report"
    assert SCORE_SPEC.timeout_s == 45.0
    assert SCORE_SPEC.retry.attempts == 1
    assert SCORE_SPEC.rate_limit_key == "solana_rpc"


def test_score_input_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ScoreReportIn(mint=MINT, curve_account=CURVE, venue_noise=1)  # type: ignore[call-arg]


async def test_required_fail_forces_kill_above_the_clear_line() -> None:
    reg, bot = _run_with(
        _handler(freeze_live=True, supply="1000000000000", holders_ok=True), _clear_in()
    )
    out = await _call(reg, bot, _clear_in())
    statuses = {c.id: c.status for c in out.checks}
    assert statuses["freeze_authority"] == "fail"
    assert out.score == 75
    assert out.score >= out.thresholds_used["conditional_below_score"]
    assert out.verdict_recommendation == "KILL"
    assert out.hard_veto is False
    assert out.failed_required_checks == ["freeze_authority"]


async def test_required_warn_does_not_force_kill() -> None:
    reg, bot = _run_with(
        _handler(freeze_live=False, supply="450000000", holders_ok=True), _clear_in()
    )
    out = await _call(reg, bot, _clear_in())
    statuses = {c.id: c.status for c in out.checks}
    assert statuses["top10_concentration"] == "warn"
    assert out.score == 90
    assert out.verdict_recommendation == "CLEAR"
    assert out.failed_required_checks == []
    assert out.hard_veto is False


async def test_top10_fail_forces_kill_and_blind_carve_out_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = tmp_path / ".planning" / "STATE.md"
    record.parent.mkdir(parents=True)
    record.write_text("paid_rpc_provider: none\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    reg, bot = _run_with(
        _handler(freeze_live=False, supply="200000000", holders_ok=True), _clear_in()
    )
    failed = await _call(reg, bot, _clear_in())
    statuses = {c.id: c.status for c in failed.checks}
    assert statuses["top10_concentration"] == "fail"
    assert failed.score == 75
    assert failed.verdict_recommendation == "KILL"
    assert failed.hard_veto is False
    assert failed.failed_required_checks == ["top10_concentration"]
    assert "top10_concentration" not in failed.advisory_blind_checks

    blind_reg, blind_bot = _run_with(
        _handler(freeze_live=False, supply="1000000000000", holders_ok=False), _clear_in()
    )
    blind = await _call(blind_reg, blind_bot, _clear_in())
    blind_status = {c.id: c.status for c in blind.checks}
    assert blind_status["top10_concentration"] == "blind"
    assert "top10_concentration" in blind.advisory_blind_checks
    assert blind.failed_required_checks == []
    assert blind.verdict_recommendation == "CLEAR"


async def test_advisory_fail_does_not_force_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    def _failing_advisory(
        wanted: set[str], evidence: list[object], clock: object
    ) -> list[CheckResult]:
        rows = _advisory_rows(wanted, evidence, clock)  # type: ignore[arg-type]
        return [
            CheckResult(
                id=row.id,
                status="fail",
                value=True,
                threshold=None,
                weight=Decimal(0),
                reason="synthetic advisory fail",
            )
            for row in rows
        ]

    monkeypatch.setattr("pumpgrok.tools.safety.score_report._advisory_rows", _failing_advisory)
    reg, bot = _run_with(
        _handler(freeze_live=False, supply="1000000000000", holders_ok=True), _clear_in()
    )
    out = await _call(reg, bot, _clear_in())
    statuses = {c.id: c.status for c in out.checks}
    assert statuses["bundler_cluster"] == "fail"
    assert out.score == 75
    assert out.verdict_recommendation == "CLEAR"
    assert out.failed_required_checks == []
    assert out.hard_veto is False


async def test_liquidity_fail_with_advisory_blinds_forces_kill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = tmp_path / ".planning" / "STATE.md"
    record.parent.mkdir(parents=True)
    record.write_text("paid_rpc_provider: none\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    payload = ScoreReportIn(mint=MINT, curve_account=CURVE, intended_size_lamports=4_000_000_000)
    reg, bot = _run_with(
        _handler(freeze_live=False, supply="1000000000000", holders_ok=False), payload
    )
    out = await _call(reg, bot, payload)
    statuses = {c.id: c.status for c in out.checks}
    assert statuses["liquidity_depth"] == "fail"
    assert statuses["mint_authority"] == "pass"
    assert statuses["freeze_authority"] == "pass"
    assert statuses["lp_status"] == "pass"
    assert statuses["transfer_tax"] == "pass"
    assert statuses["honeypot"] == "pass"
    assert statuses["token2022_extensions"] == "pass"
    assert statuses["top10_concentration"] == "blind"
    assert statuses["deployer_history"] == "blind"
    assert statuses["bundler_cluster"] == "blind"
    assert out.score == 75
    assert out.verdict_recommendation == "KILL"
    assert out.hard_veto is False
    assert out.failed_required_checks == ["liquidity_depth"]
    assert out.advisory_blind_checks == ["top10_concentration", "deployer_history"]
