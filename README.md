# Arbitrum One Read-Only Opportunity Scanner, Cross-Chain Speculative OEV, UniswapX Intent Filler & Flash-Loan Research Suite (v3.5)

## 1. Project Scope & Architecture Classification

This repository provides a **verification-first, read-only Arbitrum One (`chain_id = 42161`) and Optimism (`chain_id = 10`) opportunity discovery, cross-chain oracle lead-lag monitor, Dutch-auction intent evaluator, cost-accounting, and pinned-block simulation suite**, paired with reference smart-contract (`contracts/FlashLoanArbitrage.sol`) and multi-protocol liquidation/keeper research modules.

To maintain strict engineering transparency, every component in this repository is classified into one of three explicit categories:

### A. Observed & Tested (Verified by 31 Automated Unit & Deterministic Pinned-Block Tests)
1. **Fail-Closed Configuration & On-Chain Metadata Validation (`bot/opportunity_scanner.py`)**:
   - Rejects template configs (`is_example_template = true`), zero/placeholder addresses (`0x000...0001`), duplicate pools, and mismatched pair orientations before making network calls.
   - Verifies `eth_chainId == 42161` (Arbitrum One), checks block freshness (`max_block_age_seconds`), confirms deployed contract bytecode (`eth_getCode`) at the pinned block, and verifies on-chain token `decimals()` and pool `token0()` / `token1()`.
2. **Single-RPC `Multicall3` Pinned-Block State Batching (`bot/opportunity_scanner.py`)**:
   - Packs all pool state reads (`token0`, `token1`, `slot0`, `globalState`, `liquidity`, `fee`) into a single pinned-block [`Multicall3.tryAggregate` (`0xcA11bde05977b3631167028862bE2a173976CA11`)](https://github.com/mds1/multicall3) RPC request (`0xbce38bd7`), eliminating 13+ sequential HTTP round-trips per scan cycle, with automatic fallback to individual `eth_call` requests.
3. **Cross-Chain Speculative OEV Lead-Lag Monitor (`bot/cross_chain_oev_monitor.py`)**:
   - Grounded in [*Signals and Spoils: Speculative Oracle Extractable Value in the Era of Cross-Chain Price Feeds* (`arXiv:2606.03434v1`, June 2026)](https://arxiv.org/html/2606.03434v1) and the [Compound DAO OEV Rollout Update (July 2025)](https://www.comp.xyz/t/oev-rfp-process-update-july-2025/6945).
   - Polls Chainlink `latestRoundData()` (`0xfeaf968c`) on Optimism (`0x13e3Ee699D1909E989722E753853AE30b17e08c5`) and Arbitrum One (`0x639Fe6ab55C921f74e7fac1ee960C0B6293ba612`), detects when Optimism leads Arbitrum by up to ~6 seconds on a price drop, and projects Arbitrum borrower Health Factors (`H^{(OP)}(t) = H^{(ARB)}(t) * P^{(OP)} / P^{(ARB)}`) before the Arbitrum oracle updates.
4. **Zero-Capital UniswapX Dutch-Auction Intent & V4 Singleton Evaluator (`bot/uniswapx_filler_evaluator.py` & `contracts/FlashLoanArbitrage.sol`)**:
   - Grounded in [`Uniswap/UniswapX`](https://github.com/Uniswap/UniswapX), [Chitra et al. (`arXiv:2403.02525v1`)](https://arxiv.org/html/2403.02525v1), and [Cyfrin Updraft Uniswap V4 `PoolManager`](https://updraft.cyfrin.io/courses/uniswap-v4/pool-manager/swap-contract-calls).
   - Evaluates linear Dutch-auction decay curves (`compute_dutch_decayed_output_raw`), calculates exact timestamps where `reactorCallback` (`IReactorCallback`) fills become net-positive after L1+L2 gas, and implements `executeUniswapXIntentFill` / `reactorCallback` and `executeUniswapV4SingletonArb` / `unlockCallback` in `contracts/FlashLoanArbitrage.sol`.
5. **DEX-Specific Concentrated Liquidity Adapters (`UniswapV3PoolAdapter` & `CamelotAlgebraPoolAdapter`)**:
   - Queries Uniswap V3 pools via `slot0()`, `liquidity()`, `fee()`, and [Uniswap V3 `QuoterV2` (`0x61fFE014bA17989E743c5F6cB21bF9697530B21e`)](https://developers.uniswap.org/docs/protocols/v3/deployments/v3-arbitrum-deployments).
   - Queries Camelot V3 (Algebra) pools via `globalState()` (`0xe76c01e4`, reading dynamic fee in hundredths of a bip), `liquidity()`, and [Camelot Algebra Quoter (`0x0Fc73040b26E9bC8514fA028D998E73A254Fa76E`)](https://docs.camelot.exchange/contracts/arbitrum/one-mainnet/).
6. **Raw-Integer Profit Accounting & Live Arbitrum `NodeInterface` Gas Decomposition (`bot/profit_calculator.py` & `bot/transaction_simulator.py`)**:
   - Performs all profitability arithmetic in raw integer token units (`int`) and `Decimal`, preventing double-subtraction of pool swap fees when using fee-inclusive Quoter outputs.
   - Queries Arbitrum's precompile [`NodeInterface.gasEstimateComponents` (`0x00000000000000000000000000000000000000C8`, selector `0x4d2301cc`)](https://docs.arbitrum.io/arbitrum-essentials/nodeinterface/reference) at the pinned block to decompose exact L2 execution gas (`gasEstimate - gasEstimateForL1`) and L1 calldata poster costs (`gasEstimateForL1`).
7. **Read-Only Flash-Liquidation & Permissionless Keeper Bounty Scanners (`bot/liquidation_and_keeper_scanner.py`)**:
   - Scans [Aave V3 Pool (`0x794a61358D6845594F94dc1DB02A252b5b4814aD`)](https://aave.com/blog/historical-liquidations) borrower positions via `getUserAccountData(address)` (`0xbf92857c`), applying exact `50%` vs `100%` close-factor rules (`HF < 0.95e18`) and deducting flash-loan fees, DEX swap slippage, and L1+L2 gas.
   - Scans permissionless yield-vault harvest bounties (`callReward()` `0x6b29a679` and `paused()` `0x5c975abb`, grounded in [`beefyfinance/beefy-contracts`](https://github.com/beefyfinance/beefy-contracts)) against total L1+L2 gas cost.
8. **Multi-Block (`B -> B+1`) Persistence Logger & Go/No-Go Scorecard (`bot/paper_trading_logger.py`)**:
   - Runs multi-cycle read-only paper-trading campaigns, logs structured JSONL records, checks whether net-positive routes at block `B` survive at block `B + 1`, and outputs an objective `READY_FOR_FORK_SIM` vs `KEEP_COLLECTING_DATA` verdict.

### B. Assumed (Configurable Parameters Requiring Live RPC or Fork Confirmation)
- Default fallback gas parameters (`l2_gas_units = 240,000`, `l2_gas_price_wei = 20,000,000` [0.02 gwei], and `l1_data_fee_wei = 13,920,000,000,000`) are used when `NodeInterface` is not queried directly.
- Default slippage/competition margin (`slippage_buffer_bps = 5` for cyclic arbitrage, `20` bps for liquidations, `3` bps for UniswapX Dutch fills) is a conservative modeling buffer; actual execution slippage depends on intra-block transaction ordering on the Arbitrum sequencer.

### C. Not Implemented (Out of Scope for Read-Only Mode)
- **No Live Transaction Signing or Broadcasting**: The read-only scanner, simulator, cross-chain OEV monitor, UniswapX evaluator, liquidation scanner, and paper-trading logger never load a private key, sign transactions, or submit transactions to the Arbitrum sequencer.
- **No Sub-Millisecond Co-Located Sequencer Relay**: This codebase does not include paid private relay bidding or co-located sequencer hardware.

---

## 2. Empirical Research: Most Efficient Zero-to-Minimal Capital Smart Contract Strategies (v3.5)

Based on peer-reviewed empirical studies ([`arXiv:2606.03434v1` *Signals and Spoils*](https://arxiv.org/html/2606.03434v1); [Chitra et al. `arXiv:2403.02525v1`](https://arxiv.org/html/2403.02525v1); [Wu & Öz 2026, `arXiv:2606.00720v1`](https://arxiv.org/html/2606.00720v1); [Messias & Torres 2026, `arXiv:2509.22143v2`](https://arxiv.org/html/2509.22143v2?ref=blog.arbitrum.foundation); [Qin et al., `arXiv:2106.06389v2`](https://arxiv.org/html/2106.06389v2)) and production open-source repositories ([`Uniswap/UniswapX`](https://github.com/Uniswap/UniswapX), [`morpho-org/morpho-blue-liquidation-bot`](https://github.com/morpho-org/morpho-blue-liquidation-bot), [`0xnavarro/Aave-Liquiditor`](https://github.com/0xnavarro/Aave-Liquiditor), [`beefyfinance/beefy-contracts`](https://github.com/beefyfinance/beefy-contracts), [`mds1/multicall3`](https://github.com/mds1/multicall3)), zero-capital smart contract strategies rank from **highest efficiency / lowest latency competition** to **most latency-contested** as follows:

| Strategy Lane | Upfront Capital Needed | Typical Gross Margin per Execution | Latency & Competition Profile | Implemented Module in v3.5 |
| :--- | :--- | :--- | :--- | :--- |
| **1. Cross-Chain Speculative OEV Liquidations** ([`arXiv:2606.03434v1`](https://arxiv.org/html/2606.03434v1) Optimism-Leads-Arbitrum) | **$0 principal** (Flash-loaned via Balancer V2 / Morpho Blue; ~$15–$25 ETH gas buffer) | **5.0% – 10.0%** full liquidation bonus (no Chainlink SVR cut on L2s per [Compound DAO](https://www.comp.xyz/t/oev-rfp-process-update-july-2025/6945)) | **~6,000ms Cross-Chain Lead Window**: Optimism Chainlink updates up to 6s before Arbitrum, neutralizing ~200ms geographic latency. | `bot/cross_chain_oev_monitor.py` & `bot/liquidation_and_keeper_scanner.py` |
| **2. Zero-Capital UniswapX Dutch-Auction Intent Filling** ([`Uniswap/UniswapX`](https://github.com/Uniswap/UniswapX) & [`arXiv:2403.02525v1`](https://arxiv.org/html/2403.02525v1)) | **$0 principal & 0% flash-loan fee** (`reactorCallback` receives user's `inputToken` upfront) | **0.05% – 0.35%** Dutch decay surplus over on-chain DEX routing | **Auction-decay driven (Medium latency)**: Profitable at the exact block timestamp where `requiredOutput` decays below `Multicall3` DEX output. | `bot/uniswapx_filler_evaluator.py` & `contracts/FlashLoanArbitrage.sol` (`reactorCallback`) |
| **3. Permissionless Yield-Vault Harvest Keepers** ([Beefy `callReward()`](https://github.com/beefyfinance/beefy-contracts)) | **$0 principal & $0 contract deployment** (Callable from standard EOA; ~$5 ETH gas buffer) | **0.05% – 0.50%** of harvested vault yield paid in `WETH`/native token | **Threshold-driven (Low latency)**: Profitable whenever accumulated `callReward() > L1+L2 gas`. Immune to MEV sandwiching. | `bot/liquidation_and_keeper_scanner.py` (`scan_permissionless_keeper_bounties_readonly`) |
| **4. Long-Tail & Multi-Hop Concentrated DEX Arbitrage + V4 Singleton** ([Wu & Öz 2026](https://arxiv.org/html/2606.00720v1)) | **$0 principal** (0%-fee Balancer V2 / Morpho Blue / V4 `unlockCallback`) | **0.05% – 0.40%** net spread after pool fees | **Block-tick driven (High latency sensitivity)**: Uses `Multicall3` single-RPC batching and on-chain `quotePathOnChain` soft-abort guards (`arXiv:2606.00720v1`). | `bot/opportunity_scanner.py`, `bot/profit_calculator.py`, `bot/paper_trading_logger.py` |

---

## 3. Operational & Smart-Contract Risk Disclosure

Operating flash-loan arbitrage, speculative OEV liquidation, intent-filling, or keeper bots involves real financial and technical risks, even when flash-loan or callback principal is borrowed atomically:
1. **Reverted & Soft-Aborted Transaction Gas Drain**: If a competing searcher captures an opportunity earlier in the same 250ms Arbitrum block, or if a speculative cross-chain oracle update does not land as projected, your transaction will either soft-abort or revert, consuming **L2 execution gas plus L1 calldata posting fees** in ETH from your executor wallet.
2. **State Drift Between Scan and Inclusion**: A route, Dutch-auction order, or liquidation that evaluates as `ESTIMATED_NET_POSITIVE_UNVERIFIED` or passes `PRELIMINARY_CALL_CHECK` at block `N` is **not guaranteed** to remain profitable when your transaction is sequenced in block `N + 1`. Always use `bot/paper_trading_logger.py` to measure `next_block_persistence_rate` before risking gas.
3. **Illiquid ("Zombie") Collateral & Token Tax Risks**: Seizing collateral on lending markets with shallow DEX exit liquidity or interacting with non-standard fee-on-transfer tokens can cause repayment reverts.
4. **RPC Latency & Rate-Limiting**: Public RPC endpoints can rate-limit or lag behind the Arbitrum sequencer feed (`wss://arb1.arbitrum.io/feed`), resulting in stale reads.

---

## 4. Quickstart: Offline Validation, 31-Test Suite & Paper-Trading Logger

### Step 1 — Run the Full 31-Test Automated Verification Suite
```bash
bash scripts/run_bot.sh
```
This runs:
- `python3 bot/opportunity_scanner.py --validate-only` (offline schema, checksum, placeholder, and pair-orientation check)
- `python3 -m unittest tests/test_remediation_suite.py -v` (23 tests verifying Findings 1–10 + v3.4/v3.5 Multicall3, NodeInterface gas, Aave V3 liquidations, keeper bounties, persistence scoring, Cross-Chain Speculative OEV, and UniswapX / V4 callbacks)
- `python3 -m unittest tests/test_suite_v3.py -v` (8 tests verifying mathematical & static Solidity invariants)
- `python3 bot/scanner_stub.py` (deterministic pinned-block read-only scan output with `Multicall3` batching)

### Step 2 — Run a Live Read-Only Pinned-Block Scan on Arbitrum One
```bash
export ARBITRUM_RPC_URL="https://arb1.arbitrum.io/rpc"
python3 bot/opportunity_scanner.py --config config/arbitrum_pools.json --rpc-url "$ARBITRUM_RPC_URL"
```

### Step 3 — Run a Multi-Cycle Paper-Trading Persistence Campaign (Before Any Deployment)
```bash
python3 bot/paper_trading_logger.py \
  --config config/arbitrum_pools.json \
  --rpc-url "$ARBITRUM_RPC_URL" \
  --cycles 20 \
  --interval-sec 1.0 \
  --output-jsonl logs/paper_trading_arbitrum.jsonl
```
Only proceed to local Anvil fork simulation if the scorecard outputs `"verdict": "READY_FOR_FORK_SIM"`.
