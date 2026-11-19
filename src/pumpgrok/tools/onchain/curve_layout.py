"""Bonding-curve account decode over Solana RPC.

The prefix through ``post_complete_quote_in`` matches the ``BondingCurve``
struct in the published IDL
https://github.com/pump-fun/pump-public-docs/blob/main/idl/pump.json
(account discriminator ``sha256("account:BondingCurve")[:8]``). That struct
is 166 bytes including the discriminator. Live mainnet accounts of that
length were checked against it (fixtures under
``tests/fixtures/rpc/bonding_curve/``).

Field order after the discriminator: ``virtual_token_reserves``,
``virtual_quote_reserves``, ``real_token_reserves``, ``real_quote_reserves``,
``token_total_supply``, ``complete``, ``creator`` (bytes 49..81), then the
remaining IDL fields through ``post_complete_quote_in``. Quote reserves are
lamports only when ``quote_mint`` is native SOL (32 zero bytes) or WSOL.
Any other quote is decoded and is not reported as SOL.

Accounts shorter than 166 bytes are rejected. Longer accounts decode the
verified prefix and ignore the tail. The tail is not in the IDL snapshot
this decoder was checked against, so ``TAIL_STATUS`` stays ASSUMED.

RPC only: this module performs no HTTP reads and references no quote
endpoint.
"""

from __future__ import annotations

import base64
import hashlib
import struct
from decimal import Decimal
from typing import Final

from pydantic import BaseModel, ConfigDict

from pumpgrok.core.units import LAMPORTS_PER_SOL, lamports_to_sol, sol_to_lamports
from pumpgrok.tools._common import UNITS_PER_TOKEN

DISCRIMINATOR: Final[bytes] = hashlib.sha256(b"account:BondingCurve").digest()[:8]
VERIFIED_ACCOUNT_LEN: Final[int] = 166
# Bytes after the verified IDL struct. None were present on the captured accounts.
TAIL_STATUS: Final[str] = "ASSUMED"

#: Verified against the IDL and the captured mainnet accounts. While this is
#: VERIFIED, a paper entry may consume an RPC curve price. A quote that is
#: not SOL still fails closed inside ``curve_price_sol_per_token``.
LAYOUT_STATUS: Final[str] = "VERIFIED"

#: SOL-quoted curves complete near 85 SOL of real quote (pump.fun SOL curve).
COMPLETION_SOL_LAMPORTS = sol_to_lamports(Decimal(85))
_MAX_INCOMPLETE_REAL_LAMPORTS = sol_to_lamports(Decimal(90))
_MAX_INCOMPLETE_VIRTUAL_LAMPORTS = sol_to_lamports(Decimal(200))
_MAX_CREATOR_FEE_BPS = 10_000
_MAX_DEPTH = 8

_BODY = struct.Struct("<QQQQQB32sBB32sQBBQQBQQQ")
_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
WSOL_MINT: Final[str] = "So11111111111111111111111111111111111111112"


class CurveDecodeError(ValueError):
    """The account bytes do not match the verified curve layout or its bounds."""


class CurveState(BaseModel):
    """Decoded bonding-curve account. Money is int; the SOL fields are lamports.

    ``virtual_sol_reserves_lamports`` and ``real_sol_reserves_lamports`` are 0
    when ``quote_is_sol`` is false. The raw quote counters stay on
    ``virtual_quote_reserves`` and ``real_quote_reserves``.
    """

    model_config = ConfigDict(frozen=True)

    virtual_sol_reserves_lamports: int
    virtual_token_reserves_units: int
    real_sol_reserves_lamports: int
    real_token_reserves_units: int
    total_supply_units: int
    complete: bool
    creator: str
    quote_mint: str
    quote_is_sol: bool
    virtual_quote_reserves: int
    real_quote_reserves: int
    initial_virtual_quote_reserves: int
    is_mayhem_mode: bool
    is_cashback_coin: bool
    creator_fee_bps: int
    can_edit_creator_fee: bool
    is_holder_reward: bool
    creator_fee: int
    protocol_fees: int
    depth: int
    post_complete_base_out: int
    post_complete_quote_in: int
    data_len: int


def _b58(raw: bytes) -> str:
    number = int.from_bytes(raw, "big")
    digits: list[str] = []
    while number:
        number, remainder = divmod(number, 58)
        digits.append(_ALPHABET[remainder])
    zeros = len(raw) - len(raw.lstrip(b"\x00"))
    return "1" * zeros + "".join(reversed(digits))


def _native_sol_mint() -> str:
    return _b58(b"\x00" * 32)


def quote_is_sol_mint(quote_mint: str) -> bool:
    """True for native SOL and WSOL. Other quote mints are not lamports."""
    return quote_mint in {_native_sol_mint(), WSOL_MINT}


def _as_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CurveDecodeError(f"curve field {name} is not an integer")
    return value


def _as_bytes(value: object, name: str) -> bytes:
    if not isinstance(value, bytes):
        raise CurveDecodeError(f"curve field {name} is not bytes")
    return value


def _require_bool(value: int, name: str) -> bool:
    if value not in (0, 1):
        raise CurveDecodeError(f"curve flag {name} is not boolean: {value!r}")
    return bool(value)


def _unpack(data: bytes) -> tuple[object, ...]:
    if len(data) < VERIFIED_ACCOUNT_LEN:
        raise CurveDecodeError(f"curve account size {len(data)} < verified {VERIFIED_ACCOUNT_LEN}")
    if data[:8] != DISCRIMINATOR:
        raise CurveDecodeError("curve discriminator is not account:BondingCurve")
    if _BODY.size + 8 != VERIFIED_ACCOUNT_LEN:
        raise CurveDecodeError("curve struct size drifted from the IDL length")
    try:
        return _BODY.unpack_from(data, 8)
    except struct.error as exc:
        raise CurveDecodeError(f"curve account unpack failed: {type(exc).__name__}") from exc


def _state_from(parts: tuple[object, ...], data_len: int) -> CurveState:
    (
        virtual_token,
        virtual_quote,
        real_token,
        real_quote,
        supply,
        complete_b,
        creator_raw,
        mayhem_b,
        cashback_b,
        quote_raw,
        fee_bps,
        can_edit_b,
        holder_b,
        creator_fee,
        protocol_fees,
        depth,
        initial_quote,
        post_base,
        post_quote,
    ) = parts
    complete = _require_bool(_as_int(complete_b, "complete"), "complete")
    mayhem = _require_bool(_as_int(mayhem_b, "is_mayhem_mode"), "is_mayhem_mode")
    cashback = _require_bool(_as_int(cashback_b, "is_cashback_coin"), "is_cashback_coin")
    can_edit = _require_bool(_as_int(can_edit_b, "can_edit_creator_fee"), "can_edit_creator_fee")
    holder = _require_bool(_as_int(holder_b, "is_holder_reward"), "is_holder_reward")
    creator = _as_bytes(creator_raw, "creator")
    quote = _as_bytes(quote_raw, "quote_mint")
    quote_mint = _b58(quote)
    is_sol = quote_is_sol_mint(quote_mint)
    virtual_quote_n = _as_int(virtual_quote, "virtual_quote")
    real_quote_n = _as_int(real_quote, "real_quote")
    return CurveState(
        virtual_sol_reserves_lamports=virtual_quote_n if is_sol else 0,
        virtual_token_reserves_units=_as_int(virtual_token, "virtual_token"),
        real_sol_reserves_lamports=real_quote_n if is_sol else 0,
        real_token_reserves_units=_as_int(real_token, "real_token"),
        total_supply_units=_as_int(supply, "supply"),
        complete=complete,
        creator=_b58(creator),
        quote_mint=quote_mint,
        quote_is_sol=is_sol,
        virtual_quote_reserves=virtual_quote_n,
        real_quote_reserves=real_quote_n,
        initial_virtual_quote_reserves=_as_int(initial_quote, "initial_quote"),
        is_mayhem_mode=mayhem,
        is_cashback_coin=cashback,
        creator_fee_bps=_as_int(fee_bps, "creator_fee_bps"),
        can_edit_creator_fee=can_edit,
        is_holder_reward=holder,
        creator_fee=_as_int(creator_fee, "creator_fee"),
        protocol_fees=_as_int(protocol_fees, "protocol_fees"),
        depth=_as_int(depth, "depth"),
        post_complete_base_out=_as_int(post_base, "post_complete_base_out"),
        post_complete_quote_in=_as_int(post_quote, "post_complete_quote_in"),
        data_len=data_len,
    )


def _check_bounds(state: CurveState) -> None:
    if state.creator_fee_bps > _MAX_CREATOR_FEE_BPS:
        raise CurveDecodeError(f"creator_fee_bps {state.creator_fee_bps} is outside 0..10000")
    if state.depth > _MAX_DEPTH:
        raise CurveDecodeError(f"curve depth {state.depth} is outside 0..{_MAX_DEPTH}")
    if state.total_supply_units < 0 or state.real_token_reserves_units < 0:
        raise CurveDecodeError("curve token counters are negative")
    if state.total_supply_units > 0 and state.real_token_reserves_units > state.total_supply_units:
        raise CurveDecodeError("real token reserves exceed total supply")
    if state.complete:
        return
    if state.virtual_token_reserves_units <= 0 or state.total_supply_units <= 0:
        raise CurveDecodeError("incomplete curve is missing token supply or virtual tokens")
    _check_quote_bounds(state)


def _check_quote_bounds(state: CurveState) -> None:
    if state.initial_virtual_quote_reserves > 0:
        expected = state.initial_virtual_quote_reserves + state.real_quote_reserves
        slack = state.creator_fee + state.protocol_fees
        drift = abs(state.virtual_quote_reserves - expected)
        if drift > slack:
            raise CurveDecodeError("virtual quote reserves are not initial plus real")
    if not state.quote_is_sol:
        return
    if state.virtual_quote_reserves <= 0:
        raise CurveDecodeError("incomplete SOL curve has no virtual quote; price undefined")
    if state.real_quote_reserves > _MAX_INCOMPLETE_REAL_LAMPORTS:
        sol = state.real_quote_reserves / LAMPORTS_PER_SOL
        raise CurveDecodeError(
            f"incomplete SOL curve real reserves {sol} SOL exceed the graduation bound"
        )
    if state.virtual_quote_reserves > _MAX_INCOMPLETE_VIRTUAL_LAMPORTS:
        raise CurveDecodeError("incomplete SOL curve virtual reserves are not plausible lamports")


def decode_curve_account(data: bytes) -> CurveState:
    """Decode raw account bytes. Raises :class:`CurveDecodeError` on mismatch.

    Callers map the error to ``DataStale`` / BLIND. A longer account still
    decodes; bytes past the verified prefix are ignored.
    """
    state = _state_from(_unpack(data), len(data))
    _check_bounds(state)
    return state


def decode_curve_account_base64(data: object) -> CurveState:
    """Decode the ``data`` field of a base64 ``getAccountInfo`` value.

    Accepts only the ``["<base64>", "base64"]`` pair form. The ``jsonParsed``
    object form carries no curve bytes and is rejected as malformed.
    """
    if not isinstance(data, list) or len(data) != 2 or data[1] != "base64":
        raise CurveDecodeError("curve account is not base64-encoded bytes")
    try:
        raw = base64.b64decode(data[0], validate=True)
    except (ValueError, TypeError) as exc:
        raise CurveDecodeError("curve account base64 is malformed") from exc
    return decode_curve_account(raw)


def encode_curve_fixture(
    *,
    virtual_sol_reserves_lamports: int,
    virtual_token_reserves_units: int,
    real_sol_reserves_lamports: int,
    real_token_reserves_units: int,
    total_supply_units: int,
    complete: bool,
    creator: bytes = b"\x00" * 32,
    quote_mint: bytes | None = None,
) -> bytes:
    """Build verified-layout account bytes for tests. Never used on the wire.

    ``initial_virtual_quote_reserves`` is ``virtual - real`` when that
    difference is non-negative, so the reserve identity holds. The quote
    mint defaults to native SOL.
    """
    if len(creator) != 32:
        raise CurveDecodeError("fixture creator must be 32 bytes")
    quote = b"\x00" * 32 if quote_mint is None else quote_mint
    if len(quote) != 32:
        raise CurveDecodeError("fixture quote mint must be 32 bytes")
    initial = 0
    if virtual_sol_reserves_lamports >= real_sol_reserves_lamports:
        initial = virtual_sol_reserves_lamports - real_sol_reserves_lamports
    body = _BODY.pack(
        virtual_token_reserves_units,
        virtual_sol_reserves_lamports,
        real_token_reserves_units,
        real_sol_reserves_lamports,
        total_supply_units,
        1 if complete else 0,
        creator,
        0,
        0,
        quote,
        0,
        0,
        0,
        0,
        0,
        0,
        initial,
        0,
        0,
    )
    return DISCRIMINATOR + body


# Tests and callers still say "account size". It is the verified minimum.
CURVE_ACCOUNT_SIZE = VERIFIED_ACCOUNT_LEN


def curve_price_sol_per_token(state: CurveState) -> Decimal:
    """SOL per token from virtual reserves.

    The ported expression ``(vsol / 1e9) / (vtok / 1e6)`` is evaluated in
    :class:`Decimal`. A non-SOL quote raises: those reserves are not lamports.
    """
    if not state.quote_is_sol:
        raise CurveDecodeError("quote mint is not SOL; price is not SOL per token")
    if state.virtual_sol_reserves_lamports <= 0 or state.virtual_token_reserves_units <= 0:
        raise CurveDecodeError("virtual reserves are zero; price undefined")
    return lamports_to_sol(
        Decimal(state.virtual_sol_reserves_lamports)
        / Decimal(state.virtual_token_reserves_units)
        * Decimal(UNITS_PER_TOKEN)
    )


def curve_progress(state: CurveState) -> Decimal:
    """Completion ratio 0..1 from real SOL against the 85 SOL bar.

    A completed SOL curve is 1. A non-SOL quote raises rather than reporting
    a fake percent.
    """
    if not state.quote_is_sol:
        raise CurveDecodeError("bonding progress is defined for SOL-quoted curves only")
    if state.complete:
        return Decimal(1)
    ratio = Decimal(state.real_sol_reserves_lamports) / Decimal(COMPLETION_SOL_LAMPORTS)
    return min(Decimal(1), max(Decimal(0), ratio))


def curve_market_cap_lamports(state: CurveState) -> int:
    """Market cap in lamports: virtual SOL scaled by supply over virtual tokens."""
    if not state.quote_is_sol:
        raise CurveDecodeError("market cap in lamports requires a SOL quote")
    if state.virtual_token_reserves_units <= 0:
        raise CurveDecodeError("virtual token reserves are zero; market cap undefined")
    return (
        state.virtual_sol_reserves_lamports * state.total_supply_units
    ) // state.virtual_token_reserves_units


def rpc_curve_price_allowed(*, curve_source: str) -> bool:
    """True when a price from ``curve_source`` may price a paper entry.

    The reserve prefix is VERIFIED, so an RPC snapshot may price a paper
    fill. Fixture, rest and pumpportal ticks never pass through this layout.
    A non-SOL quote still cannot produce a SOL price.
    """
    return not (curve_source == "rpc" and LAYOUT_STATUS != "VERIFIED")
