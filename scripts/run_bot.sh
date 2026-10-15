#!/usr/bin/env bash
set -euo pipefail

# Safe launcher: dry-run is the default. This script never stores secrets.
MODE="${MODE:-dry-run}"
BOT_FILE="${BOT_FILE:-bot/arbitrum_flash_bot.py}"

if [[ ! -f "$BOT_FILE" ]]; then
  echo "Bot file not found: $BOT_FILE" >&2
  exit 1
fi

if [[ "$MODE" != "dry-run" ]]; then
  echo "Refusing non-dry-run mode. Review and test the project independently before implementing live execution." >&2
  exit 2
fi

echo "Starting in dry-run mode. No live transactions should be broadcast."
exec python3 "$BOT_FILE" --help
