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
