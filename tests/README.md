# Test plan

Automated Python tests are available in this repository. They support research validation but do not establish deployment safety, security, or profitability.

The published research found zero profitable outcomes across 33 route/size combinations and a separate 1,008-combination long-tail scan. The best result in the 33-combination scan was −$1.08. These results describe the tested strategy and conditions, not future returns.

Before any live use, test:

- Solidity compilation with the version declared in the source.
- Callback authorization and accounting.
- Route continuity, token validation, and pair/pool fee assumptions.
- Allowance reset behavior and tokens with unusual transfer behavior.
- Partial liquidations, pre-existing balances, and unspent principal accounting.
- Slippage, gas cost, token decimals, stale quotes, RPC failures, reorgs, and competing transactions.
- Python configuration validation and dry-run behavior.

Run tests on a local fork or test network. Do not treat a passing test suite or simulation as proof of profitability or security. Obtain an independent security audit before using real funds.
