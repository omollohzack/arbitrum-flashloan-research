# Long-Tail Pair Scan — Results (18 Nov 2026)

Question (Yuqi7, dev.to, 15 Nov): had the scanner been tried on long-tail
pairs yet? Now it has.

## Method

- Repo: omollohzack/arbitrum-flashloan-research, main v3.6 (29e02de), suite 38/38.
- Scanner: bot/opportunity_scanner.py (ArbitrumReadOnlyScanner), pinned-block
  quotes, Multicall3 batching, same accounting as the published study:
  Balancer 0 bps flash fee, default gas parameters (README section B),
  5 bps slippage buffer.
- Tokens verified on-chain before use (symbol/decimals/getCode):
  WETH, USDC, USDT, ARB, GMX, MAGIC, PENDLE, RDNT, GNS, wstETH, LINK, UNI.
  (MAGIC's address from memory was wrong; corrected via Arbiscan and then
  verified on-chain. USDT's on-chain symbol is "USD₮0".)
- Pool discovery: Uniswap V3 factory getPool() across fee tiers
  100/500/3000/10000 -> 78 pools, 20 pairs; filtered to live liquidity.
- Grid: sizes $100 / $500 / $1,000 / $5,000 / $10,000 / $25,000 per pair.
- ETH/USD (Chainlink, at run time): ~$1,790.
- RPC: public endpoints (PublicNode via curl transport — the default urllib
  transport dropped connections intermittently; no keys used).

## Result

**1,008 route/size combinations. 0 profitable. 0 scan errors.**

Best route per pair (every best is at the smallest size, $100):

| Pair | Best net | Route |
|---|---|---|
| USDC/USDT | −$0.0766 | 0.05% → 0.01% |
| WETH/USDT | −$0.2518 | 0.30% → 0.05% |
| ARB/WETH | −$0.3590 | 0.30% → 0.05% |
| PENDLE/WETH | −$0.3964 | 0.05% → 0.30% |
| LINK/WETH | −$0.4294 | 0.05% → 0.30% |
| GMX/USDC | −$0.7338 | 0.30% → 1.00% |
| ARB/USDC | −$0.9589 | 0.30% → 1.00% |
| GMX/WETH | −$1.8020 | 0.30% → 1.00% |
| PENDLE/USDC | −$4.2470 | 0.30% → 1.00% |
| wstETH/WETH | −$5.1019 | 0.01% → 0.30% |
| MAGIC/WETH | −$5.2672 | 1.00% → 0.30% |
| UNI/WETH | −$14.2452 | 1.00% → 0.30% |
| LINK/USDC | −$25.1623 | 0.05% → 0.30% |
| wstETH/USDC | −$84.4098 | 0.05% → 0.30% |
| GNS/WETH | −$96.8648 | 0.30% → 0.01% |
| RDNT/WETH | −$99.5258 | 1.00% → 0.30% |
| UNI/USDC | −$99.6856 | 0.05% → 0.30% |
| MAGIC/USDC | −$99.8077 | 0.30% → 0.05% |
| RDNT/USDC | −$99.9414 | 1.00% → 0.50% |

## Interpretation

- On the majors, the spread is smaller than the fees (the published finding).
- On the long tail, spreads are wider but liquidity is the binding
  constraint: 695 of 1,008 combos lost >90% of the cycle value — many pools
  hold out-of-range ("zombie") liquidity, so the return leg pays ~nothing.
- Every pair's best result sits at the minimum size and degrades as size
  grows: price impact, not fees, is what kills long-tail routes. The optimal
  trade size is zero — the EV gate's answer, again.
- Closest-to-zero overall is the USDC/USDT stablecoin pair (−$0.077 at
  $100), and it too degrades immediately with size.

Provenance: full machine-readable results (every route's profit breakdown),
the verified token/pool discovery data, and the exact discovery + scan
scripts that produced this note are retained by the author and available on
request. This note was produced as a direct follow-up to a reader question
on the dev.to post-mortem linked in the README.
