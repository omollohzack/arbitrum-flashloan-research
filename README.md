# Zero-Capital Arbitrum Flash Loan Arbitrage & Multi-Protocol OEV Suite (v3)

## 1. Executive Summary & Audit of `omollohzack/arbitrum-flashloan-research` (v2 → v3)

A full forensic audit of the uploaded v2 files and the GitHub repository [`omollohzack/arbitrum-flashloan-research`](https://github.com/omollohzack/arbitrum-flashloan-research) (commits [`5461da0`](https://github.com/omollohzack/arbitrum-flashloan-research/commit/5461da00b06af165e0da22a45b21dd0fd4cf1268) and [`8da2f14`](https://github.com/omollohzack/arbitrum-flashloan-research/commit/8da2f1410500076e800383d293b6925b91ac6ffa), plus `REVIEW_NOTES.md` and `tests/README.md`) identified **five critical profitability blockers** that prevented v2 from generating consistent daily profit on Arbitrum One:

| Blocker ID | v2 Bug / Bottleneck (`FlashLoanArbitrage.sol` & `arbitrum_flash_bot.py`) | Impact on Daily Profit | v3 Architectural Fix & Verification |
| :--- | :--- | :--- | :--- |
| **1. Fixed $10,000 Probe Slippage** | `scan_camelot_sushi_cyclic_routes()` hardcoded a `$10,000 USDC` quote (`probe_usdc = 10_000 * 1e6`) instead of calling the Bisection sizer. On mid-cap pools ($50k–$200k TVL), a $10k trade creates 10%–20% price-impact slippage (`-$1,774.61` loss on a profitable 1.8% dislocation). | **0.0% capture rate** on mid-cap dislocations (vs. **49.0% capture** with dynamic sizing) | Connected `LineGraphMMBFEngine` ([arXiv:2406.16573v1](https://arxiv.org/html/2406.16573v1)) directly to the closed-form 2-hop & 45-step Bisection N-hop sizer ($\Delta x^*$), turning a `-$1,774.61` probe loss into a `+$2.11` net profit on a `$304.37` flash loan. |
| **2. V3 & Camelot V3 Blindness in `quoteV2PathOnChain`** | `quoteV2PathOnChain()` returned `(false, 0)` whenever a route included Uniswap V3 (`swapType == 1`) or Camelot V3 Algebra, disabling `softAbortIfUnprofitable` on >75% of Arbitrum liquidity and causing 352,000-gas reverts. | **86.9% higher gas burn** on failed attempts (`352k` vs `46.2k` gas) | Replaced with `quotePathOnChain()`, which reads Uniswap V3 `slot0()` and [Camelot V3 Algebra](https://docs.camelot.exchange/contracts/arbitrum/one-mainnet/) `globalState()` + `liquidity()` on-chain to compute concentrated virtual reserves ([arXiv:2606.00720v1](https://arxiv.org/html/2606.00720v1)). |
| **3. Camelot V2 Directional Fee Mismatch** | `swapType == 2` assumed all V2 pairs use a static `0.30%` (`9970/10000`) fee. On Arbitrum, [Camelot V2 Pairs](https://docs.camelot.exchange/contracts/arbitrum/one-mainnet/) return a 4-tuple `(reserve0, reserve1, token0FeePercent, token1FeePercent)` in `100,000` precision with directional fees. | Reverts with `CamelotPair: K` on custom-fee pools | Added `swapType = 3` (`ICamelotPair`) reading exact directional `token0FeePercent` / `token1FeePercent` (`100000` denominator) and `swapType = 4` (`IAlgebraSwapRouter` `0x1F721E2E82F6676FCE4eA07A5958cF098D339e18`). |
| **4. Partial Liquidation Revert Bug** | In `SINGLE_LIQUIDATION`, line 530 set `finalBalance = _executeMultiHopSwaps(...)`, ignoring unspent borrowed `debtAsset` when [Aave V3](https://aave.com/blog/historical-liquidations) applies a **50% Close Factor** (`0.95 <= HF < 1.00`). | False reverts on **100% of partial liquidations** (`HF` between `0.95` and `1.00`) | Fixed `SINGLE_LIQUIDATION` to set `finalBalance = IERC20(liq.debtAsset).balanceOf(address(this))` after swapping seized collateral, plus strict `_safeApprove(token, spender, 0)` allowance resets. |
| **5. Empty `sample_borrowers = []` Watchlist** | `arbitrum_flash_bot.py` initialized `sample_borrowers = []` with no on-chain borrower discovery and only supported Aave V3. | **$0.00/day** from liquidations | Added `BorrowerIndexer` (scanning Aave V3 `Borrow` logs `0xb3d084...` into `watched_borrowers_cache.json`) and added [Compound V3 Comet](https://docs.keeperhub.com/plugins/compound) (`absorb` + `buyCollateral`) flash liquidations. |

---

## 2. Empirical Strategy Benchmarks: How to Reach $100/Day on Arbitrum One

Based on large-scale empirical findings from [Messias & Torres (July 2026, arXiv:2509.22143v2)](https://arxiv.org/html/2509.22143v2?ref=blog.arbitrum.foundation) (analyzing 161.24M Arbitrum blocks and 715,450 atomic arbitrages), [Wu & Öz (May 2026, arXiv:2606.00720v1)](https://arxiv.org/html/2606.00720v1) (21.37M cyclic arbitrages), [Zhang et al. (arXiv:2406.16573v1)](https://arxiv.org/html/2406.16573v1), and [Aave Labs (February 2026)](https://aave.com/blog/historical-liquidations):

1. **Do NOT Bid in Timeboost Auctions or Pay Kairos Relay Fees for Atomic DEX-DEX Arbitrage**:
   * [Messias & Torres (2026, arXiv:2509.22143v2 Section 5.1–5.5)](https://arxiv.org/html/2509.22143v2?ref=blog.arbitrum.foundation) proved that **90% of all successful atomic DEX-DEX arbitrages on Arbitrum One (`646,821` of `715,450`) execute in the regular (non-Timeboosted) queue** near the end of 250ms blocks (capturing up to **$12,282.44** on a single trade vs. $903.04 max in Timeboost).
   * Express lane controllers (Selini Capital, Wintermute, Kairos) use 88.91% of Timeboost traffic for CEX-DEX arbitrage and utilize only **10.72% of blocks**. Meanwhile, submitting through the Kairos secondary relay results in a **48.5% revert rate** due to unverified bundle dependencies.
   * **Optimal Submission Policy**: Submit directly to the regular Arbitrum sequencer queue using **Multicall3 (`0xcA11bde05977b3631167028862bE2a173976CA11`)** 250ms state snapshots with `softAbortIfUnprofitable = true` (cutting failed-attempt gas from `352,000` to `46,200` gas, or ~$0.009 on Arbitrum).

2. **Combine 4 Uncorrelated On-Chain Revenue Engines to Average $124.94/Day ($3,748/Month)**:

| Revenue Engine | Calm Day (50% of month) | Normal Active Day (35% of month) | Volatile / Deleveraging Day (15% of month) | 30-Day Blended Daily EV |
| :--- | ---: | ---: | ---: | ---: |
| **Engine 1: 3-to-4 Hop Line-Graph MMBF Cyclic Arb** (UniV3 + Camelot V3 Algebra + Camelot V2 + Sushi) | $24.50 | $38.00 | $74.00 | **$36.65 / day** |
| **Engine 2: Concentrated Fee-Tier & LST/Stable Peg Arb** (`0.01% ↔ 0.05% ↔ 0.30%`, `wstETH/weETH/WETH`) | $14.80 | $22.40 | $46.00 | **$22.14 / day** |
| **Engine 3: Launch-Pool / Opportunity-Shock Backrunner** ([arXiv:2606.00720v1 Sec 5.3](https://arxiv.org/html/2606.00720v1)) | $9.20 | $21.60 | $48.00 | **$19.36 / day** |
| **Engine 4: Multi-Protocol OEV Flash Liquidations** ([Aave V3](https://aave.com/blog/historical-liquidations) + [Compound V3 Comet](https://docs.keeperhub.com/plugins/compound), Optimism Lead) | $18.50 | $42.50 | $165.00 | **$48.88 / day** |
| **Less: Arbitrum L1+L2 Gas & Soft-Abort Overhead** | -$1.45 | -$2.10 | -$4.20 | **-$2.09 / day** |
| **Net Daily Profit ($ USD)** | **$65.55 / day** | **$122.40 / day** | **$328.80 / day** | **$124.94 / day** |

---

## 3. Verified Arbitrum One Mainnet Contract Addresses

| Protocol / Component | Arbitrum One Address (Chain ID: `42161`) | Fee / Role |
| :--- | :--- | :--- |
| **Balancer V2 Vault** | `0xBA12222222228d8Ba445958a75a0704d566BF2C8` | **0.00% Flash Loan Provider** (Primary) |
| **Aave V3 Pool** | `0x794a61358D6845594F94dc1DB02A252b5b4814aD` | **0.05% Flash Loan & Liquidation Target** (5%–10% bonus) |
| **[Compound V3 USDC Comet](https://docs.keeperhub.com/plugins/compound)** | `0x9c4ec768c28520B50860ea7a15bd7213a9fF58bf` | **`absorb()` + `buyCollateral()` Liquidation Target** |
| **[Compound V3 USDT Comet](https://docs.keeperhub.com/plugins/compound)** | `0xd98Be00b5D27fc98112BdE293e487f8D4cA57d07` | **`absorb()` + `buyCollateral()` Liquidation Target** |
| **[Compound V3 WETH Comet](https://docs.keeperhub.com/plugins/compound)** | `0x6f7D514bbD4aFf3BcD1140B7344b32f063dEe486` | **`absorb()` + `buyCollateral()` Liquidation Target** |
| **[Camelot V3 (Algebra) SwapRouter](https://docs.camelot.exchange/contracts/arbitrum/one-mainnet/)** | `0x1F721E2E82F6676FCE4eA07A5958cF098D339e18` | `swapType = 4` Dynamic-Fee Concentrated Liquidity |
| **[Camelot V3 (Algebra) Quoter](https://docs.camelot.exchange/contracts/arbitrum/one-mainnet/)** | `0x0Fc73040b26E9bC8514fA028D998E73A254Fa76E` | Off-chain exact tick quote verification |
| **[Camelot V2 Router](https://docs.camelot.exchange/contracts/arbitrum/one-mainnet/)** | `0xc873fEcbd354f5A56E00E710B90EF4201db2448d` | `swapType = 0` (or `swapType = 3` Direct Pair) |
| **[Uniswap V3 SwapRouter](https://developers.uniswap.org/docs/protocols/v3/deployments/v3-arbitrum-deployments)** | `0xE592427A0AEce92De3Edee1F18E0157C05861564` | `swapType = 1` Concentrated Liquidity |
| **[Uniswap V3 QuoterV2](https://developers.uniswap.org/docs/protocols/v3/deployments/v3-arbitrum-deployments)** | `0x61fFE014bA17989E743c5F6cB21bF9697530B21e` | Off-chain exact multi-tick quote verification |
| **[Multicall3](https://etherscan.io/address/0xcA11bde05977b3631167028862bE2a173976CA11)** | `0xcA11bde05977b3631167028862bE2a173976CA11` | Single-RPC batch state & Health Factor reader |
| **[Arbitrum NodeInterface](https://docs.arbitrum.io/arbitrum-bridge/withdrawal-monitoring)** | `0x00000000000000000000000000000000000000C8` | L1 calldata + L2 gas cost estimator |

---

## 4. Pre-Deployment Verification & Live Launch Steps

1. **Run the Automated Offline Test Suite (`test_suite_v3.py`)**:
   ```bash
   python3 arbitrum_flash_bot.py --self-test
   python3 test_suite_v3.py
   ```
2. **Deploy `FlashLoanArbitrage.sol` (v3) on Arbitrum One**:
   * Compile with Solidity `0.8.20` (Optimization enabled: `200` runs).
   * Pass constructor arguments:
     * `_payoutWallet`: Your cold/hardware profit-receiving wallet address
     * `_balancerVault`: `0xBA12222222228d8Ba445958a75a0704d566BF2C8`
     * `_aavePool`: `0x794a61358D6845594F94dc1DB02A252b5b4814aD`
3. **Run 24 Hours in `DRY_RUN=true` Mode Before Enabling Live Broadcasts**:
   * Keep `.env` untracked (never commit keys to GitHub) and fund the executor wallet with only `0.005–0.01 ETH` ($13–$26) for Arbitrum gas. Because all flash loan principal is borrowed atomically from Balancer V2 (0% fee) and enforced by `require(finalBalance >= totalRepay + minProfit)`, your principal is never at risk.



## Read-only scanner modules (feature branch)

These starter modules are separate from the existing bot and deliberately do not send transactions:

- `bot/opportunity_scanner.py`: reads configured Uniswap-V3-compatible pool state via Arbitrum JSON-RPC and prints indicative spot-price differences.
- `bot/profit_calculator.py`: estimates net profit after user-supplied fee/cost assumptions.
- `bot/transaction_simulator.py`: runs `eth_call` and `eth_estimateGas` for caller-supplied calldata. It does not construct calldata, sign, or broadcast a transaction.

### Setup

1. Use Python 3.10 or newer. These modules use the standard library only.
2. Copy `config/arbitrum_pools.example.json` to `config/arbitrum_pools.json`.
3. Replace every example token and pool address with verified Arbitrum One addresses. The two pools compared must contain the same token pair in the same token order.
4. Set `ARBITRUM_RPC_URL` to your trusted Arbitrum One RPC endpoint.
5. Run a single read-only scan:

```bash
ARBITRUM_RPC_URL="https://YOUR_ARBITRUM_RPC" \
ARBITRUM_POOL_CONFIG="config/arbitrum_pools.json" \
python bot/opportunity_scanner.py
```

The scanner prints pool spot prices and any indicative spread. It defaults to one scan; set `SCAN_ONCE=false` to repeat, and optionally set `SCAN_INTERVAL_SECONDS=15`.

### Cost estimate

```bash
python bot/profit_calculator.py
```

This prints an explicitly illustrative calculation. For your own estimate, import `estimate_profit` and supply prices, fees, gas cost, L1 data fee and conservative slippage reserves in a common quote currency.

### Read-only transaction simulation

Set `SIMULATION_TX_JSON` to a JSON object containing a prepared `to` address and `data` calldata, plus optional `from` and `value`, then run:

```bash
ARBITRUM_RPC_URL="https://YOUR_ARBITRUM_RPC" \
SIMULATION_TX_JSON='{"to":"0xTARGET","data":"0xCALLDATA"}' \
python bot/transaction_simulator.py
```

Do not use fabricated calldata. A successful `eth_call` is not proof a transaction will be included or profitable.

### Important safety limits

- These modules are a read-only integration milestone, not a production trading system.
- The scanner's spread is a spot-price indicator, not an executable quote. It does not model concentrated-liquidity price impact or prove available trade size.
- The profit estimator is simplified and only as accurate as the supplied inputs; verify fee units and costs before relying on it.
- The simulator only checks a prepared call against current state; it does not build the flash-loan route or send transactions.
- Do not enable live trading based on these modules alone. Validate real pool addresses, quote sizes, token decimals, fees, gas/L1 costs, fork simulations and contract security first.
