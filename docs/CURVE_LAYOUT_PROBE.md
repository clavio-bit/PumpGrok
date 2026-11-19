# Bonding-curve layout probe (INT-10, R65)

Status: VERIFIED

The `BondingCurve` prefix is the published IDL struct
`https://github.com/pump-fun/pump-public-docs/blob/main/idl/pump.json`
(account `BondingCurve`, discriminator `sha256("account:BondingCurve")[:8]` =
`17b7f83760d8ac60`). The struct is 166 bytes including the discriminator.
Live accounts of that length were decoded and checked on 2026-11-19.

Field order after the 8-byte discriminator:

1. virtual_token_reserves u64
2. virtual_quote_reserves u64
3. real_token_reserves u64
4. real_quote_reserves u64
5. token_total_supply u64
6. complete bool
7. creator pubkey (bytes 49..81)
8. is_mayhem_mode bool
9. is_cashback_coin bool
10. quote_mint pubkey
11. creator_fee_bps u64
12. can_edit_creator_fee bool
13. is_holder_reward bool
14. creator_fee u64
15. protocol_fees u64
16. depth u8
17. initial_virtual_quote_reserves u64
18. post_complete_base_out u64
19. post_complete_quote_in u64

Quote reserves are lamports only when `quote_mint` is native SOL
(`11111111111111111111111111111111`, 32 zero bytes) or WSOL. Any other quote
is decoded and is not reported as SOL. An incomplete SOL curve whose real
quote exceeds 90 SOL, or whose virtual quote exceeds 200 SOL, is rejected
and the caller goes BLIND.

Captured accounts (raw bytes in `tests/fixtures/rpc/bonding_curve/`):

| Mint | Kind | Quote | complete | Cross-check |
|---|---|---|---|---|
| G9FFfstBYuh2N6Us5iybQQGDQXG8m9D7grYoMqefpump | open | native SOL | false | DexScreener mcap within 0.3 percent at the Jupiter SOL/USDC price |
| HuicCrxFvSEEhMud5DsAd8E7n2S5wsJLJKQZZ7Popump | open | pump token | false | real quote is not SOL; SOL fields stay 0 |
| 5yK1F7qMGfZdhwMHrLsW2oi3Bqf63PmZpG4uhGywpump | open | pump token | false | same |
| HcJGYDM5bS19fDSMUHNcH6wKP68kmHksNmXsTW2Wpump (TOGO) | graduated | native SOL | true | reserves 0; PumpSwap pool scored separately |
| HsprHxBRgiVaQdRf3TnKNuZZtaX9JUSPgMFdAkk6pump (GATO) | graduated | native SOL | true | reserves 0; LP burned on the canonical pool |

On the open SOL capture, virtual quote equals initial virtual quote plus real
quote. That identity also held on the two non-SOL captures, which is how a
quote-token reserve was previously mislabeled as hundreds or thousands of SOL.

Bytes after offset 166 were not present on these accounts. A longer account
still decodes the prefix. The unread tail stays ASSUMED.

The probe script still writes `DECODED_PENDING_REVIEW` for a fresh account
and `ASSUMED` when the account is missing or the bytes do not decode. It does
not flip this status by itself.
