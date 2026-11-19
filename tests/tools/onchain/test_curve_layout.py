"""Verified bonding-curve layout: IDL order, minimum length, SOL bounds."""

from __future__ import annotations

import hashlib

import pytest

from pumpgrok.tools.onchain.curve_layout import (
    DISCRIMINATOR,
    LAYOUT_STATUS,
    TAIL_STATUS,
    VERIFIED_ACCOUNT_LEN,
    CurveDecodeError,
    curve_price_sol_per_token,
    curve_progress,
    decode_curve_account,
    encode_curve_fixture,
    quote_is_sol_mint,
)

WSOL = "So11111111111111111111111111111111111111112"


def _open(**overrides: int | bool) -> bytes:
    values: dict[str, int | bool] = {
        "virtual_sol_reserves_lamports": 30_367_798_117,
        "virtual_token_reserves_units": 1_060_004_427_389_821,
        "real_sol_reserves_lamports": 367_798_117,
        "real_token_reserves_units": 780_104_427_389_821,
        "total_supply_units": 1_000_000_000_000_000,
        "complete": False,
    }
    values.update(overrides)
    return encode_curve_fixture(**values)  # type: ignore[arg-type]


def test_discriminator_matches_the_idl_preimage() -> None:
    assert hashlib.sha256(b"account:BondingCurve").digest()[:8] == DISCRIMINATOR
    assert DISCRIMINATOR.hex() == "17b7f83760d8ac60"
    assert LAYOUT_STATUS == "VERIFIED"
    assert TAIL_STATUS == "ASSUMED"


def test_round_trip_puts_token_reserves_before_quote() -> None:
    raw = _open()
    assert len(raw) == VERIFIED_ACCOUNT_LEN
    state = decode_curve_account(raw)
    assert state.quote_is_sol is True
    assert quote_is_sol_mint(state.quote_mint)
    assert state.virtual_token_reserves_units == 1_060_004_427_389_821
    assert state.real_sol_reserves_lamports == 367_798_117
    expected = state.initial_virtual_quote_reserves + state.real_quote_reserves
    assert state.virtual_quote_reserves == expected
    progress = curve_progress(state)
    assert progress < 1
    assert curve_price_sol_per_token(state) > 0


def test_longer_account_decodes_and_ignores_the_tail() -> None:
    state = decode_curve_account(_open() + b"\xff\x00")
    assert state.data_len == VERIFIED_ACCOUNT_LEN + 2
    assert state.real_sol_reserves_lamports == 367_798_117


def test_exact_49_byte_account_is_rejected() -> None:
    with pytest.raises(CurveDecodeError, match="166"):
        decode_curve_account(b"\x00" * 49)


def test_incomplete_sol_above_graduation_is_blind() -> None:
    raw = _open(real_sol_reserves_lamports=961 * 1_000_000_000)
    with pytest.raises(CurveDecodeError, match="graduation"):
        decode_curve_account(raw)


def test_non_sol_quote_decodes_but_is_not_priced_as_sol() -> None:
    quote = hashlib.sha256(b"not-sol").digest()
    raw = encode_curve_fixture(
        virtual_sol_reserves_lamports=1_720_489_670_616,
        virtual_token_reserves_units=763_908_325_129_828,
        real_sol_reserves_lamports=495_609_538_543,
        real_token_reserves_units=484_008_325_129_828,
        total_supply_units=1_000_000_000_000_000,
        complete=False,
        quote_mint=quote,
    )
    state = decode_curve_account(raw)
    assert state.quote_is_sol is False
    assert state.real_sol_reserves_lamports == 0
    assert state.real_quote_reserves == 495_609_538_543
    assert state.quote_mint != WSOL
    with pytest.raises(CurveDecodeError, match="not SOL"):
        curve_price_sol_per_token(state)
    with pytest.raises(CurveDecodeError, match="SOL-quoted"):
        curve_progress(state)
