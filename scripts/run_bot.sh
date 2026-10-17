#!/usr/bin/env bash
set -euo pipefail

# Safe v3.2 Multi-Chain Launcher for Arbitrum One, Base & Optimism
MODE="${MODE:-dry-run}"
CHAIN="${CHAIN:-arbitrum}"
BOT_FILE="${BOT_FILE:-bot/arbitrum_flash_bot.py}"
TEST_FILE="${TEST_FILE:-tests/test_suite_v3.py}"

if [[ ! -f "$BOT_FILE" ]]; then
  echo "Bot file not found: $BOT_FILE" >&2
  exit 1
fi

echo "[1/3] Running FlashBot v3.2 self-tests on chain: ${CHAIN}..."
python3 "$BOT_FILE" --chain "$CHAIN" --self-test

if [[ -f "$TEST_FILE" ]]; then
  echo "[2/3] Running automated 8-test unit & security test suite..."
  python3 "$TEST_FILE"
fi

if [[ "$MODE" != "dry-run" ]]; then
  echo "Refusing non-dry-run mode via default launcher. Set DRY_RUN=false in untracked .env only after fork testing."
  exit 2
fi

echo "[3/3] Starting in dry-run mode on ${CHAIN}. No live transactions will be broadcast."
exec python3 "$BOT_FILE" --chain "$CHAIN" --dry-run
