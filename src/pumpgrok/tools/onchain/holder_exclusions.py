"""Drop curve and PumpSwap vault accounts from the top-10 safety check.

Read-only. The canonical migration pool is derived and read with one
``getAccountInfo``. Nothing here signs or submits a transaction.

An open bonding curve has no pool vault: holders owned by the curve account,
and the curve account itself, are removed and the rest are scored. A canonical
pool, including when the curve account still decodes as open, removes the base
vault by token-account address and any holder owned by the pool PDA. The quote
vault is WSOL; it is recorded and is not a holder of the mint.

When the curve is not a confirmed open curve and the pool cannot be read, the
check is blind. A missing lookup is not a pass, and the unread vault is not
left in the sample to force a fail.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from pumpgrok.core.errors import DataStale
from pumpgrok.tools.onchain.curve_layout import CurveDecodeError, decode_curve_account_base64
from pumpgrok.tools.onchain.pumpswap_layout import (
    PUMPSWAP_PROGRAM,
    WSOL_MINT,
    PumpSwapDecodeError,
    PumpSwapPool,
    decode_pool_account_base64,
)
from pumpgrok.tools.onchain.pumpswap_pda import PdaError, canonical_pool_address
from pumpgrok.tools.onchain.pumpswap_pool import lookup_pumpswap_pool
from pumpgrok.tools.onchain.rpc import RpcClient, RpcError
from pumpgrok.tools.onchain.top_holders import TopHoldersIn, run_top_holders

_CURVE_REASON = "bonding-curve account"
_VAULT_REASON = "pumpswap base vault"
_POOL_ACCOUNT_REASON = "pumpswap pool account"
_POOL_OWNER_REASON = "owner is the pumpswap pool"
_CANONICAL = "lookup=canonical_pda"


@dataclass(frozen=True, slots=True)
class _Canonical:
    """A decoded migration pool, a confirmed miss, or a read that failed."""

    pool: PumpSwapPool | None
    failed: bool


def _slot(result: dict[str, Any]) -> int | None:
    context = result.get("context")
    if not isinstance(context, dict):
        return None
    slot = context.get("slot")
    if isinstance(slot, bool) or not isinstance(slot, int):
        return None
    return slot


def _decoded_open(result: object) -> bool:
    """True only when the account decodes as a curve that is not complete."""
    if not isinstance(result, dict):
        return False
    value = result.get("value")
    if not isinstance(value, dict):
        return False
    try:
        state = decode_curve_account_base64(value.get("data"))
    except CurveDecodeError:
        return False
    return not state.complete


async def curve_is_open(rpc: RpcClient, curve_account: str) -> bool:
    """True when ``curve_account`` decodes and its completion flag is clear."""
    try:
        result = await rpc.call("getAccountInfo", [curve_account, {"encoding": "base64"}])
    except RpcError:
        return False
    return _decoded_open(result)


def _owner_of(result: object) -> str | None:
    if not isinstance(result, dict):
        return None
    value = result.get("value")
    if not isinstance(value, dict):
        return None
    data = value.get("data")
    parsed = data.get("parsed") if isinstance(data, dict) else None
    info = parsed.get("info") if isinstance(parsed, dict) else None
    if not isinstance(info, dict):
        return None
    owner = info.get("owner")
    if isinstance(owner, str) and owner:
        return owner
    return None


async def _owner(rpc: RpcClient, address: str) -> str | None:
    """Token-account owner. A failed read is unknown, not an exclusion."""
    try:
        result = await rpc.call("getAccountInfo", [address, {"encoding": "jsonParsed"}])
    except RpcError:
        return None
    return _owner_of(result)


def _classify(result: dict[str, Any], address: str, mint: str) -> _Canonical:
    """A matching pool, a confirmed non-pool, or an unreadable AMM account."""
    value = result.get("value")
    if value is None:
        return _Canonical(pool=None, failed=False)
    if not isinstance(value, dict) or value.get("owner") != PUMPSWAP_PROGRAM:
        return _Canonical(pool=None, failed=not isinstance(value, dict))
    try:
        pool = decode_pool_account_base64(value.get("data"), address=address, slot=_slot(result))
    except PumpSwapDecodeError:
        return _Canonical(pool=None, failed=True)
    if pool.base_mint != mint or pool.quote_mint != WSOL_MINT:
        return _Canonical(pool=None, failed=False)
    return _Canonical(pool=pool, failed=False)


async def _canonical(rpc: RpcClient, mint: str) -> _Canonical:
    """One ``getAccountInfo`` of the migration PDA. A miss is not a scan."""
    try:
        address = canonical_pool_address(mint)
    except PdaError:
        return _Canonical(pool=None, failed=True)
    try:
        result = await rpc.call("getAccountInfo", [address, {"encoding": "base64"}])
    except RpcError:
        return _Canonical(pool=None, failed=True)
    if not isinstance(result, dict):
        return _Canonical(pool=None, failed=True)
    return _classify(result, address, mint)


def exclusion_reason(
    address: str,
    owner: str | None,
    *,
    curve: str | None,
    pool: str | None,
    base_vault: str | None,
) -> str | None:
    """Why ``address`` is a liquidity account, or None when it counts."""
    parts: list[str] = []
    if curve is not None and curve in (address, owner):
        parts.append(_CURVE_REASON)
    if base_vault is not None and address == base_vault:
        parts.append(_VAULT_REASON)
    if pool is not None and address == pool:
        parts.append(_POOL_ACCOUNT_REASON)
    elif pool is not None and owner == pool:
        parts.append(_POOL_OWNER_REASON)
    if not parts:
        return None
    return "; ".join(parts)


def _blind(detail: str) -> dict[str, Any]:
    """No share. A zero top-5 share cannot trip the hard veto on unread data."""
    reason = (
        "cannot tell whether the dominant holder is the PumpSwap pool; "
        f"{detail}; concentration not scored"
    )
    return {
        "holders": [],
        "top1_share": Decimal(0),
        "top5_share": Decimal(0),
        "top10_share": Decimal(0),
        "supply_units": 0,
        "excluded": [],
        "exclusions": [],
        "blind_reason": reason,
    }


def _share(rows: list[dict[str, Any]], count: int) -> Decimal:
    total = sum((Decimal(str(row["share"])) for row in rows[:count]), Decimal(0))
    return min(Decimal(1), total)


async def _with_owners(rpc: RpcClient, rows: list[Any]) -> list[dict[str, Any]]:
    owned: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("address"), str):
            raise DataStale("onchain.top_holders snapshot malformed")
        address = row["address"]
        owned.append({**row, "owner": await _owner(rpc, address)})
    return owned


def _partition(
    rows: list[dict[str, Any]],
    *,
    curve: str,
    pool: PumpSwapPool | None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    pool_address = None if pool is None else pool.address
    base_vault = None if pool is None else pool.base_vault
    kept: list[dict[str, Any]] = []
    exclusions: list[dict[str, str]] = []
    for row in rows:
        address = str(row["address"])
        owner = row.get("owner")
        owner_s = owner if isinstance(owner, str) else None
        reason = exclusion_reason(
            address, owner_s, curve=curve, pool=pool_address, base_vault=base_vault
        )
        if reason is None:
            kept.append(row)
            continue
        exclusions.append({"address": address, "reason": reason})
    exclusions.sort(key=lambda item: item["address"])
    return kept, exclusions


def _payload(
    base: dict[str, Any],
    kept: list[dict[str, Any]],
    exclusions: list[dict[str, str]],
    *,
    pool: PumpSwapPool | None,
    lookup: str | None,
) -> dict[str, Any]:
    out = dict(base)
    out["holders"] = kept
    out["top1_share"] = _share(kept, 1)
    out["top5_share"] = _share(kept, 5)
    out["top10_share"] = _share(kept, 10)
    out["excluded"] = [item["address"] for item in exclusions]
    out["exclusions"] = exclusions
    out["blind_reason"] = None
    if pool is None:
        return out
    out["pool"] = pool.address
    out["base_vault"] = pool.base_vault
    out["quote_vault"] = pool.quote_vault
    out["pool_lookup"] = lookup or _CANONICAL
    if pool.slot is not None:
        out["slot"] = pool.slot
    return out


def _sample_unreadable(pool: PumpSwapPool) -> dict[str, Any]:
    """The pool is known and the largest-accounts sample is not. Do not score."""
    return _blind(
        "holder sample unreadable after identifying "
        f"pool={pool.address} base_vault={pool.base_vault} quote_vault={pool.quote_vault}"
    )


async def _concentrate(
    mint: str,
    rpc: RpcClient,
    *,
    curve: str,
    pool: PumpSwapPool | None,
    lookup: str | None,
) -> dict[str, Any]:
    try:
        base = await run_top_holders(TopHoldersIn(mint=mint), rpc)
    except DataStale:
        if pool is None:
            raise
        return _sample_unreadable(pool)
    raw = base.get("holders")
    rows = raw if isinstance(raw, list) else []
    owned = await _with_owners(rpc, rows)
    kept, exclusions = _partition(owned, curve=curve, pool=pool)
    return _payload(base, kept, exclusions, pool=pool, lookup=lookup)


async def _fallback(mint: str, curve: str, rpc: RpcClient) -> dict[str, Any]:
    """Scan only after the canonical account missed and the curve is not open."""
    lookup = await lookup_pumpswap_pool(rpc, mint)
    if lookup.pool is None:
        detail = lookup.reason or "PumpSwap pool lookup returned no pool"
        return _blind(detail)
    return await _concentrate(mint, rpc, curve=curve, pool=lookup.pool, lookup=lookup.reason)


async def run_concentration(mint: str, curve_account: str, rpc: RpcClient) -> dict[str, Any]:
    """Top-10 concentration with the curve and the PumpSwap base vault removed.

    A canonical pool hit wins even if the curve account still decodes as open,
    which is how a graduated mint avoids counting its base vault. An open curve
    with no canonical pool is scored after the curve exclusion. Anything else
    that does not yield one pool is blind.
    """
    open_curve = await curve_is_open(rpc, curve_account)
    found = await _canonical(rpc, mint)
    if found.pool is not None:
        return await _concentrate(
            mint, rpc, curve=curve_account, pool=found.pool, lookup=_CANONICAL
        )
    if open_curve and not found.failed:
        return await _concentrate(mint, rpc, curve=curve_account, pool=None, lookup=None)
    if found.failed and open_curve:
        return _blind("PumpSwap canonical pool account unreadable")
    return await _fallback(mint, curve_account, rpc)
