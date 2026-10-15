# Test plan

This project does not yet have a verified automated test suite. Before any live use, add and run tests for:

- Solidity compilation with the version declared in the source.
- Callback authorization and accounting.
- Route continuity, token validation, and pair/pool fee assumptions.
- Allowance reset behavior and tokens with unusual transfer behavior.
- Partial liquidations, pre-existing balances, and unspent principal accounting.
- Slippage, gas cost, token decimals, stale quotes, RPC failures, reorgs, and competing transactions.
- Python configuration validation and dry-run behavior.

Run tests on a local fork or test network. Do not treat a passing simulation as proof of profitability or security.
