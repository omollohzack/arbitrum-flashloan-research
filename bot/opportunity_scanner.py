#!/usr/bin/env python3
"""Read-only Arbitrum V3 pool scanner. No wallet and no transaction submission.

Configuration:
  ARBITRUM_RPC_URL=https://... (use a trusted Arbitrum One RPC)
  ARBITRUM_POOL_CONFIG=path/to/pools.json

Example pools.json:
{
  "tokens": {
    "0xTOKEN_ADDRESS": {"symbol": "TOKEN", "decimals": 18},
    "0xOTHER_ADDRESS": {"symbol": "OTHER", "decimals": 6}
  },
  "pools": [
    {"dex": "DEX A", "address": "0xPOOL_ADDRESS", "fee_bps": 30},
    {"dex": "DEX B", "address": "0xOTHER_POOL", "fee_bps": 5}
  ]
}
Use actual Arbitrum One Uniswap V3-compatible pool addresses and token metadata.
Detected spreads are indicative spot-price differences only; not executable profit.
"""
from __future__ import annotations
import json
import os
import sys
import time
import urllib.request
from decimal import Decimal, getcontext
from pathlib import Path
from typing import Any

getcontext().prec = 50
Q96 = Decimal(2) ** 96
SELECTORS = {
    "token0": "0x0dfe1681",
    "token1": "0xd21220a7",
    "slot0": "0x3850c7bd",
    "liquidity": "0x1a686502",
}

class RpcError(RuntimeError):
    pass

class Rpc:
    def __init__(self, url: str, timeout: int = 15):
        self.url = url
        self.timeout = timeout
        self.counter = 0

    def call(self, to: str, data: str) -> str:
        self.counter += 1
        payload = json.dumps({
            "jsonrpc": "2.0", "id": self.counter,
            "method": "eth_call",
            "params": [{"to": to, "data": data}, "latest"]
        }).encode()
        req = urllib.request.Request(self.url, data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                body = json.loads(response.read().decode())
        except Exception as exc:
            raise RpcError(f"RPC request failed: {exc}") from exc
        if "error" in body:
            raise RpcError(str(body["error"]))
        result = body.get("result")
        if not isinstance(result, str) or not result.startswith("0x"):
            raise RpcError(f"Unexpected eth_call result: {result!r}")
        return result

def as_address(word: str) -> str:
    return "0x" + word[-40:].lower()

def word(result: str, index: int = 0) -> int:
    raw = result[2:]
    start = index * 64
    if len(raw) < start + 64:
        raise RpcError("RPC returned too few bytes")
    return int(raw[start:start + 64], 16)

def read_pool(rpc: Rpc, entry: dict[str, Any], tokens: dict[str, Any]) -> dict[str, Any]:
    address = entry["address"]
    token0 = as_address(rpc.call(address, SELECTORS["token0"]))
    token1 = as_address(rpc.call(address, SELECTORS["token1"]))
    if token0 not in tokens or token1 not in tokens:
        raise ValueError(f"{address}: add token metadata for {token0} and {token1} to config")
    slot0 = rpc.call(address, SELECTORS["slot0"])
    sqrt_price_x96 = word(slot0, 0)
    liquidity = word(rpc.call(address, SELECTORS["liquidity"]))
    t0, t1 = tokens[token0], tokens[token1]
    if int(t0["decimals"]) < 0 or int(t0["decimals"]) > 36 or int(t1["decimals"]) < 0 or int(t1["decimals"]) > 36:
        raise ValueError(f"{address}: token decimals must be between 0 and 36")
    price = (Decimal(sqrt_price_x96) / Q96) ** 2
    price *= Decimal(10) ** (int(t0["decimals"]) - int(t1["decimals"]))
    return {
        "dex": entry.get("dex", "unknown"),
        "pool": address,
        "token0": t0["symbol"], "token1": t1["symbol"],
        "token0_address": token0, "token1_address": token1,
        "price_token1_per_token0": price,
        "fee_bps": Decimal(str(entry.get("fee_bps", 0))),
        "liquidity_raw": liquidity,
        "sqrtPriceX96": sqrt_price_x96,
    }

def scan_once(config: dict[str, Any], rpc: Rpc) -> list[dict[str, Any]]:
    tokens = {key.lower(): value for key, value in config["tokens"].items()}
    rows = []
    for entry in config.get("pools", []):
        try:
            row = read_pool(rpc, entry, tokens)
            if row["liquidity_raw"] == 0 or row["sqrtPriceX96"] == 0:
                print(f"SKIP {row['dex']}: pool has zero liquidity or uninitialized price")
                continue
            rows.append(row)
            print(f'{row["dex"]}: {row["token0"]}/{row["token1"]} '
                  f'price={row["price_token1_per_token0"]:.12g} '
                  f'fee={row["fee_bps"]} bps liquidity_raw={row["liquidity_raw"]}')
        except Exception as exc:
            print(f'SKIP {entry.get("dex", entry.get("address", "pool"))}: {exc}', file=sys.stderr)
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        pair = (row["token0_address"], row["token1_address"])
        groups.setdefault(pair, []).append(row)
    opportunities = []
    for pair_rows in groups.values():
        if len(pair_rows) < 2:
            continue
        for buy in pair_rows:
            for sell in pair_rows:
                if buy is sell:
                    continue
                # For the same token ordering, lower token1/token0 price is the
                # indicative buy venue and higher is the indicative sell venue.
                if sell["price_token1_per_token0"] <= buy["price_token1_per_token0"]:
                    continue
                gross_pct = (sell["price_token1_per_token0"] / buy["price_token1_per_token0"] - 1) * 100
                fee_pct = (buy["fee_bps"] + sell["fee_bps"]) / 100
                opportunities.append({
                    "pair": f'{buy["token0"]}/{buy["token1"]}',
                    "buy_dex": buy["dex"], "sell_dex": sell["dex"],
                    "buy_price": str(buy["price_token1_per_token0"]),
                    "sell_price": str(sell["price_token1_per_token0"]),
                    "gross_spread_pct": str(gross_pct),
                    "combined_pool_fee_pct": str(fee_pct),
                    "spread_after_pool_fees_pct": str(gross_pct - fee_pct),
                    "warning": "Spot estimate only; excludes price impact, gas, L1 data fee, loan fee, and execution risk."
                })
    for opp in opportunities:
        print("CANDIDATE (NOT VERIFIED PROFIT): " + json.dumps(opp))
    return opportunities

def main() -> int:
    url = os.getenv("ARBITRUM_RPC_URL")
    config_path = os.getenv("ARBITRUM_POOL_CONFIG", "config/arbitrum_pools.json")
    if not url:
        print("Set ARBITRUM_RPC_URL to a trusted Arbitrum One RPC URL.", file=sys.stderr)
        return 2
    try:
        config = json.loads(Path(config_path).read_text(encoding="utf-8"))
        if not config.get("pools"):
            raise ValueError("config must contain a non-empty 'pools' list")
        rpc = Rpc(url)
        while True:
            print(f"\nScan started at {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
            scan_once(config, rpc)
            if os.getenv("SCAN_ONCE", "true").lower() == "true":
                break
            time.sleep(max(5, int(os.getenv("SCAN_INTERVAL_SECONDS", "15"))))
    except Exception as exc:
        print(f"Scanner stopped safely: {exc}", file=sys.stderr)
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
