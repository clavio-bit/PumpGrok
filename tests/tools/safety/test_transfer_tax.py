"""transfer_tax reads the mint and is required (amendment 2026-11-13).

MockTransport only. Classic SPL and Token-2022 without a fee pass at 0.
A decoded TransferFeeConfig is scored against the warn and hard-fail percents.
An unreadable mint or an undecodable extension is BLIND.
"""

from __future__ import annotations

import json
import random
from collections.abc import Callable
from typing import Any

import httpx

from pumpgrok.config.settings import SafetyThresholds
from pumpgrok.replay.clock import FixtureClock
from pumpgrok.tools.onchain.rpc import RpcClient
from pumpgrok.tools.safety.checks.transfer_tax import (
    SPL_TOKEN_PROGRAM,
    TOKEN_2022_PROGRAM,
    run_transfer_tax_check,
)
from pumpgrok.tools.safety.score_report import ScoreReportIn, run_score_report

START = 1469918176.385
MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
CURVE = "8" * 44
EPOCH = 600


def _fee(epoch: int, bps: int, maximum: int) -> dict[str, object]:
    return {
        "epoch": str(epoch),
        "maximumFee": str(maximum),
        "transferFeeBasisPoints": bps,
    }


def _fee_extension(*, older_bps: int, newer_bps: int, newer_epoch: int) -> dict[str, object]:
    return {
        "extension": "transferFeeConfig",
        "state": {
            "olderTransferFee": _fee(0, older_bps, 1_000),
            "newerTransferFee": _fee(newer_epoch, newer_bps, 9_000),
            "withheldAmount": "0",
        },
    }


def _account(
    *,
    owner: str,
    program: str,
    extensions: list[dict[str, object]] | object,
    parsed_type: str | None = "mint",
    slot: int = 42,
) -> dict[str, object]:
    info: dict[str, object] = {
        "decimals": 6,
        "supply": "1",
        "isInitialized": True,
        "mintAuthority": None,
        "freezeAuthority": None,
        "extensions": extensions,
    }
    parsed: dict[str, object] = {"info": info}
    if parsed_type is not None:
        parsed["type"] = parsed_type
    return {
        "context": {"slot": slot},
        "value": {
            "owner": owner,
            "lamports": 1,
            "data": {"program": program, "parsed": parsed},
        },
    }


def _rpc(
    account: dict[str, object] | None,
    *,
    epoch: int | None = EPOCH,
    account_error: bool = False,
    epoch_error: bool = False,
) -> RpcClient:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        method = body["method"]
        if method == "getAccountInfo":
            if account_error:
                return httpx.Response(
                    200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32005}}
                )
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": account})
        if method == "getEpochInfo":
            if epoch_error:
                return httpx.Response(
                    200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32005}}
                )
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 1, "result": {"epoch": epoch, "slotIndex": 1}},
            )
        raise AssertionError(f"unexpected method {method}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return RpcClient(["https://rpc.invalid"], client, FixtureClock(start=START), random.Random(1))


async def _run(rpc: RpcClient, thresholds: SafetyThresholds | None = None) -> dict[str, Any]:
    outcome = await run_transfer_tax_check(
        mint=MINT,
        rpc=rpc,
        thresholds=thresholds or SafetyThresholds(),
    )
    return {
        "status": outcome.result.status,
        "value": outcome.result.value,
        "reason": outcome.result.reason,
        "slot": outcome.slot,
        "source": outcome.source,
    }


async def test_classic_spl_mint_passes_with_zero_fee() -> None:
    account = _account(owner=SPL_TOKEN_PROGRAM, program="spl-token", extensions=[])
    out = await _run(_rpc(account))
    assert out["status"] == "pass"
    assert out["value"] == 0
    assert out["reason"] == "classic SPL mint has no transfer fee; effective bps 0"
    assert out["slot"] == 42
    assert out["source"] == "solana_rpc"


async def test_token2022_without_fee_extension_passes_with_zero() -> None:
    account = _account(
        owner=TOKEN_2022_PROGRAM,
        program="spl-token-2022",
        extensions=[{"extension": "metadataPointer", "state": {"authority": None}}],
    )
    out = await _run(_rpc(account))
    assert out["status"] == "pass"
    assert out["value"] == 0
    assert "no TransferFeeConfig" in out["reason"]
    assert out["slot"] == 42


async def test_one_percent_fee_passes_and_records_both_schedules() -> None:
    account = _account(
        owner=TOKEN_2022_PROGRAM,
        program="spl-token-2022",
        extensions=[_fee_extension(older_bps=0, newer_bps=100, newer_epoch=100)],
    )
    out = await _run(_rpc(account, epoch=EPOCH))
    assert out["status"] == "pass"
    assert out["value"] == 1
    reason = out["reason"]
    assert "effective_bps=100" in reason
    assert "active=newer" in reason
    assert "older_bps=0" in reason
    assert "older_maximum_fee=1000" in reason
    assert "newer_bps=100" in reason
    assert "newer_maximum_fee=9000" in reason
    assert f"epoch={EPOCH}" in reason


async def test_newer_fee_is_ignored_until_its_epoch() -> None:
    account = _account(
        owner=TOKEN_2022_PROGRAM,
        program="spl-token-2022",
        extensions=[_fee_extension(older_bps=100, newer_bps=1500, newer_epoch=EPOCH + 5)],
    )
    out = await _run(_rpc(account, epoch=EPOCH))
    assert out["status"] == "pass"
    assert out["value"] == 1
    assert "active=older" in out["reason"]
    assert "effective_bps=100" in out["reason"]
    assert "newer_bps=1500" in out["reason"]


async def test_fifteen_percent_fee_fails() -> None:
    account = _account(
        owner=TOKEN_2022_PROGRAM,
        program="spl-token-2022",
        extensions=[_fee_extension(older_bps=100, newer_bps=1500, newer_epoch=1)],
    )
    out = await _run(_rpc(account, epoch=EPOCH))
    assert out["status"] == "fail"
    assert out["value"] == 15
    assert "effective_bps=1500" in out["reason"]
    assert "older_maximum_fee=1000" in out["reason"]
    assert "newer_maximum_fee=9000" in out["reason"]


async def test_warn_and_hard_fail_boundaries() -> None:
    warn = _account(
        owner=TOKEN_2022_PROGRAM,
        program="spl-token-2022",
        extensions=[_fee_extension(older_bps=0, newer_bps=500, newer_epoch=1)],
    )
    hard = _account(
        owner=TOKEN_2022_PROGRAM,
        program="spl-token-2022",
        extensions=[_fee_extension(older_bps=0, newer_bps=1000, newer_epoch=1)],
    )
    warned = await _run(_rpc(warn))
    failed = await _run(_rpc(hard))
    assert warned["status"] == "warn"
    assert warned["value"] == 5
    assert failed["status"] == "fail"
    assert failed["value"] == 10


async def test_undecodable_extension_is_blind() -> None:
    account = _account(
        owner=TOKEN_2022_PROGRAM,
        program="spl-token-2022",
        extensions=[{"extension": "transferFeeConfig", "state": {"newerTransferFee": "nope"}}],
    )
    out = await _run(_rpc(account))
    assert out["status"] == "blind"
    assert out["value"] is None
    assert "undecodable" in out["reason"]


async def test_extensions_that_are_not_a_list_are_blind() -> None:
    account = _account(
        owner=TOKEN_2022_PROGRAM,
        program="spl-token-2022",
        extensions="transferFeeConfig",  # type: ignore[arg-type]
    )
    out = await _run(_rpc(account))
    assert out["status"] == "blind"


async def test_missing_mint_is_blind() -> None:
    out = await _run(_rpc({"context": {"slot": 7}, "value": None}))
    assert out["status"] == "blind"
    assert "unreadable" in out["reason"]


async def test_non_mint_account_is_blind() -> None:
    account = _account(
        owner=SPL_TOKEN_PROGRAM,
        program="spl-token",
        extensions=[],
        parsed_type="account",
    )
    out = await _run(_rpc(account))
    assert out["status"] == "blind"


async def test_rpc_error_is_blind() -> None:
    out = await _run(_rpc(None, account_error=True))
    assert out["status"] == "blind"


async def test_epoch_unreadable_is_blind() -> None:
    account = _account(
        owner=TOKEN_2022_PROGRAM,
        program="spl-token-2022",
        extensions=[_fee_extension(older_bps=0, newer_bps=100, newer_epoch=1)],
    )
    out = await _run(_rpc(account, epoch_error=True))
    assert out["status"] == "blind"
    assert out["slot"] == 42


def _score_rpc(handler: Callable[[httpx.Request], httpx.Response]) -> RpcClient:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return RpcClient(["https://rpc.invalid"], client, FixtureClock(start=START), random.Random(2))


async def test_score_report_transfer_tax_blind_kills() -> None:
    out = await run_score_report(
        ScoreReportIn(mint=MINT, curve_account=CURVE, checks=["transfer_tax"]),
        _score_rpc(
            lambda _request: httpx.Response(
                200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32005}}
            )
        ),
    )
    assert out["verdict_recommendation"] == "KILL"
    assert out["blind_checks"] == ["transfer_tax"]
    assert out["checks"][0]["status"] == "blind"


async def test_score_report_records_transfer_tax_slot() -> None:
    account = _account(owner=SPL_TOKEN_PROGRAM, program="spl-token", extensions=[], slot=99)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": account})

    out = await run_score_report(
        ScoreReportIn(mint=MINT, curve_account=CURVE, checks=["transfer_tax"]),
        _score_rpc(handler),
    )
    assert out["verdict_recommendation"] == "CLEAR"
    assert out["checks"][0]["status"] == "pass"
    assert out["checks"][0]["value"] == 0
    assert out["evidence"][0]["slot"] == 99
    assert out["evidence"][0]["source"] == "solana_rpc"
