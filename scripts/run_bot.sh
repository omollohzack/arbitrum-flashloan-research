#!/usr/bin/env bash
set -euo pipefail

# scripts/run_bot.sh — Read-Only Validation, Multi-Strategy, Cross-Chain OEV & Persistence Suite Runner (v3.5)
MODE="${MODE:-read-only}"

echo "[1/5] Validating Arbitrum One (chain_id=42161) pool, Chainlink & UniswapX configuration..."
python3 bot/opportunity_scanner.py --validate-only

echo "[2/5] Running 23-test Technical Review, v3.4 & v3.5 Cross-Chain OEV / UniswapX Regression Suite..."
python3 -m unittest tests/test_remediation_suite.py -v

echo "[3/5] Running 8-test Math & Static Security Suite..."
python3 -m unittest tests/test_suite_v3.py -v

echo "[4/5] Running deterministic pinned-block Multicall3 read-only scanner harness..."
python3 bot/scanner_stub.py

echo "[5/5] Running 7-test Profitability Improvements Suite (optimal sizing, zero-fee flash selection, execution gate)..."
python3 -m unittest tests/test_profitability_improvements.py -v
