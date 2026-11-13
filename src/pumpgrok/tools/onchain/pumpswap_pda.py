"""Canonical PumpSwap migration-pool address. Read-only. No signing.

The seeds are the published IDLs in
https://github.com/pump-fun/pump-public-docs (``idl/pump_amm.ts`` and
``idl/pump.json``), not a guess:

- ``migrate`` / ``migrate_v2`` account ``pool_authority`` is a PDA of the
  pump program ``6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P`` with seeds
  ``["pool-authority", mint]``.
- ``createPool`` on the AMM program, and the ``pool`` account of
  ``migrate``, use seeds ``["pool", index, creator, base_mint, quote_mint]``.
  Migration encodes the index as the constant bytes ``[0, 0]`` (u16
  little-endian 0). The creator is the pool-authority PDA. The quote mint
  on ``migrate`` is WSOL.

Those seeds reproduce the mainnet pools TOGO
``AeV3q7n6bHcSmWuf4vfKSTPtWbn59y5wNnqLQCvNwnaF`` and GATO
``5E3dXLXGwmTDCokA51WYYaKttjFZBwqCoyUHBUi5gt2m``. The off-curve check is
ed25519 point decompression, the same rule Solana uses for
``is_on_curve``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Final

from pumpgrok.tools.onchain.pumpswap_layout import PUMPSWAP_PROGRAM, WSOL_MINT

PUMP_PROGRAM: Final[str] = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
POOL_SEED: Final[bytes] = b"pool"
POOL_AUTHORITY_SEED: Final[bytes] = b"pool-authority"
# migrate.pool seeds: const [0, 0], which is index u16 little-endian 0.
CANONICAL_INDEX: Final[bytes] = b"\x00\x00"
_PDA_MARKER: Final[bytes] = b"ProgramDerivedAddress"
_PUBKEY_LEN: Final[int] = 32
_P: Final[int] = 2**255 - 19
_D: Final[int] = (-121665 * pow(121666, _P - 2, _P)) % _P
_ALPHABET: Final[str] = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_INDEX: Final[dict[str, int]] = {char: index for index, char in enumerate(_ALPHABET)}


class PdaError(ValueError):
    """The seeds do not derive an address. Callers stay blind and do not guess."""


def pubkey_bytes(text: str) -> bytes:
    """Decode one base58 pubkey. ``PdaError`` unless it is exactly 32 bytes."""
    return _decode_pubkey(text)


def program_derived_address(seeds: Sequence[bytes], program: str) -> str:
    """Base58 address at the canonical bump for ``seeds`` under ``program``."""
    derived, _bump = _find_program_address(seeds, _decode_pubkey(program))
    return _encode(derived)


def canonical_pool_address(mint: str) -> str:
    """Migration pool PDA for ``mint`` quoted in WSOL, index 0."""
    mint_bytes = _decode_pubkey(mint)
    pump = _decode_pubkey(PUMP_PROGRAM)
    authority, _authority_bump = _find_program_address([POOL_AUTHORITY_SEED, mint_bytes], pump)
    pool, _pool_bump = _find_program_address(
        [POOL_SEED, CANONICAL_INDEX, authority, mint_bytes, _decode_pubkey(WSOL_MINT)],
        _decode_pubkey(PUMPSWAP_PROGRAM),
    )
    return _encode(pool)


def _decode_pubkey(text: str) -> bytes:
    try:
        raw = _b58decode(text)
    except ValueError as exc:
        raise PdaError("pubkey is not base58") from exc
    if len(raw) != _PUBKEY_LEN:
        raise PdaError("pubkey is not 32 bytes")
    return raw


def _b58decode(text: str) -> bytes:
    number = 0
    for char in text:
        digit = _INDEX.get(char)
        if digit is None:
            raise ValueError("character outside the base58 alphabet")
        number = number * 58 + digit
    leading = len(text) - len(text.lstrip("1"))
    body = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    return b"\x00" * leading + body


def _encode(raw: bytes) -> str:
    number = int.from_bytes(raw, "big")
    digits: list[str] = []
    while number:
        number, remainder = divmod(number, 58)
        digits.append(_ALPHABET[remainder])
    zeros = len(raw) - len(raw.lstrip(b"\x00"))
    return "1" * zeros + "".join(reversed(digits))


def _y_and_parity(point: bytes) -> tuple[int, int]:
    y = int.from_bytes(point, "little")
    parity = y >> 255
    return y & ((1 << 255) - 1), parity


def _x_squared(y: int) -> int:
    y2 = y * y % _P
    u = (y2 - 1) % _P
    v = (_D * y2 + 1) % _P
    return u * pow(v, _P - 2, _P) % _P


def _sqrt_exists(x2: int, parity: int) -> bool:
    """True when the compressed point has an x, which means it is on the curve."""
    if x2 == 0:
        return parity == 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * pow(2, (_P - 1) // 4, _P) % _P
    return (x * x - x2) % _P == 0


def _is_on_curve(point: bytes) -> bool:
    if len(point) != _PUBKEY_LEN:
        return False
    y, parity = _y_and_parity(point)
    if y >= _P:
        return False
    return _sqrt_exists(_x_squared(y), parity)


def _create_program_address(seeds: Sequence[bytes], program: bytes) -> bytes:
    hashed = hashlib.sha256(b"".join(seeds) + program + _PDA_MARKER).digest()
    if _is_on_curve(hashed):
        raise PdaError("derived address is on the curve")
    return hashed


def _find_program_address(seeds: Sequence[bytes], program: bytes) -> tuple[bytes, int]:
    for bump in range(255, -1, -1):
        try:
            return _create_program_address([*seeds, bytes([bump])], program), bump
        except PdaError:
            continue
    raise PdaError("no program address for these seeds")
