"""Paper verdict caps from the operator risk-limits note."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from pumpgrok.tools.safety.checks import CheckResult
from pumpgrok.tools.safety.score_report import _apply_paper_caps


def _row(check_id: str, status: str) -> CheckResult:
    return CheckResult(
        id=check_id,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        weight=Decimal("0.1"),
        reason=f"{check_id} {status}",
    )


def _clean(
    bundler: str = "blind", deployer: str = "pass", extra: str = "pass"
) -> list[CheckResult]:
    return [
        _row("honeypot", extra),
        _row("lp_status", extra),
        _row("liquidity_depth", extra),
        _row("bundler_cluster", bundler),
        _row("deployer_history", deployer),
    ]


def test_bundler_blind_with_live_passes_is_conditional() -> None:
    assert _apply_paper_caps("CLEAR", _clean()) == "CONDITIONAL"


def test_bundler_blind_without_the_live_passes_is_kill() -> None:
    assert _apply_paper_caps("CLEAR", _clean(extra="blind")) == "KILL"
    assert _apply_paper_caps("CONDITIONAL", _clean(extra="fail")) == "KILL"


def test_deployer_warn_blocks_clear() -> None:
    rows = _clean(bundler="pass", deployer="warn")
    assert _apply_paper_caps("CLEAR", rows) == "CONDITIONAL"
    assert _apply_paper_caps("KILL", rows) == "KILL"


def test_paid_provider_record_names_helius() -> None:
    text = Path(".planning/STATE.md").read_text(encoding="utf-8")
    assert "paid_rpc_provider: helius" in text
