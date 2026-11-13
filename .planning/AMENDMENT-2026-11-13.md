# Safety amendment, 2026-11-13

Status: applied in this change. Paper only. Fail-closed. No live execution path, live gate, signer, or wallet code is modified.

## What changed

The honeypot check moves from the advisory set to the required set.

Advisory, still blind by construction and not a KILL on their own:

- transfer_tax
- bundler_cluster

Required, and now backed by a data source:

- honeypot

## Probe

The check calls Jupiter Swap V2 quote-only:

    GET https://api.jup.ag/swap/v2/order

Parameters are only:

- inputMint: the token mint
- outputMint: WSOL So11111111111111111111111111111111111111112
- amount: raw token units
- slippageBps: 50

Raw units are `safety.honeypot_probe_ui_amount` whole tokens (default 1) times `10**decimals`. Decimals come from the mint account through the existing metadata helper. If decimals cannot be read, the check is BLIND.

The configured URL must be HTTPS and its path must be `/swap/v2/order` with an empty query. Any other target is refused before a request is sent.

## Outcomes

- HTTP error, non-200 that does not say no route, timeout, network error, malformed JSON, or `ok: false`: BLIND. A required BLIND forces KILL. Jupiter not responding kills the token.
- Body says no route / route not found, or outAmount is missing or 0: FAIL. A honeypot FAIL forces KILL.
- Route with outAmount > 0: PASS. The row records outAmount, priceImpact, and route labels. High price impact is a warning in the reason and is not a honeypot.

## What this amendment does not do

- It does not pass a taker, user public key, or wallet.
- It does not call build, execute, submit, or any signing endpoint.
- It does not use PaperAdapter `exec.quote` or `exec.simulate` (those are bonding-curve fills, not this check).
- It does not enable live trading, change `LiveGate`, or touch key custody.

## Carriers

- `src/pumpgrok/tools/safety/checks/__init__.py` (`REQUIRED_IDS`, `ADVISORY_IDS`)
- `src/pumpgrok/tools/safety/checks/honeypot.py`
- `src/pumpgrok/tools/safety/score_report.py`
- `src/pumpgrok/config/sections/engine.py` (`safety.honeypot_probe_ui_amount`)
- `docs/RISK_POLICY.md` section 2.4
- `docs/TOOL_CATALOG.md` `safety.score_report`
- `agents/risk.md` checklist item 5 (generated from `prompts/bot-02-risk.xml`)
- `skills/safety-scoring/SKILL.md`
- `skills/risk-audit/SKILL.md`

## Update, same date: position-sized probe and required transfer tax

Status: applied in this change. Paper only. Fail-closed. No live execution path, live gate, signer, or wallet code is modified. This update supersedes the one-token probe size and the advisory status of `transfer_tax` in the section above. `bundler_cluster` stays advisory.

### Probe size

`intended_sol = paper_equity_sol * max_trade_fraction`. At the shipped defaults that is 0.2 SOL, 200000000 lamports. `safety.honeypot_probe_sol` (`PUMPGROK_SAFETY__HONEYPOT_PROBE_SOL`) overrides that size when set. The field is unset by default.

Leg 1, quote only: WSOL to the mint for `intended_sol` lamports. The token `outAmount` is the sell size.

Leg 2, quote only: the mint to WSOL for that raw token amount.

The quote request still uses `slippageBps` 50. It still sends no taker and no public key. The URL guard is unchanged.

### Outcomes

- HTTP error, timeout, or malformed response on either leg: BLIND.
- Buy leg has no route: BLIND, reason `no buy route`. That is not a pass. A buy `outAmount` of 0 or missing is also BLIND, because the sell cannot be sized.
- Sell leg has no route, or sell `outAmount` is missing or 0: FAIL. A honeypot FAIL forces KILL.
- Sell `outAmount` greater than 0: PASS.
- Unreadable mint decimals: BLIND, and Jupiter is not called.

Evidence on the row: buy outAmount, sell outAmount, buy-in lamports, sell price impact, retained fraction (`sell_out / buy_in`, 8 decimal places), and route labels for both legs. If the absolute sell price impact exceeds `limits.max_slippage_bps` (default 300), the reason adds `position-size sell impact exceeds max slippage`. The status stays PASS. Whether that warning should fail is a later policy decision.

### Transfer tax

`transfer_tax` moves from `ADVISORY_IDS` to `REQUIRED_IDS`. The check reads the mint with `getAccountInfo` `jsonParsed`.

- Classic SPL Token mint (`TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA`): PASS, value 0.
- Token-2022 mint (`TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb`) with no `transferFeeConfig` extension: PASS, value 0.
- `transferFeeConfig` present: effective basis points are the newer schedule when `newer.epoch <=` the current epoch from `getEpochInfo`, otherwise the older schedule. This matches spl-token-2022 `TransferFeeConfig::get_epoch_fee`. The reason records both schedules and both `maximumFee` values. The value is the effective percent (basis points / 100). Warn at `transfer_tax_warn_pct` 5. Fail at `transfer_tax_hard_fail_pct` 10.
- Mint missing, not a mint, RPC error, extension present but undecodable, or epoch unreadable: BLIND. A required BLIND forces KILL.

A transfer-tax fail subtracts 25 points. It does not by itself force KILL the way a honeypot fail does.

### What this update does not do

- It does not pass a taker, user public key, or wallet.
- It does not call build, execute, submit, or any signing endpoint.
- It does not enable live trading, change `LiveGate`, or touch key custody.
- It does not score non-PumpSwap liquidity. That is a separate change.
