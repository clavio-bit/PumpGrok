"""Captured mainnet bonding-curve accounts decode under the verified IDL layout."""

from __future__ import annotations

import base64
import json
from pathlib import Path

from pumpgrok.tools.onchain.curve_accounts import curve_token_accounts
from pumpgrok.tools.onchain.curve_layout import decode_curve_account
from pumpgrok.tools.onchain.holder_exclusions import exclusion_reason

FIXTURES = Path("tests/fixtures/rpc/bonding_curve")


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _state(record: dict):
    return decode_curve_account(base64.b64decode(record["account_b64"]))


def test_open_sol_curve_matches_the_capture_and_dex_mcap() -> None:
    record = _load("g9ff.json")
    state = _state(record)
    assert record["account_len"] == 166
    assert state.quote_is_sol is True
    assert state.complete is False
    assert state.real_sol_reserves_lamports == record["real_sol_lamports"]
    assert state.real_sol_reserves_lamports < 85_000_000_000
    assert state.creator == record["creator"]
    assert record["cross_check"]["ok"] is True


def test_non_sol_quotes_are_not_reported_as_sol() -> None:
    for name in ("huic.json", "5yk1.json"):
        record = _load(name)
        state = _state(record)
        assert state.complete is False
        assert state.quote_is_sol is False
        assert state.real_sol_reserves_lamports == 0
        assert state.real_quote_reserves == record["real_quote_reserves"]
        assert state.real_quote_reserves > 85_000_000_000


def test_graduated_curves_are_complete_with_zero_reserves() -> None:
    for name in ("togo.json", "gato.json"):
        record = _load(name)
        state = _state(record)
        assert state.complete is True
        assert state.quote_is_sol is True
        assert state.real_sol_reserves_lamports == 0
        assert state.virtual_sol_reserves_lamports == 0


def test_curve_token_account_is_excluded_from_concentration() -> None:
    record = _load("g9ff.json")
    accounts = curve_token_accounts(record["mint"])
    assert accounts
    reason = exclusion_reason(
        next(iter(accounts)),
        "holder",
        curve=record["curve"],
        pool=None,
        base_vault=None,
        token_accounts=accounts,
    )
    assert reason is not None
    assert "bonding-curve token account" in reason
