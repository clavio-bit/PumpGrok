# RISK rule parity

Paper only. Each row is a rule from the operator risk-limits note and the
desk safety constitution, the code that enforces it, and the test that
fails if the rule is dropped. Live execution, signing, and key custody are
out of this table.

Source of the size and exit numbers: operator risk-limits note, 2026-11-19.
Bankroll 100 USDC paper, per-trade 2 percent (2 USDC), max slippage 500 bps,
daily loss halt 5 percent, exits SL -20 percent, TP +60 percent, trailing
25 percent, max hold 1800 s. Overrides are `PUMPGROK_*` env vars.

| Rule | Code | Test |
|---|---|---|
| Required checks. A BLIND or FAIL on a required id is KILL | `REQUIRED_IDS` in `src/pumpgrok/tools/safety/checks/__init__.py`; `_kill_facts` and `_verdict` in `src/pumpgrok/tools/safety/score_report.py` | `tests/tools/safety/test_score_report.py`; `tests/safety/test_blind_advisory.py` |
| With `paid_rpc_provider` recorded, top10 or deployer BLIND is KILL. `none` keeps that pair advisory | `paid_rpc_provider_recorded` in `src/pumpgrok/tools/safety/provider.py`; `HOLDER_ADVISORY_PAIR` | `tests/safety/test_blind_advisory.py`; `tests/safety/test_risk_parity.py` (record is `helius`) |
| A required WARN does not by itself force KILL | `_kill_facts` ignores `warn` | `tests/tools/safety/test_score_report.py::test_required_warn_does_not_force_kill` |
| Bundler BLIND is at most CONDITIONAL, and only when honeypot, lp_status, and liquidity_depth are live passes. Otherwise KILL | `_apply_paper_caps` in `src/pumpgrok/tools/safety/score_report.py` | `tests/safety/test_risk_parity.py` |
| Deployer WARN (serial launcher, or a truncated scan) is at most CONDITIONAL | `_apply_paper_caps`; `check_deployer_history` in `src/pumpgrok/tools/safety/checks/readers.py` | `tests/safety/test_risk_parity.py` |
| Deployer history paginates. A budget hit says `at least N` and sets `truncated`. It never returns a silent 100 | `src/pumpgrok/tools/onchain/deployer_history.py` | `tests/tools/onchain/test_deployer_pagination.py` |
| Creator comes from the curve account (bytes 49..81) or the PumpSwap `coin_creator` when the caller omits it | `_resolve_creator` in `src/pumpgrok/tools/safety/score_report.py` | `tests/tools/onchain/test_curve_fixtures.py`; live score artifacts |
| Slippage cap default 500 bps | `EngineLimits.max_slippage_bps` in `src/pumpgrok/risk/limits.py`; paper overlay `[limits]` | `tests/config/test_settings_surface_characterization.py` |
| Trade size is 100 USDC times 2 percent, converted at a live SOL/USDC price. No price is BLIND, not 0.2 SOL | `trade_lamports_from_usdc` in `src/pumpgrok/core/bankroll.py`; `read_sol_price_usdc` in `src/pumpgrok/tools/market/sol_price.py`; honeypot resolve in `src/pumpgrok/tools/safety/checks/honeypot.py`; `RiskEngine` `usdc_sizing` in `src/pumpgrok/risk/engine/steps.py` | `tests/tools/safety/test_honeypot.py` |
| Daily loss halt is 5 percent of the 100 USDC bankroll. The loss is realised plus unrealised P&L converted at the live SOL price. A missing price refuses new entries and does not use the 10 SOL ledger. Replay without USDC sizing still uses the lamport baseline so fixture hashes stay stable | `_detect_breach` in `src/pumpgrok/risk/engine/steps.py`; `daily_loss_exceeds_bankroll` in `src/pumpgrok/core/bankroll.py` | `tests/risk/test_usdc_daily_loss.py`; `tests/integrity/test_daily_loss_integer.py` |
| Exits: SL 20, TP 60, trail 25, hold 1800 s, enforced by the paper exit watcher | `ExitsSettings` in `src/pumpgrok/config/sections/engine.py`; `exit_signal` in `src/pumpgrok/engine/exits.py`; `ExitRules` | `tests/engine/test_exit_defaults.py`; `tests/replay/test_replay_exits.py` |
| Open paper positions are polled from the supervisor tick. Interval `safety.position_watch_interval_s` (30 s) and at most `position_watch_max_per_poll` (1) reads per poll. A trigger is a paper `rug_alert` plus a paper close | `PaperPositionPoller` in `src/pumpgrok/supervisor/position_watch.py`; `_poll_open_positions` in `src/pumpgrok/supervisor/scheduler/supervisor.py`; wired from `run_once_replay` and serve | `tests/supervisor/test_position_watch_tick.py` |
| Open curve is not migrated. Progress is real SOL over 85 SOL when the quote is SOL | `check_lp_status` in `src/pumpgrok/tools/safety/checks/readers.py`; `curve_progress` | `tests/tools/onchain/test_curve_fixtures.py`; `artifacts/score-g9ff.json` |
| Liquidity depth on an open SOL curve is real SOL versus the probe size. A non-SOL quote is BLIND | `run_liquidity` in `src/pumpgrok/tools/market/liquidity.py` | `tests/tools/market/test_market_reads.py`; live score artifacts |
| Top 10 excludes the curve PDA and its token account | `curve_token_accounts` in `src/pumpgrok/tools/onchain/curve_accounts.py`; `exclusion_reason` | `tests/tools/onchain/test_curve_fixtures.py` |
| Incomplete SOL real reserves above 90 SOL, or virtual reserves above 200 SOL, are rejected (BLIND), not reported | `_check_quote_bounds` in `src/pumpgrok/tools/onchain/curve_layout.py` | `tests/tools/onchain/test_curve_layout.py` |
| Paper position watch: creator sell or transfer, LP fail, mint or freeze authority set, liquidity drop, honeypot re-probe. The signal is a paper `rug_alert`. No live send | `src/pumpgrok/tools/safety/position_watch.py` | `tests/tools/safety/test_position_watch.py` |

The supervisor tick calls the poller. Replay fixture cycles attach it too.
A failed read does not write a row and does not move the fixture clock.
The replay risk engine leaves USDC sizing off, so its daily-loss check
stays on lamport equity and the pinned replay hashes do not move. The
operator paper engine (`usdc_sizing`) measures the halt in USDC.

`bundler_cluster` still has no data source, so it is BLIND by construction.
Under the cap above, a paper report cannot be CLEAR while that check is blind.
