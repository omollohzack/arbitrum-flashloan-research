# Arbitrum Flash-Loan Research

Research prototype for studying flash-loan arbitrage and liquidation mechanics on Arbitrum.

## Architecture v3

The v3 architecture introduces key updates to the platform:

- **5-Mode V2/V3/Algebra Routing**: Multi-protocol execution and flexible routing across V2, V3, and Algebra DEX pools.
- **`quotePathOnChain()` Concentrated Virtual Reserve Soft-Abort**: On-chain quoter validation checking virtual reserves to perform soft-aborts on execution if depth or slippage thresholds are breached.
- **`BorrowerIndexer`**: Off-chain indexer for efficiently scanning and monitoring borrower account positions across protocols.
- **Compound V3 Comet Liquidations**: Support for targeting and executing liquidations on Compound V3 Comet pools.
- **`SINGLE_LIQUIDATION` Partial Close-Factor Fix**: Corrected close-factor logic during single liquidation calls to support partial repayments accurately.

## Project layout

- `contracts/FlashLoanArbitrage.sol` — Solidity smart-contract source.
- `bot/arbitrum_flash_bot.py` — off-chain scanner and simulation logic.
- `scripts/run_bot.sh` — guarded launch helper.
- `REVIEW_NOTES.md` — known fixes, checks, and remaining work.

## Status

**Research only — not production-ready.** Profitability has not been established, and the contract has not been independently audited. Do not use real funds until the source is compiled, tested on a fork or test network, and independently reviewed.

## Setup

Use a dedicated Python virtual environment and install dependencies only after reviewing the bot's imports and any dependency manifest you create. Configure RPC endpoints using environment variables or a local, untracked `.env` file.

## Safe operation

- Keep dry-run mode enabled during development.
- Never commit private keys, seed phrases, API tokens, or a populated `.env` file.
- Verify protocol and token addresses against official deployment registries.
- Account for gas, pool fees, slippage, token decimals, stale quotes, and transaction competition.
- A successful simulation does not guarantee a live transaction will execute or be profitable.

## Validation still required
