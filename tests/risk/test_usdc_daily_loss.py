"""Daily-loss halt is 5 percent of the 100 USDC bankroll, not 10 SOL."""

from __future__ import annotations

import random
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from pumpgrok.core.adapter import OrderRequest
from pumpgrok.core.ids import IdFactory
from pumpgrok.core.risk_budget import RiskBudget
from pumpgrok.ledger.store import LedgerStore, recreate_ledger_db
from pumpgrok.replay.clock import FixtureClock, format_ts_utc
from pumpgrok.replay.rng import make_rng
from pumpgrok.risk.engine import RiskEngine

START = datetime(2026, 10, 21, 0, 0, 0, tzinfo=UTC).timestamp()
EQUITY = 10_000_000_000
TICKET = "SOL-20260201-001"
MINT = "So11111111111111111111111111111111111111112"


def _store(tmp_path: Path) -> LedgerStore:
    path = tmp_path / "ledger.sqlite"
    recreate_ledger_db(path)
    store = LedgerStore(str(path))
    store.set_meta("initial_equity_lamports", str(EQUITY))
    store.set_meta("source", "paper")
    return store


def _engine(store: LedgerStore, clock: FixtureClock, price: Decimal | None) -> RiskEngine:
    return RiskEngine(
        store=store,
        budget=RiskBudget(),
        ids=IdFactory(clock, random.Random(3)),
        clock=clock,
        autonomy="autonomous-paper",
        env={},
        get_reserves=lambda _mint: (42_000_000_000, 764_000_000_000_000),
        usdc_sizing=True,
        sol_price_usdc=price,
    )


def _buy(clock: FixtureClock, ids: IdFactory) -> OrderRequest:
    return OrderRequest(
        ticket_id=TICKET,
        mint=MINT,
        side="buy",
        decision_id=ids.ulid(),
        size_lamports=10_000_000,
        max_slippage_bps=50,
        approval_id=f"apr-{ids.ulid()}",
        correlation_id="test",
    )


def _fill(store: LedgerStore, clock: FixtureClock, ids: IdFactory, cash_delta: int) -> None:
    store.append(
        ts_utc=format_ts_utc(clock.now()),
        source="paper",
        correlation_id="test",
        idempotency_key=f"fill-{ids.ulid()}",
        type="FillRecorded",
        payload={
            "source": "paper",
            "order_id": ids.ulid(),
            "decision_id": ids.ulid(),
            "side": "sell",
            "mint": MINT,
            "filled_lamports": 0,
            "filled_units": 0,
            "filled_fraction": "1",
            "price_avg": "0.00001",
            "venue_fee_lamports": 0,
            "network_cost_lamports": 0,
            "cash_delta_lamports": cash_delta,
            "latency_s": "1.0",
            "venue_tx_ref": "",
        },
    )


def test_five_usdc_loss_halts_and_a_five_percent_sol_loss_does_not(tmp_path: Path) -> None:
    clock = FixtureClock(start=START)
    store = _store(tmp_path)
    try:
        ids = IdFactory(clock, make_rng(3))
        # 0.05 SOL at 100 USDC/SOL is 5 USDC, exactly 5 percent of the 100 USDC bankroll.
        # It is 0.5 percent of the 10 SOL ledger, so the old baseline would not halt.
        _fill(store, clock, ids, cash_delta=-50_000_000)
        halted = _engine(store, clock, Decimal("100"))
        decision = halted.check(_buy(clock, ids), seat="bot-04-sniper")
        assert decision.reason == "daily_loss_halt"
        assert len(store.read(types=["HaltSet"], limit=None)) == 1
    finally:
        store.close()

    clock = FixtureClock(start=START)
    store = _store(tmp_path)
    try:
        ids = IdFactory(clock, make_rng(4))
        # 0.5 SOL is 5 percent of 10 SOL and 0.5 USDC at 1 USDC/SOL.
        _fill(store, clock, ids, cash_delta=-500_000_000)
        engine = _engine(store, clock, Decimal("1"))
        decision = engine.check(_buy(clock, ids), seat="bot-04-sniper")
        assert decision.passed is True
        assert store.read(types=["HaltSet"], limit=None) == []
    finally:
        store.close()


def test_missing_sol_price_refuses_the_entry_and_writes_no_halt(tmp_path: Path) -> None:
    clock = FixtureClock(start=START)
    store = _store(tmp_path)
    try:
        ids = IdFactory(clock, make_rng(5))
        engine = _engine(store, clock, None)
        decision = engine.check(_buy(clock, ids), seat="bot-04-sniper")
        assert decision.passed is False
        assert decision.reason == "max_trade_fraction"
        assert engine.evaluate_halts() == "price_unavailable"
        assert store.read(types=["HaltSet"], limit=None) == []
    finally:
        store.close()
