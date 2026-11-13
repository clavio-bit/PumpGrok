"""market.liquidity: curve depth, or PumpSwap quote-vault SOL once the curve is done.

An open bonding curve reports its virtual SOL reserve. A complete curve, or
a curve account that is missing or not the ASSUMED layout, reports the
PumpSwap pool's WSOL vault balance. No PumpSwap pool is not a number from
another AMM. This module does not sign.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import Field

from pumpgrok.core.errors import DataStale
from pumpgrok.core.ids import ambient_correlation
from pumpgrok.core.side_effect import SideEffect
from pumpgrok.core.tool import Tool, ToolInput, ToolOutput, ToolSpec, tool
from pumpgrok.tools._common import J4, SOLANA_RPC_SOURCE, MintStr, meta_now
from pumpgrok.tools.onchain.curve_layout import (
    CurveDecodeError,
    CurveState,
    curve_progress,
    decode_curve_account_base64,
)
from pumpgrok.tools.onchain.pumpswap_layout import WSOL_MINT
from pumpgrok.tools.onchain.pumpswap_pool import lookup_pumpswap_pool
from pumpgrok.tools.onchain.rpc import RpcClient, RpcError


class LiquidityIn(ToolInput):
    """Depth for ``mint`` plus impact for a hypothetical order size."""

    mint: MintStr
    curve_account: MintStr
    order_size_lamports: int = Field(default=0, ge=0)


class LiquidityOut(ToolOutput):
    """Reserves and impact ratios; ``graduated`` mirrors the curve flag."""

    virtual_sol_reserves_lamports: int = Field(ge=0)
    virtual_token_reserves_units: int = Field(ge=0)
    liquidity_lamports: int = Field(ge=0)
    curve_progress: Decimal = Field(ge=0, le=1)
    price_impact: Decimal = Field(ge=0)
    order_to_liquidity_ratio: Decimal = Field(ge=0)
    graduated: bool
    detail: str | None = None
    slot: int | None = None
    pool: str | None = None
    base_vault: str | None = None
    quote_vault: str | None = None
    unscored: bool = False


LIQUIDITY_SPEC = ToolSpec(
    name="market.liquidity",
    description=(
        "Pool depth from the bonding-curve reserves, or from the PumpSwap "
        "WSOL vault once the curve is complete or unreadable."
    ),
    input_model=LiquidityIn,
    output_model=LiquidityOut,
    side_effect=SideEffect.READ,
    timeout_s=10.0,
    retry=J4,
    rate_limit_key=SOLANA_RPC_SOURCE,
    backend="src/pumpgrok/tools/onchain/curve_layout.py via solana_rpc getAccountInfo",
)


def _impact(reserves: int, order_size: int) -> tuple[Decimal, Decimal]:
    order = Decimal(order_size)
    sol = Decimal(reserves)
    impact = order / (sol + order) if order > 0 else Decimal(0)
    ratio = order / sol if sol > 0 else Decimal(0)
    return impact, ratio


def _meta(inp: LiquidityIn, rpc: RpcClient) -> Any:
    return meta_now(
        SOLANA_RPC_SOURCE,
        inp.correlation_id or (str(a) if (a := ambient_correlation()) else None),
        rpc.clock,
    )


async def _curve_state(inp: LiquidityIn, rpc: RpcClient) -> CurveState | None:
    try:
        result = await rpc.call("getAccountInfo", [inp.curve_account, {"encoding": "base64"}])
    except RpcError as exc:
        raise DataStale(f"market.liquidity upstream failed mint={inp.mint}") from exc
    if not isinstance(result, dict) or result.get("value") is None:
        return None
    try:
        return decode_curve_account_base64(result["value"].get("data"))
    except CurveDecodeError:
        return None


def _curve_payload(inp: LiquidityIn, rpc: RpcClient, state: CurveState) -> dict[str, Any]:
    impact, ratio = _impact(state.virtual_sol_reserves_lamports, inp.order_size_lamports)
    return {
        "virtual_sol_reserves_lamports": state.virtual_sol_reserves_lamports,
        "virtual_token_reserves_units": state.virtual_token_reserves_units,
        "liquidity_lamports": state.virtual_sol_reserves_lamports,
        "curve_progress": curve_progress(state),
        "price_impact": impact,
        "order_to_liquidity_ratio": ratio,
        "graduated": state.complete,
        "meta": _meta(inp, rpc),
    }


async def _token_amount(rpc: RpcClient, address: str) -> int:
    try:
        result = await rpc.call("getTokenAccountBalance", [address])
    except RpcError as exc:
        raise DataStale("pumpswap vault balance unreadable") from exc
    value = result.get("value") if isinstance(result, dict) else None
    amount = value.get("amount") if isinstance(value, dict) else None
    if isinstance(amount, str) and amount.isdecimal():
        return int(amount)
    raise DataStale("pumpswap vault balance malformed")


def _unscored(inp: LiquidityIn, rpc: RpcClient, reason: str) -> dict[str, Any]:
    return {
        "virtual_sol_reserves_lamports": 0,
        "virtual_token_reserves_units": 0,  # pragma: allowlist secret
        "liquidity_lamports": 0,
        "curve_progress": Decimal(0),
        "price_impact": Decimal(0),
        "order_to_liquidity_ratio": Decimal(0),
        "graduated": True,
        "detail": reason,
        "unscored": True,
        "meta": _meta(inp, rpc),
    }


async def _pumpswap_payload(inp: LiquidityIn, rpc: RpcClient) -> dict[str, Any]:
    lookup = await lookup_pumpswap_pool(rpc, inp.mint)
    pool = lookup.pool
    if pool is None or pool.quote_mint != WSOL_MINT:
        return _unscored(inp, rpc, lookup.reason or "no PumpSwap WSOL pool")
    try:
        quote = await _token_amount(rpc, pool.quote_vault)
        base = await _token_amount(rpc, pool.base_vault)
    except DataStale:
        return _unscored(inp, rpc, "PumpSwap vault balance unreadable")
    impact, ratio = _impact(quote, inp.order_size_lamports)
    detail = (
        f"pool={pool.address} base_vault={pool.base_vault} "
        f"quote_vault={pool.quote_vault} slot={pool.slot} source={SOLANA_RPC_SOURCE}"
    )
    return {
        "virtual_sol_reserves_lamports": quote,
        "virtual_token_reserves_units": base,  # pragma: allowlist secret
        "liquidity_lamports": quote,
        "curve_progress": Decimal(1),
        "price_impact": impact,
        "order_to_liquidity_ratio": ratio,
        "graduated": True,
        "detail": detail,
        "slot": pool.slot,
        "pool": pool.address,
        "base_vault": pool.base_vault,
        "quote_vault": pool.quote_vault,
        "meta": _meta(inp, rpc),
    }


async def run_liquidity(inp: LiquidityIn, rpc: RpcClient) -> dict[str, Any]:
    """Curve reserves while the curve is open; PumpSwap WSOL depth after that."""
    state = await _curve_state(inp, rpc)
    if state is not None and not state.complete:
        payload = _curve_payload(inp, rpc, state)
    else:
        payload = await _pumpswap_payload(inp, rpc)
    return LiquidityOut.model_validate(payload).model_dump()


def build_liquidity_tool(rpc: RpcClient) -> Tool:
    """Bind ``market.liquidity`` to one injected RPC client (no globals)."""

    @tool(LIQUIDITY_SPEC)
    async def _liquidity(payload: LiquidityIn) -> dict[str, Any]:
        return await run_liquidity(payload, rpc)

    return _liquidity
