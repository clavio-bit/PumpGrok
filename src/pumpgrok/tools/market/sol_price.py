"""Quote-only SOL/USDC price from Jupiter Swap V2.

One ``GET`` of the order endpoint, WSOL in and USDC out, for 1 SOL.
No taker, no build, no execute. A missing route or a bad body is ``None``.
"""

from __future__ import annotations

from decimal import Decimal

import httpx

from pumpgrok.core.units import LAMPORTS_PER_SOL
from pumpgrok.tools.market.jupiter import QUOTE_URL, get_quote

WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
_USDC_BASE = Decimal(1_000_000)


async def read_sol_price_usdc(
    client: httpx.AsyncClient,
    *,
    quote_url: str = QUOTE_URL,
    slippage_bps: int = 50,
) -> Decimal | None:
    """USDC per 1 SOL, or ``None`` when the quote cannot be used."""
    quote = await get_quote(
        WSOL_MINT,
        USDC_MINT,
        LAMPORTS_PER_SOL,
        slippage_bps,
        client,
        quote_url=quote_url,
    )
    if not quote.get("ok") or quote.get("noRoute"):
        return None
    raw = quote.get("outAmount")
    if isinstance(raw, int) and not isinstance(raw, bool):
        text = str(raw)
    elif isinstance(raw, str) and raw.isdecimal():
        text = raw
    else:
        return None
    usdc = Decimal(text) / _USDC_BASE
    if usdc <= 0:
        return None
    return usdc
