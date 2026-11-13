"""onchain.lp_status: curve completion, then a PumpSwap pool when the curve is done.

An open bonding curve has no LP to lock. A complete curve, or a curve
account that is missing or not the ASSUMED layout, is scored from the
PumpSwap pool when one exists. No PumpSwap pool is not a pass: other AMMs
stay blind. This module does not sign.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from pumpgrok.core.errors import DataStale
from pumpgrok.core.ids import ambient_correlation
from pumpgrok.core.side_effect import SideEffect
from pumpgrok.core.tool import Tool, ToolInput, ToolOutput, ToolSpec, tool
from pumpgrok.tools._common import J5, SOLANA_RPC_SOURCE, MintStr, meta_now
from pumpgrok.tools.onchain.curve_layout import (
    CurveDecodeError,
    CurveState,
    decode_curve_account_base64,
)
from pumpgrok.tools.onchain.pumpswap_pool import lookup_pumpswap_pool
from pumpgrok.tools.onchain.rpc import RpcClient, RpcError


class LpStatusIn(ToolInput):
    """Liquidity stage for ``mint`` from its curve account."""

    mint: MintStr
    curve_account: MintStr


class LpStatusOut(ToolOutput):
    """Stage plus completion flag; LP-mint fields are unwired (null)."""

    stage: Literal["bonding_curve", "graduated", "pumpswap", "unknown"]
    curve_complete: bool | None = None
    lp_mint: str | None = None
    lp_burned_pct: float | None = None
    lp_locked_pct: float | None = None
    locker: str | None = None
    lock_expires_at: datetime | None = None
    checked: list[str] = Field(default_factory=list)
    detail: str | None = None
    slot: int | None = None
    pool: str | None = None
    base_vault: str | None = None
    quote_vault: str | None = None
    lp_mint_supply: int | None = None
    pool_lp_supply: int | None = None
    holders: list[dict[str, str | int]] = Field(default_factory=list)
    creator: str | None = None
    coin_creator: str | None = None


LP_STATUS_SPEC = ToolSpec(
    name="onchain.lp_status",
    description=(
        "Liquidity stage from the bonding curve, or from the PumpSwap pool "
        "once the curve is complete or unreadable. Other AMMs stay blind."
    ),
    input_model=LpStatusIn,
    output_model=LpStatusOut,
    side_effect=SideEffect.READ,
    timeout_s=20.0,
    retry=J5,
    rate_limit_key=SOLANA_RPC_SOURCE,
    backend="src/pumpgrok/tools/onchain/curve_layout.py via solana_rpc getAccountInfo",
)


def _meta(inp: LpStatusIn, rpc: RpcClient) -> Any:
    return meta_now(
        SOLANA_RPC_SOURCE,
        inp.correlation_id or (str(a) if (a := ambient_correlation()) else None),
        rpc.clock,
    )


async def _curve_state(inp: LpStatusIn, rpc: RpcClient) -> CurveState | None:
    """The curve when it decodes. Transport errors still raise ``DataStale``."""
    try:
        result = await rpc.call("getAccountInfo", [inp.curve_account, {"encoding": "base64"}])
    except RpcError as exc:
        raise DataStale(f"onchain.lp_status upstream failed mint={inp.mint}") from exc
    if not isinstance(result, dict) or result.get("value") is None:
        return None
    try:
        return decode_curve_account_base64(result["value"].get("data"))
    except CurveDecodeError:
        return None


def _open_curve(inp: LpStatusIn, rpc: RpcClient) -> dict[str, Any]:
    return {
        "stage": "bonding_curve",
        "curve_complete": False,
        "lp_mint": None,
        "lp_burned_pct": None,
        "lp_locked_pct": None,
        "locker": None,
        "lock_expires_at": None,
        "checked": ["curve_account"],
        "meta": _meta(inp, rpc),
    }


def _amount(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return None


async def _lp_supply(rpc: RpcClient, lp_mint: str) -> int | None:
    try:
        result = await rpc.call("getAccountInfo", [lp_mint, {"encoding": "jsonParsed"}])
    except RpcError as exc:
        raise DataStale("pumpswap LP mint unreadable") from exc
    if not isinstance(result, dict) or not isinstance(result.get("value"), dict):
        return None
    data = result["value"].get("data")
    parsed = data.get("parsed") if isinstance(data, dict) else None
    info = parsed.get("info") if isinstance(parsed, dict) else None
    if not isinstance(info, dict):
        return None
    return _amount(info.get("supply"))


async def _holders(rpc: RpcClient, lp_mint: str) -> list[dict[str, str | int]] | None:
    try:
        result = await rpc.call("getTokenLargestAccounts", [lp_mint])
    except RpcError as exc:
        raise DataStale("pumpswap LP holders unreadable") from exc
    rows = result.get("value") if isinstance(result, dict) else None
    if not isinstance(rows, list):
        return None
    holders: list[dict[str, str | int]] = []
    for row in rows:
        if not isinstance(row, dict):
            return None
        address = row.get("address")
        amount = _amount(row.get("amount"))
        if not isinstance(address, str) or amount is None:
            return None
        try:
            info = await rpc.call("getAccountInfo", [address, {"encoding": "jsonParsed"}])
        except RpcError as exc:
            raise DataStale("pumpswap LP holder account unreadable") from exc
        owner = _token_owner(info)
        if owner is None:
            return None
        holders.append({"owner": owner, "amount": amount})
    return holders


def _token_owner(result: Any) -> str | None:
    if not isinstance(result, dict) or not isinstance(result.get("value"), dict):
        return None
    data = result["value"].get("data")
    parsed = data.get("parsed") if isinstance(data, dict) else None
    info = parsed.get("info") if isinstance(parsed, dict) else None
    if not isinstance(info, dict):
        return None
    owner = info.get("owner")
    if isinstance(owner, str) and owner:
        return owner
    return None


async def _pumpswap_payload(
    inp: LpStatusIn, rpc: RpcClient, complete: bool | None
) -> dict[str, Any]:
    lookup = await lookup_pumpswap_pool(rpc, inp.mint)
    pool = lookup.pool
    if pool is None:
        return {
            "stage": "unknown",
            "curve_complete": complete,
            "lp_mint": None,
            "lp_burned_pct": None,
            "lp_locked_pct": None,
            "locker": None,
            "lock_expires_at": None,
            "checked": ["curve_account", "pumpswap"],
            "detail": lookup.reason,
            "meta": _meta(inp, rpc),
        }
    holders: list[dict[str, str | int]] = []
    try:
        supply = await _lp_supply(rpc, pool.lp_mint)
        if supply is not None and supply > 0:
            found = await _holders(rpc, pool.lp_mint)
            if found is None:
                supply = None
            else:
                holders = found
    except DataStale:
        supply = None
    detail = (
        f"pool={pool.address} base_vault={pool.base_vault} "
        f"quote_vault={pool.quote_vault} slot={pool.slot} source={SOLANA_RPC_SOURCE}"
    )
    return {
        "stage": "pumpswap",
        "curve_complete": complete,
        "lp_mint": pool.lp_mint,
        "lp_burned_pct": None,
        "lp_locked_pct": None,
        "locker": None,
        "lock_expires_at": None,
        "checked": ["pumpswap_pool", "lp_mint"],
        "detail": detail,
        "slot": pool.slot,
        "pool": pool.address,
        "base_vault": pool.base_vault,
        "quote_vault": pool.quote_vault,
        "lp_mint_supply": supply,
        "pool_lp_supply": pool.lp_supply,
        "holders": holders,
        "creator": pool.creator,
        "coin_creator": pool.coin_creator,
        "meta": _meta(inp, rpc),
    }


async def run_lp_status(inp: LpStatusIn, rpc: RpcClient) -> dict[str, Any]:
    """Score an open curve, or the PumpSwap pool once the curve is done or unreadable."""
    state = await _curve_state(inp, rpc)
    if state is not None and not state.complete:
        return LpStatusOut.model_validate(_open_curve(inp, rpc)).model_dump()
    complete = None if state is None else state.complete
    payload = await _pumpswap_payload(inp, rpc, complete)
    return LpStatusOut.model_validate(payload).model_dump()


def build_lp_status_tool(rpc: RpcClient) -> Tool:
    """Bind ``onchain.lp_status`` to one injected RPC client."""

    @tool(LP_STATUS_SPEC)
    async def _lp_status(payload: LpStatusIn) -> dict[str, Any]:
        return await run_lp_status(payload, rpc)

    return _lp_status
