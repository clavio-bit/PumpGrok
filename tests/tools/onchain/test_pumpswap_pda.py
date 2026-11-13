"""Offline canonical PumpSwap pool PDA. No network.

Seeds are the published pump and pump_amm IDLs. These two addresses are
mainnet migration pools.
"""

from __future__ import annotations

import pytest

from pumpgrok.tools.onchain.pumpswap_pda import (
    PdaError,
    canonical_pool_address,
    program_derived_address,
    pubkey_bytes,
)

TOGO = "HcJGYDM5bS19fDSMUHNcH6wKP68kmHksNmXsTW2Wpump"
GATO = "HsprHxBRgiVaQdRf3TnKNuZZtaX9JUSPgMFdAkk6pump"
POOLS = {
    TOGO: "AeV3q7n6bHcSmWuf4vfKSTPtWbn59y5wNnqLQCvNwnaF",
    GATO: "5E3dXLXGwmTDCokA51WYYaKttjFZBwqCoyUHBUi5gt2m",
}


@pytest.mark.parametrize(("mint", "pool"), list(POOLS.items()))
def test_canonical_pda_reproduces_the_mainnet_migration_pool(mint: str, pool: str) -> None:
    assert canonical_pool_address(mint) == pool
    assert len(pubkey_bytes(pool)) == 32


def test_bad_mint_does_not_guess_an_address() -> None:
    with pytest.raises(PdaError):
        canonical_pool_address("not-a-mint")
    with pytest.raises(PdaError):
        program_derived_address([b"pool"], "short")
