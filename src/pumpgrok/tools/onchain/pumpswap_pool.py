"""Find one PumpSwap pool for a mint. Read-only RPC. No signing.

The primary lookup derives the canonical migration pool PDA and reads it
with one ``getAccountInfo``. Seeds are in ``pumpswap_pda`` (published pump
and pump_amm IDLs). A hit on that account, owned by the AMM program, with
the Pool discriminator and this mint quoted in WSOL, is the migration pool.

A missing or invalid derived account falls back to Helius
``getProgramAccountsV2`` with pagination and the same base-mint memcmp at
offset 43, when that method is offered. Otherwise the fallback is
``getProgramAccounts``. More than one WSOL pool on a fallback is blind.
Nothing found is blind. A scan error, including JSON-RPC -32600 from the
legacy method, is blind. This module never guesses a pool.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from pumpgrok.core.errors import DataStale
from pumpgrok.tools.onchain.pumpswap_layout import (
    BASE_MINT_OFFSET,
    PUMPSWAP_PROGRAM,
    WSOL_MINT,
    PumpSwapDecodeError,
    PumpSwapPool,
    decode_pool_account_base64,
)
from pumpgrok.tools.onchain.pumpswap_pda import PdaError, canonical_pool_address
from pumpgrok.tools.onchain.rpc import RpcClient, RpcError

LOOKUP_CANONICAL: Final[str] = "canonical_pda"
LOOKUP_V2: Final[str] = "getProgramAccountsV2"
LOOKUP_GPA: Final[str] = "getProgramAccounts"
V2_PAGE_LIMIT: Final[int] = 1000
_MAX_V2_PAGES: Final[int] = 8
_NO_POOL: Final[str] = "no PumpSwap pool for this mint; other AMMs are not scored"
_NO_WSOL: Final[str] = "no PumpSwap WSOL pool for this mint; other AMMs are not scored"
_BAD_DECODE: Final[str] = "PumpSwap account at the base-mint offset did not decode"
_MANY: Final[str] = "multiple PumpSwap WSOL pools for this mint; not choosing one"
_SCAN_FAILED: Final[str] = "PumpSwap pool scan failed"


class _V2Failed(Exception):
    """V2 answered, then a later page was unusable. Do not guess from a prefix."""


@dataclass(frozen=True, slots=True)
class PoolLookup:
    """A decoded pool, or a fail-closed reason when there is nothing to score."""

    pool: PumpSwapPool | None
    reason: str


def _tag(reason: str, path: str) -> str:
    suffix = f"lookup={path}"
    if not reason:
        return suffix
    return f"{reason}; {suffix}"


def _blind(reason: str, path: str) -> PoolLookup:
    return PoolLookup(pool=None, reason=_tag(reason, path))


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


def _account_pool(result: object, address: str, mint: str) -> PumpSwapPool | None:
    if not isinstance(result, dict):
        return None
    value = result.get("value")
    if not isinstance(value, dict) or value.get("owner") != PUMPSWAP_PROGRAM:
        return None
    try:
        pool = decode_pool_account_base64(value.get("data"), address=address, slot=_slot(result))
    except PumpSwapDecodeError:
        return None
    if pool.base_mint != mint or pool.quote_mint != WSOL_MINT:
        return None
    return pool


def _memcmp_filter(mint: str) -> dict[str, Any]:
    return {
        "encoding": "base64",
        "filters": [{"memcmp": {"offset": BASE_MINT_OFFSET, "bytes": mint}}],
    }


def _v2_params(mint: str, pagination_key: str | None) -> list[Any]:
    config = _memcmp_filter(mint)
    config["limit"] = V2_PAGE_LIMIT
    if pagination_key:
        config["paginationKey"] = pagination_key
    return [PUMPSWAP_PROGRAM, config]


def _v2_body(result: dict[str, Any]) -> dict[str, Any] | None:
    if isinstance(result.get("accounts"), list):
        return result
    value = result.get("value")
    if isinstance(value, dict) and isinstance(value.get("accounts"), list):
        return value
    return None


def _v2_page(result: object) -> tuple[list[object], str | None] | None:
    if not isinstance(result, dict):
        return None
    body = _v2_body(result)
    if body is None:
        return None
    accounts = body.get("accounts")
    if not isinstance(accounts, list):
        return None
    key = body.get("paginationKey")
    if isinstance(key, str) and key:
        return accounts, key
    return accounts, None


def _select(items: list[object], mint: str, path: str) -> PoolLookup:
    """The single WSOL pool in ``items``, or a blind reason. Does not re-read."""
    decoded = [pool for item in items if (pool := _decode_item(item)) is not None]
    if items and not decoded:
        return _blind(_BAD_DECODE, path)
    pools = [pool for pool in decoded if pool.base_mint == mint and pool.quote_mint == WSOL_MINT]
    if len(pools) > 1:
        return _blind(_MANY, path)
    if len(pools) == 1:
        return PoolLookup(pool=pools[0], reason="")
    if decoded:
        return _blind(_NO_WSOL, path)
    return _blind(_NO_POOL, path)


def _confirm_miss(result: object, address: str) -> PoolLookup:
    if not isinstance(result, dict) or not isinstance(result.get("value"), dict):
        return PoolLookup(pool=None, reason="PumpSwap pool account missing")
    value = result["value"]
    if not isinstance(value, dict):
        return PoolLookup(pool=None, reason="PumpSwap pool account missing")
    if value.get("owner") != PUMPSWAP_PROGRAM:
        return PoolLookup(pool=None, reason="PumpSwap pool owner is not the AMM program")
    try:
        decode_pool_account_base64(value.get("data"), address=address)
    except PumpSwapDecodeError:
        return PoolLookup(pool=None, reason="PumpSwap pool account did not decode")
    return PoolLookup(pool=None, reason="PumpSwap pool account did not match the mint")


async def _confirm_pool(rpc: RpcClient, chosen: PumpSwapPool, mint: str) -> PoolLookup:
    """Re-read the chosen pool so the row carries that account's slot."""
    try:
        result = await rpc.call("getAccountInfo", [chosen.address, {"encoding": "base64"}])
    except RpcError as exc:
        raise DataStale("pumpswap pool account read failed") from exc
    pool = _account_pool(result, chosen.address, mint)
    if pool is None:
        return _confirm_miss(result, chosen.address)
    return PoolLookup(pool=pool, reason="")


async def _choose(rpc: RpcClient, items: list[object], mint: str, path: str) -> PoolLookup:
    selected = _select(items, mint, path)
    if selected.pool is None:
        return selected
    confirmed = await _confirm_pool(rpc, selected.pool, mint)
    if confirmed.pool is None:
        return PoolLookup(pool=None, reason=_tag(confirmed.reason, path))
    return PoolLookup(pool=confirmed.pool, reason=_tag("", path))


async def _one_v2_page(
    rpc: RpcClient, mint: str, pagination_key: str | None, *, started: bool
) -> tuple[list[object], str | None]:
    try:
        result = await rpc.call("getProgramAccountsV2", _v2_params(mint, pagination_key))
    except RpcError:
        if started:
            raise _V2Failed from None
        raise
    page = _v2_page(result)
    if page is None:
        raise _V2Failed
    return page


async def _v2_accounts(rpc: RpcClient, mint: str) -> list[object] | None:
    """Account items from V2, or None when the method is not offered."""
    try:
        batch, key = await _one_v2_page(rpc, mint, None, started=False)
    except RpcError:
        return None
    collected = list(batch)
    seen: set[str] = set()
    pages = 1
    while key and batch:
        if key in seen or pages >= _MAX_V2_PAGES:
            raise _V2Failed
        seen.add(key)
        batch, key = await _one_v2_page(rpc, mint, key, started=True)
        collected.extend(batch)
        pages += 1
    return collected


async def _read_canonical(rpc: RpcClient, mint: str, address: str) -> PoolLookup | None:
    try:
        result = await rpc.call("getAccountInfo", [address, {"encoding": "base64"}])
    except RpcError:
        return None
    pool = _account_pool(result, address, mint)
    if pool is None:
        return None
    return PoolLookup(pool=pool, reason=_tag("", LOOKUP_CANONICAL))


async def _legacy_accounts(rpc: RpcClient, mint: str) -> PoolLookup:
    try:
        found = await rpc.call("getProgramAccounts", [PUMPSWAP_PROGRAM, _memcmp_filter(mint)])
    except RpcError:
        return _blind(_SCAN_FAILED, LOOKUP_GPA)
    if not isinstance(found, list):
        return _blind(_NO_POOL, LOOKUP_GPA)
    return await _choose(rpc, found, mint, LOOKUP_GPA)


async def _lookup_by_scan(rpc: RpcClient, mint: str) -> PoolLookup:
    try:
        found = await _v2_accounts(rpc, mint)
    except _V2Failed:
        return _blind(_SCAN_FAILED, LOOKUP_V2)
    if found is None:
        return await _legacy_accounts(rpc, mint)
    return await _choose(rpc, found, mint, LOOKUP_V2)


async def lookup_pumpswap_pool(rpc: RpcClient, mint: str) -> PoolLookup:
    """Return the WSOL-quoted PumpSwap pool for ``mint``, or a blind reason."""
    try:
        address = canonical_pool_address(mint)
    except PdaError:
        return _blind("mint is not a 32-byte pubkey", LOOKUP_CANONICAL)
    canonical = await _read_canonical(rpc, mint, address)
    if canonical is not None:
        return canonical
    return await _lookup_by_scan(rpc, mint)
