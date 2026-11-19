"""Paper exit rules use the operator risk-limits defaults."""

from __future__ import annotations

from pumpgrok.config.settings import ExitsSettings
from pumpgrok.engine.exits import exit_signal
from pumpgrok.engine.models import ExitPosition
from pumpgrok.supervisor.exit_rules import LedgerExitBook

ENTRY = 1_000_000


def _pos(**kwargs: int) -> ExitPosition:
    values = {
        "mint": "mint",
        "entry_price": ENTRY,
        "peak_price": ENTRY,
        "token_amount": 1,
        "opened_at": 0.0,
    }
    values.update(kwargs)
    return ExitPosition(**values)  # type: ignore[arg-type]


def test_paper_exit_book_uses_the_configured_defaults() -> None:
    book = LedgerExitBook(ExitsSettings())
    assert book.exit_config.stop_loss_pct == 20.0
    assert book.exit_config.take_profit_pct == 60.0
    assert book.exit_config.trailing_stop_pct == 25.0
    assert book.exit_config.max_hold_seconds == 1800.0


def test_stop_loss_fires_at_minus_20_percent() -> None:
    cfg = ExitsSettings()
    assert exit_signal(_pos(), int(ENTRY * 0.80), cfg, 10.0).reason == "stop_loss"  # type: ignore[union-attr]
    assert exit_signal(_pos(), int(ENTRY * 0.801), cfg, 10.0) is None


def test_take_profit_fires_at_plus_60_percent() -> None:
    cfg = ExitsSettings()
    signal = exit_signal(_pos(), int(ENTRY * 1.60), cfg, 10.0)
    assert signal is not None
    assert signal.reason == "take_profit"
    assert exit_signal(_pos(), int(ENTRY * 1.599), cfg, 10.0) is None


def test_trailing_stop_fires_at_25_percent_from_the_peak() -> None:
    cfg = ExitsSettings()
    peak = ENTRY * 2
    signal = exit_signal(_pos(peak_price=peak), int(peak * 0.75), cfg, 10.0)
    assert signal is not None
    assert signal.reason == "trailing_stop"
    assert exit_signal(_pos(peak_price=peak), int(peak * 0.76), cfg, 10.0) is None


def test_max_hold_fires_at_1800_seconds() -> None:
    cfg = ExitsSettings()
    # opened_at must be non-zero: the rule treats 0 as "no open time".
    held = _pos(opened_at=1_000.0)
    assert exit_signal(held, ENTRY, cfg, 1_000.0 + 1800.0).reason == "max_hold"  # type: ignore[union-attr]
    assert exit_signal(held, ENTRY, cfg, 1_000.0 + 1799.0) is None
