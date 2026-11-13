"""PumpSwap pool account prefix (pump.fun AMM).

Program: ``pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA``.

The field order through ``coin_creator`` follows the ``pool`` struct in the
published IDL at
https://github.com/pump-fun/pump-public-docs/blob/main/idl/pump_amm.ts
(struct name ``pool``; the Anchor discriminator is ``sha256("account:Pool")[:8]``).

That prefix was checked against mainnet pool
``AeV3q7n6bHcSmWuf4vfKSTPtWbn59y5wNnqLQCvNwnaF`` captured in
``tests/fixtures/rpc/pumpswap/togo_pool.json``. The canonical migration PDA
(``pumpswap_pda``) reproduces this address. The capture itself used
``getProgramAccounts`` with ``memcmp`` at offset 43 on the TOGO mint.
Offset 43 is that mint, offset 75 is WSOL, the two vaults are token
accounts of those mints, and the LP mint authority is the pool.

Bytes from offset 244 to the end are not decoded. The IDL lists further
fields there (``isCashbackCoin``, ``virtualQuoteReserves``, and others).
On the 301-byte fixture those bytes are not separated from padding, so the
tail is ASSUMED and unused. A short account or a bad discriminator is a
decode error. Callers stay blind. This module does not sign or send.
"""

from __future__ import annotations

import base64
import hashlib
import struct
from typing import Final

from pydantic import BaseModel, ConfigDict

PUMPSWAP_PROGRAM: Final[str] = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
WSOL_MINT: Final[str] = "So11111111111111111111111111111111111111112"
BASE_MINT_OFFSET: Final[int] = 43
# Through coin_creator. is_mayhem_mode is the next byte when the account has it.
VERIFIED_PREFIX_LEN: Final[int] = 243
POOL_DISCRIMINATOR: Final[bytes] = hashlib.sha256(b"account:Pool").digest()[:8]
# The unused tail is not a verified layout. See the module docstring.
LAYOUT_TAIL: Final[str] = "ASSUMED"

_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


class PumpSwapDecodeError(ValueError):
    """The account bytes do not match the verified PumpSwap pool prefix."""


class PumpSwapPool(BaseModel):
    """Decoded pool prefix. Money stays int. The tail is not represented."""

    model_config = ConfigDict(frozen=True)

    address: str
    slot: int | None = None
    bump: int
    index: int
    creator: str
    base_mint: str
    quote_mint: str
    lp_mint: str
    base_vault: str
    quote_vault: str
    lp_supply: int
    coin_creator: str
    data_len: int


def _base58(raw: bytes) -> str:
    number = int.from_bytes(raw, "big")
    digits: list[str] = []
    while number:
        number, remainder = divmod(number, 58)
        digits.append(_ALPHABET[remainder])
    zeros = len(raw) - len(raw.lstrip(b"\x00"))
    return "1" * zeros + "".join(reversed(digits))


def _pubkey(data: bytes, offset: int, *, allow_zero: bool = False) -> str:
    raw = data[offset : offset + 32]
    if len(raw) != 32:
        raise PumpSwapDecodeError(f"pool pubkey at offset {offset} is short")
    if raw == b"\x00" * 32:
        if not allow_zero:
            raise PumpSwapDecodeError(f"pool pubkey at offset {offset} is empty")
        return "1" * 32
    return _base58(raw)


def decode_pool_account(data: bytes, *, address: str, slot: int | None = None) -> PumpSwapPool:
    """Decode the verified prefix. The tail from offset 244 is ignored."""
    if len(data) < VERIFIED_PREFIX_LEN:
        raise PumpSwapDecodeError(
            f"pool account size {len(data)} < verified prefix {VERIFIED_PREFIX_LEN}"
        )
    if data[:8] != POOL_DISCRIMINATOR:
        raise PumpSwapDecodeError("pool discriminator is not account:Pool")
    bump = data[8]
    index = struct.unpack_from("<H", data, 9)[0]
    lp_supply = struct.unpack_from("<Q", data, 203)[0]
    return PumpSwapPool(
        address=address,
        slot=slot,
        bump=bump,
        index=index,
        creator=_pubkey(data, 11),
        base_mint=_pubkey(data, 43),
        quote_mint=_pubkey(data, 75),
        lp_mint=_pubkey(data, 107),
        base_vault=_pubkey(data, 139),
        quote_vault=_pubkey(data, 171),
        lp_supply=lp_supply,
        coin_creator=_pubkey(data, 211, allow_zero=True),
        data_len=len(data),
    )


def decode_pool_account_base64(
    data: object, *, address: str, slot: int | None = None
) -> PumpSwapPool:
    """Decode a ``["<base64>", "base64"]`` account data pair."""
    if not isinstance(data, list) or len(data) != 2 or data[1] != "base64":
        raise PumpSwapDecodeError("pool account is not base64-encoded bytes")
    try:
        raw = base64.b64decode(data[0], validate=True)
    except (ValueError, TypeError) as exc:
        raise PumpSwapDecodeError("pool account base64 is malformed") from exc
    return decode_pool_account(raw, address=address, slot=slot)
