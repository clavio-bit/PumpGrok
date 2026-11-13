---
name: risk-audit
description: Absolute safety gate for every Solana token. Runs a fail-closed checklist and returns only CLEAR, CONDITIONAL, or KILL. Used exclusively by the RISK Bot. A KILL cannot be overridden.
bots:
  - bot-05-rug
gates:
  - G-2
required_tools:
  - safety.score_report
  - onchain.authorities
  - onchain.top_holders
required_data:
  - solana_rpc
permissions:
  max_side_effect: read
  network: true
  filesystem: none
---

# Risk Audit

## L1 - Summary
The absolute safety gate for every token. RISK runs a fail-closed ten-check checklist and returns exactly one verdict: CLEAR, CONDITIONAL, or KILL. A KILL ends the token path permanently and cannot be overridden. A FAIL on any required check forces KILL even when the score is high. A WARN does not. Bundler cluster is advisory and does not force KILL. The holder BLIND carve-out does not cover a real FAIL.

## L2 - Method
1. Run every check, every time, with the preferred tool path (operator registry;
`pumpgrok tools <id> --help` prints the schema):
```bash
pumpgrok tools onchain.authorities --help
```
Parse the JSON. If `hasMintAuthority` or `hasFreezeAuthority` is true, that is a strong KILL signal.
2. Mandatory checklist:
   - Mint authority: must be null / revoked.
   - Freeze authority: must be null / revoked.
   - LP status. Open bonding curve: no LP. PumpSwap: burned LP passes, creator-held LP fails, pool-owned LP without a lock expiry is BLIND. Other AMMs are BLIND. Never pass an unread lock.
   - Sell / transfer tax. Required. Classic SPL and Token-2022 without TransferFeeConfig pass at 0. With TransferFeeConfig, use the newer fee when its epoch is active, otherwise the older fee. Warn at 5 percent, hard fail at 10 percent. A fail forces KILL. An unreadable mint or undecodable extension is BLIND and therefore KILL.
   - Honeypot / cannot-sell. Required. Quote-only Jupiter round trip at the intended position: WSOL to the mint, then that token amount back to WSOL. No taker. HTTP error, timeout, or malformed response on either leg is BLIND. No buy route is BLIND. No sell route, or a missing or zero sell outAmount, is FAIL and KILL. Sell price impact above max slippage is a warning only. Never build, execute, submit, or sign.
   - Top-10 holders (ex-LP) concentration: flag above 20-25 %. A fail forces KILL. BLIND is advisory only while no paid RPC provider is recorded.
   - Bundler / insider cluster detection. Advisory. A fail here does not force KILL.
   - Deployer wallet history if visible. A fail forces KILL. BLIND is advisory only while no paid RPC provider is recorded.
   - Token-2022 extensions: document any. A fail forces KILL. A warn does not.
   - Liquidity depth relative to intended size. PumpSwap uses the WSOL vault. Below 10x the intended position fails the row and forces KILL. No PumpSwap pool is BLIND.
3. Emit the output schema:
```
LEAD / TICKET-ID: ...
Verdict: CLEAR | CONDITIONAL | KILL
Residual Risks:
1. ...
Evidence: [source plus UTC]
Conditions (if CONDITIONAL): ...
Self-Audit: All 10 checks completed: YES / NO
```

## Failure modes
- Any check cannot be completed with live data: the verdict is not CLEAR. Incomplete evidence is CONDITIONAL at best, KILL when the gap hides catastrophic risk.
- Narrative or FOMO pressure to soften: never soften a KILL; no other Bot may proceed past one.
- Skipped or approximated check: re-run. The self-audit line must read YES.

## References
- Tools: `onchain.authorities`, `onchain.top_holders` (operator registry); port: `src/pumpgrok/tools/onchain/`
- Gate: G-2; veto: absolute, non-appealable (`desk-operating-model`)
