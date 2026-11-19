"""USDC paper bankroll converted to lamports at a live SOL price.

Source: the operator risk-limits note (2026-11-19). The paper bankroll is
100 USDC and one trade is 2 percent of it (2 USDC). The SOL ledger equity
(``paper_equity_sol``) is a separate replay figure. This module does not
invent a SOL price: a missing or non-positive price returns ``None``, and
the caller stays BLIND.
"""

from __future__ import annotations

from decimal import Decimal

from pumpgrok.core.units import sol_to_lamports

# Operator risk-limits note, 2026-11-19.
DEFAULT_PAPER_BANKROLL_USDC = Decimal("100")
DEFAULT_PER_TRADE_FRACTION = Decimal("0.02")


def trade_lamports_from_usdc(
    *,
    bankroll_usdc: Decimal,
    fraction: Decimal,
    sol_price_usdc: Decimal | None,
) -> int | None:
    """Lamports for ``bankroll_usdc * fraction`` at ``sol_price_usdc``.

    ``None`` when the price is missing or the notional is not positive.
    Callers treat ``None`` as BLIND and do not substitute a SOL default.
    """
    if sol_price_usdc is None or sol_price_usdc <= 0:
        return None
    if bankroll_usdc <= 0 or fraction <= 0:
        return None
    sol = (bankroll_usdc * fraction) / sol_price_usdc
    if sol <= 0:
        return None
    return sol_to_lamports(sol)
