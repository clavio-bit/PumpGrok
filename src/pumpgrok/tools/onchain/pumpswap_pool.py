"""Find one PumpSwap pool for a mint. Read-only RPC. No signing.

``getProgramAccounts`` on the PumpSwap program with ``memcmp`` at offset 43
(the verified ``base_mint`` offset) is how a mint is mapped to its pool.
That filter returned the TOGO fixture pool. A mint with no such account is
not scored as Raydium, Meteora, or any other AMM.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pumpgrok.core.errors import DataStale
from pumpgrok.tools.onchain.pumpswap_layout import (
    BASE_MINT_OFFSET,
    PUMPSWAP_PROGRAM,
    WSOL_MINT,
    PumpSwapDecodeError,
    PumpSwapPool,
    decode_pool_account_base64,
)
from pumpgrok.tools.onchain.rpc import RpcClient, RpcError


@dataclass(frozen=True, slots=True)
class PoolLookup:
    """A decoded pool, or a fail-closed reason when there is nothing to score."""

    pool: PumpSwapPool | None
    reason: str


def _slot(result: dict[str, Any]) -> int | None:
    context = result.get("context")
    if not isinstance(context, dict):
        return None
    slot = context.get("slot")
    if isinstance(slot, bool) or not isinstance(slot, int):
        return None
    return slot


def _decode_item(item: object) -> PumpSwapPool | None:
    if not isinstance(item, dict):
        return None
    address = item.get("pubkey")
    account = item.get("account")
    if not isinstance(address, str) or not isinstance(account, dict):
        return None
    if account.get("owner") != PUMPSWAP_PROGRAM:
        return None
    try:
        return decode_pool_account_base64(account.get("data"), address=address)
    except PumpSwapDecodeError:
        return None


async def lookup_pumpswap_pool(rpc: RpcClient, mint: str) -> PoolLookup:
    """Return the single WSOL-quoted PumpSwap pool for ``mint``, or a reason."""
    try:
        found = await rpc.call(
            "getProgramAccounts",
            [
                PUMPSWAP_PROGRAM,
                {
                    "encoding": "base64",
                    "filters": [{"memcmp": {"offset": BASE_MINT_OFFSET, "bytes": mint}}],
                },
            ],
        )
    except RpcError as exc:
        raise DataStale("pumpswap pool lookup failed") from exc
    if not isinstance(found, list) or not found:
        return PoolLookup(
            pool=None,
            reason="no PumpSwap pool for this mint; other AMMs are not scored",
        )
    decoded = [pool for item in found if (pool := _decode_item(item)) is not None]
    if not decoded:
        return PoolLookup(
            pool=None,
            reason="PumpSwap account at the base-mint offset did not decode",
        )
    pools = [pool for pool in decoded if pool.base_mint == mint and pool.quote_mint == WSOL_MINT]
    if not pools:
        return PoolLookup(
            pool=None,
            reason="no PumpSwap WSOL pool for this mint; other AMMs are not scored",
        )
    if len(pools) != 1:
        return PoolLookup(
            pool=None,
            reason="multiple PumpSwap WSOL pools for this mint; not choosing one",
        )
    return await _confirm_pool(rpc, pools[0], mint)


async def _confirm_pool(rpc: RpcClient, chosen: PumpSwapPool, mint: str) -> PoolLookup:
    """Re-read the chosen pool so the row carries that account's slot."""
    try:
        result = await rpc.call("getAccountInfo", [chosen.address, {"encoding": "base64"}])
    except RpcError as exc:
        raise DataStale("pumpswap pool account read failed") from exc
    if not isinstance(result, dict) or not isinstance(result.get("value"), dict):
        return PoolLookup(pool=None, reason="PumpSwap pool account missing")
    value = result["value"]
    if value.get("owner") != PUMPSWAP_PROGRAM:
        return PoolLookup(pool=None, reason="PumpSwap pool owner is not the AMM program")
    try:
        confirmed = decode_pool_account_base64(
            value.get("data"), address=chosen.address, slot=_slot(result)
        )
    except PumpSwapDecodeError:
        return PoolLookup(pool=None, reason="PumpSwap pool account did not decode")
    if confirmed.base_mint != mint or confirmed.quote_mint != WSOL_MINT:
        return PoolLookup(pool=None, reason="PumpSwap pool account did not match the mint")
    return PoolLookup(pool=confirmed, reason="")
