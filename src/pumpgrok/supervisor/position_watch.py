"""Poll open paper positions from the supervisor tick. Paper only.

Each due poll reads at most ``max_per_poll`` open positions through
:func:`watch_open_position`. A trigger appends a paper ``rug_alert`` and
asks the paper closer to sell. Nothing here signs or sends a transaction.
A read that fails is logged and does not move the fixture clock.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from pumpgrok.ledger.reducer import PositionView, initial_state, reduce
from pumpgrok.ledger.store import LedgerStore
from pumpgrok.replay.clock import Clock
from pumpgrok.tools.onchain.curve_accounts import bonding_curve_address
from pumpgrok.tools.onchain.pumpswap_pda import PdaError
from pumpgrok.tools.onchain.rpc import RpcClient
from pumpgrok.tools.safety.position_watch import (
    WatchReport,
    WatchSnapshot,
    append_paper_watch,
    watch_open_position,
)

log = logging.getLogger("pumpgrok.supervisor.position_watch")

ClosePaper = Callable[..., Awaitable[dict[str, Any]]]
WatchFn = Callable[..., Awaitable[WatchReport]]


def open_positions(store: LedgerStore) -> list[PositionView]:
    """Open paper positions in ledger order. Money stays on the views."""
    state = initial_state(source="paper", initial_equity_lamports=0, day_utc="")
    for event in store.read(limit=None):
        state = reduce(state, event)
    return [pos for pos in state.positions.values() if pos.token_units > 0]


class PaperPositionPoller:
    """Rate-limited paper watch. ``interval_s`` is measured on the injected clock."""

    def __init__(
        self,
        *,
        store: LedgerStore,
        clock: Clock,
        rpc: RpcClient | None = None,
        close: ClosePaper | None = None,
        interval_s: float = 30.0,
        max_per_poll: int = 1,
        watch: WatchFn = watch_open_position,
    ) -> None:
        self._store = store
        self._clock = clock
        self._rpc = rpc
        self._close = close
        self._interval_s = interval_s
        self._max_per_poll = max(0, max_per_poll)
        self._watch = watch
        self._last: float | None = None
        self._cursor = 0
        self._snapshots: dict[str, WatchSnapshot] = {}

    def due(self, now: float) -> bool:
        if self._max_per_poll == 0:
            return False
        if self._last is None:
            return True
        return now - self._last >= self._interval_s

    async def poll(self, now: float) -> list[str]:
        """Watch the next batch of open positions. Returns mints that signaled.

        The interval gate runs even when nothing is open, so a quiet book does
        not poll on every tick.
        """
        if not self.due(now):
            return []
        self._last = now
        positions = open_positions(self._store)
        if not positions or self._rpc is None:
            return []
        start = self._cursor % len(positions)
        batch = [
            positions[(start + offset) % len(positions)] for offset in range(self._max_per_poll)
        ]
        self._cursor = start + len(batch)
        signaled: list[str] = []
        for pos in batch:
            if await self._one(pos):
                signaled.append(pos.mint)
        return signaled

    async def _one(self, pos: PositionView) -> bool:
        try:
            curve = bonding_curve_address(pos.mint)
            report = await self._watch(
                mint=pos.mint,
                curve_account=curve,
                rpc=self._rpc,
                creator=pos.creator or None,
                snapshot=self._snapshots.get(pos.mint),
            )
        except (PdaError, Exception) as exc:
            log.warning(
                "paper position watch failed mint=%s error=%s",
                pos.mint[:8],
                type(exc).__name__,
            )
            return False
        if report.signal is None:
            return False
        append_paper_watch(self._store, self._clock, report)
        if self._close is not None:
            await self._close(mint=pos.mint, ticket_id=pos.ticket_id, reason="rug_alert")
        self._snapshots.pop(pos.mint, None)
        return True
