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

## Update, same date: PumpSwap LP and quote-side liquidity

Status: applied in this change. Paper only. Fail-closed. No live execution path, live gate, signer, or wallet code is modified.

`lp_status` and `liquidity_depth` only read the ASSUMED bonding-curve layout, so a graduated mint was blind and therefore KILL. This update scores a PumpSwap pool and leaves every other AMM blind.

### How a mint is found

Program `pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA`. `getProgramAccounts` with `memcmp` at offset 43, the `base_mint` field. That filter returned one account for TOGO `HcJGYDM5bS19fDSMUHNcH6wKP68kmHksNmXsTW2Wpump`: pool `AeV3q7n6bHcSmWuf4vfKSTPtWbn59y5wNnqLQCvNwnaF`. The captured account is `tests/fixtures/rpc/pumpswap/togo_pool.json` with its slot. Offset 43 is the TOGO mint. Offset 75 is WSOL. The two vaults are token accounts of those mints. The LP mint authority is the pool.

The discriminator is `sha256("account:Pool")[:8]`, which matches the account. The field order through `coin_creator` matches the `pool` struct in https://github.com/pump-fun/pump-public-docs/blob/main/idl/pump_amm.ts. Bytes from offset 244 to the end of the 301-byte account are not decoded. The IDL lists more fields there. They are not separated from padding on this account, so that tail is ASSUMED and unused. A short account or a bad discriminator does not decode, and the check is BLIND.

An open bonding curve is unchanged and does not look up a pool. A complete curve, or a curve account that is missing or does not decode, does.

### LP

Burned means the LP mint supply is 0 while the pool still records `lp_supply`, or the whole mint supply sits at `1nc1nerator11111111111111111111111111111111` or the System Program. That passes, because a burn does not unlock, so it meets `lp_lock_min_days`. LP held by the pool creator or the coin creator fails. `coin_creator` may be the all-zero pubkey. That still decodes. LP that sits on the pool with no lock expiry, an unrecognized holder, a holder sample that does not cover the supply, or more than one WSOL pool is BLIND. No PumpSwap pool is BLIND with the reason that other AMMs are not scored. The row records the pool address, both vaults, the slot, and `solana_rpc`.

### Liquidity

Quote-side liquidity is the WSOL vault balance in lamports. It is compared with the intended position: `intended_size_lamports` when that is positive, otherwise the honeypot probe size from the same derivation as the position-sized probe. Below `liquidity_multiple_min` (10) fails. Below `liquidity_multiple_recommended` (20) warns. A vault that cannot be read is BLIND.

### What this update does not do

- It does not decode Raydium, Meteora, or any other AMM.
- It does not treat the curve tail or the PumpSwap tail as verified.
- It does not sign, build, or submit a transaction, and it does not change the live gate or wallet code.

## Update, same date: canonical pool PDA and host-scoped RPC key

Status: applied in this change. Paper only. Fail-closed. No live execution path, live gate, signer, or wallet code is modified.

Helius rejects `getProgramAccounts` on the PumpSwap program with JSON-RPC -32600 (`Too many accounts requested`). `lp_status` and `liquidity_depth` were then BLIND for every graduated mint on that RPC.

### Pool lookup

The primary lookup is the canonical migration pool PDA, then one `getAccountInfo`. Seeds were checked against https://github.com/pump-fun/pump-public-docs `idl/pump_amm.ts` (`createPool` seeds `["pool", index, creator, baseMint, quoteMint]`) and `idl/pump.json` (`migrate` / `migrate_v2`). The pool-authority PDA is `["pool-authority", mint]` on pump program `6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P`. Migration encodes the index as the constant bytes `[0, 0]` (u16 little-endian 0). The creator is that pool-authority PDA. The quote mint on `migrate` is WSOL. The AMM program is `pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA`.

Those seeds reproduce both mainnet pools, offline:

- TOGO `HcJGYDM5bS19fDSMUHNcH6wKP68kmHksNmXsTW2Wpump` -> `AeV3q7n6bHcSmWuf4vfKSTPtWbn59y5wNnqLQCvNwnaF`
- GATO `HsprHxBRgiVaQdRf3TnKNuZZtaX9JUSPgMFdAkk6pump` -> `5E3dXLXGwmTDCokA51WYYaKttjFZBwqCoyUHBUi5gt2m`

The derived account must be owned by the AMM program, carry the Pool discriminator, and match the mint and WSOL. A hit is the migration pool. A missing or invalid account falls back to `getProgramAccountsV2` with pagination and the same memcmp at offset 43, when that method is offered. Otherwise the fallback is `getProgramAccounts`. More than one WSOL pool on a fallback is BLIND. Nothing found is BLIND. A scan error, including -32600 on the legacy method, is BLIND and does not crash. The check reason records `lookup=canonical_pda`, `lookup=getProgramAccountsV2`, or `lookup=getProgramAccounts`.

The pool tail after offset 244 stays ASSUMED and unused. The seeds themselves are verified, not ASSUMED.

### RPC key scope

`x-api-key` was verified on Helius on 2026-11-13: HTTP 200 with the header, 401 without. `rpc.api_key_hosts` (`PUMPGROK_RPC__API_KEY_HOSTS`) lists the hosts that may receive it. When the list is empty and a key is set, only the first endpoint receives the header. No other host is sent the key. Endpoint URLs stay keyless. Errors and logs do not carry the URL or the header value.

### What this update does not do

- It does not change live execution, the live gate, signing, or wallet code.
- It does not put the key in a URL.
- It does not score a pool it did not validate.
