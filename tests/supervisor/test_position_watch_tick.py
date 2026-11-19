"""The supervisor tick polls open paper positions on a rate limit."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pumpgrok.ledger.store import LedgerStore, recreate_ledger_db
from pumpgrok.replay.clock import FixtureClock, format_ts_utc
from pumpgrok.replay.rng import make_rng
from pumpgrok.supervisor.position_watch import PaperPositionPoller
from pumpgrok.supervisor.scheduler import ROSTER, Supervisor
from pumpgrok.tools.safety.position_watch import PaperExitSignal, WatchReport, WatchTrigger

MINT = "G9FFfstBYuh2N6Us5iybQQGDQXG8m9D7grYoMqefpump"
START = 1_700_000_000.0


def _store(tmp_path: Path) -> LedgerStore:
    path = tmp_path / "ledger.sqlite"
    recreate_ledger_db(path)
    store = LedgerStore(str(path))
    store.set_meta("source", "paper")
    store.set_meta("initial_equity_lamports", "10000000000")
    return store


def _open(store: LedgerStore, clock: FixtureClock) -> None:
    store.append(
        ts_utc=format_ts_utc(clock.now()),
        source="paper",
        correlation_id="test",
        idempotency_key="open-1",
        type="PositionOpened",
        payload={
            "source": "paper",
            "mint": MINT,
            "symbol": "G9",
            "creator": "H3D1X1mushAjMYTSHp447hgqkfPCLrRKAzUMEg3CmmUs",
            "ticket_id": "SOL-20260201-001",
            "price_entry": "0.00000001",
            "token_units": 1000,
            "cost_basis_lamports": 1_000_000,
            "opened_ts_utc": format_ts_utc(clock.now()),
        },
    )


async def _cycles(_ctx: Any) -> dict[str, bool]:
    return {"ok": True}


async def test_supervisor_tick_rate_limits_the_watch(tmp_path: Path) -> None:
    clock = FixtureClock(start=START)
    store = _store(tmp_path)
    calls: list[float] = []

    async def _watch(now: float) -> None:
        calls.append(now)

    try:
        supervisor = Supervisor(
            store=store,
            clock=clock,
            rng=make_rng(1),
            cycles=dict.fromkeys(ROSTER, _cycles),
            position_watch=_watch,
            watch_interval_s=30.0,
        )
        await supervisor.run_once()
        await supervisor.run_once()
        assert calls == [START]
        clock.advance_to(START + 30.0)
        await supervisor.run_once()
        assert calls == [START, START + 30.0]
    finally:
        store.close()


async def test_poller_writes_a_paper_rug_alert_and_asks_for_a_paper_close(tmp_path: Path) -> None:
    clock = FixtureClock(start=START)
    store = _store(tmp_path)
    _open(store, clock)
    closed: list[tuple[str, str]] = []

    async def _watch(**_kwargs: Any) -> WatchReport:
        return WatchReport(
            triggers=[WatchTrigger("lp_change", "creator holds the LP")],
            signal=PaperExitSignal(mint=MINT, reason="rug_alert"),
        )

    async def _close(*, mint: str, ticket_id: str, reason: str) -> dict[str, str]:
        closed.append((mint, reason))
        assert ticket_id == "SOL-20260201-001"
        return {"status": "closed"}

    try:
        poller = PaperPositionPoller(
            store=store,
            clock=clock,
            rpc=object(),  # type: ignore[arg-type]
            close=_close,
            interval_s=0.0,
            max_per_poll=1,
            watch=_watch,
        )
        assert await poller.poll(clock.now()) == [MINT]
        kinds = [row["payload"]["kind"] for row in store.read(types=["Escalation"], limit=None)]
        assert kinds == ["rug_alert"]
        assert closed == [(MINT, "rug_alert")]
    finally:
        store.close()
