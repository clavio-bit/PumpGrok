"""Bonding-curve PDA and its token account. Read-only. No signing.

Seeds are the ``buy`` / ``sell`` accounts in the published pump IDL
(``idl/pump.json``, instruction ``buy``):

- curve: ``["bonding-curve", mint]`` under ``6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P``
- associated token account: ``[curve, token_program, mint]`` under the
  Associated Token program
"""

from __future__ import annotations

from typing import Final

from pumpgrok.tools.onchain.pumpswap_pda import (
    PUMP_PROGRAM,
    PdaError,
    program_derived_address,
    pubkey_bytes,
)

ATA_PROGRAM: Final[str] = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
SPL_TOKEN_PROGRAM: Final[str] = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"  # noqa: S105
TOKEN_2022_PROGRAM: Final[str] = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"  # noqa: S105
_CURVE_SEED: Final[bytes] = b"bonding-curve"


def bonding_curve_address(mint: str) -> str:
    """Canonical bonding-curve PDA for ``mint``."""
    return program_derived_address([_CURVE_SEED, pubkey_bytes(mint)], PUMP_PROGRAM)


def associated_bonding_curve(mint: str, token_program: str) -> str:
    """Token account that holds the curve's tokens for one token program."""
    curve = bonding_curve_address(mint)
    return program_derived_address(
        [pubkey_bytes(curve), pubkey_bytes(token_program), pubkey_bytes(mint)],
        ATA_PROGRAM,
    )


def curve_token_accounts(mint: str) -> frozenset[str]:
    """SPL and Token-2022 curve token accounts. An underivable mint yields an empty set."""
    found: set[str] = set()
    for program in (SPL_TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
        try:
            found.add(associated_bonding_curve(mint, program))
        except PdaError:
            continue
    return frozenset(found)
