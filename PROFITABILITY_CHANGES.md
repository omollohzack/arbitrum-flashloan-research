# Profitability changes — v3.6 (2026-11-09)

Research first, changes second. The full sourced research is in the
accompanying report ("Arbitrum Flash-Loan DEX Arbitrage Profitability
2025–2026"). Its blunt conclusion: the textbook WETH/USDC 2-hop flash-loan
route is structurally net-negative for a small solo operator — a 2-hop
route must clear roughly 0.15–0.65% in fees before price impact, while
aggregate cross-rollup arbitrage value is only ~0.03–0.05% of volume, and
the market is contested by on-chain probing contracts and two firms that
won >90% of Timeboost express-lane auctions (Apr–Jul 2025).

**None of these changes guarantees profit.** Their expected effect is
narrower and honest: stop the systematic losses (fixed sizing, 5 bps
flash fee, submitting negative-EV routes, an undeployable contract), so
the bot only acts when a measured, executable edge actually exists.

## What changed

### 1. The contract now compiles — and fits the deploy limit
`contracts/FlashLoanArbitrage.sol`
- `_executeMultiHopSwaps` was split into one internal function per swap
  type (`_swapV2Router`, `_swapV3Router`, `_swapDirectV2Pair`,
  `_swapCamelotV2Pair`, `_swapAlgebraRouter`, dispatched by
  `_executeSwapStep`). The old single function failed solc 0.8.20 with
  "Stack too deep" at the Camelot V2 branch — the contract had never
  compiled under a standard toolchain.
- Revert strings were removed (conditions unchanged) for size.
- Verified 2026-11-09 with solc 0.8.20 (`scripts/compile_contract.mjs`):
  - via-IR + optimizer runs=1 (deployable config): **22,720 bytes**
    deployed, under the 24,576-byte limit, **0 errors, 0 warnings**
    (previously 27,454 bytes via-IR and a hard failure without it).
  - standard compile (runs=200) also passes now, at 27,353 bytes —
    still over the deploy limit, which is why the script pins via-IR.

### 2. Optimal trade sizing — `bot/route_optimizer.py`
`find_optimal_amount()` ternary-searches trade size against an
exact-input quote function, scoring every candidate with the existing
fee-consistent `evaluate_exact_route_profit()`. Profit is unimodal in
size, so the previous fixed size ($1,000 in the deterministic harness,
which lost ~$9–$10 per route) was systematically wrong. If no size is
net-positive, the result is explicitly non-executable.

### 3. Zero-fee flash-loan selection — `bot/route_optimizer.py`
`cheapest_flash_provider()` prefers Balancer V2 (0%) → Morpho Blue (0%)
→ Aave V3 (0.05%), among providers whose *caller-verified* on-chain
liquidity covers the loan. Aave's 5 bps is $5 per $10k — often the whole
margin. Unknown liquidity is treated as unavailable, never assumed.
The contract already supports Balancer and Morpho callbacks; this adds
the off-chain selection logic that was missing.

### 4. Execution gate (EV threshold) — `bot/route_optimizer.py`
`passes_execution_gate()` refuses submission unless net profit is
positive, clears a caller-set minimum (mirroring the contract's on-chain
`minProfit`), and covers the expected revert gas at the caller's
attempts-per-success rate. At the observed 6–12% probe success rate for
L2 cyclic arb, a barely-positive trade is negative-EV after revert
drain; the gate makes that arithmetic explicit instead of hopeful.

### 5. Tests
`tests/test_profitability_improvements.py` adds 7 tests; `scripts/run_bot.sh`
now runs them as step 5/5. Full suite: **38/38 passing** (31 pre-existing
+ 7 new), verified 2026-11-09.

## What did NOT change, and what still blocks real earnings
- The scanner is still **read-only**: nothing signs or broadcasts. That
  is deliberate. Adding an execution path is a separate, explicit
  decision for the owner, after the steps below.
- **Fork/testnet testing has still not been done.** Compiling is not
  deploying, and deploying is not an audit.
- Balancer/Morpho **flash depth on Arbitrum was not verified live**;
  check Vault/Morpho balances for the asset and size before relying on
  the 0% route.
- The route universe is still only 3 pools of major pairs — the segment
  the research rates worst. Widening it (more pools/DEXs, filtered
  long-tail, low-fee 3-hop cycles) is the next engineering step, and it
  needs the gate and sizing above in place first to be measurable.
- An independent security audit remains a prerequisite for real funds,
  per this repo's own REVIEW_NOTES.md.
