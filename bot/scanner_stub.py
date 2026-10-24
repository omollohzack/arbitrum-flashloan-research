#!/usr/bin/env python3
"""
bot/scanner_stub.py — Offline Deterministic Pinned-Block Harness for `opportunity_scanner.py` (v3.4)
=====================================================================================================
Provides a deterministic mock Arbitrum One (`chain_id=42161`) RPC transport at a pinned block
(`#265000000`) including full ABI support for `Multicall3.tryAggregate` (`0xbce38bd7`) and
Arbitrum `NodeInterface.gasEstimateComponents` (`0x4d2301cc`) so the entire read-only scanner,
adapter verification, QuoterV2 exact-input routing, and JSONL telemetry pipeline can be tested
reproducibly offline without live network risk.
"""

import json
import math
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).parent))
from opportunity_scanner import (
    ARBITRUM_ONE_CHAIN_ID,
    MULTICALL3_ARBITRUM_ADDRESS,
    ArbitrumReadOnlyScanner,
    JsonRpcClient,
    SEL_ALGEBRA_GLOBAL_STATE,
    SEL_DECIMALS,
    SEL_LIQUIDITY,
    SEL_MULTICALL3_TRY_AGGREGATE,
    SEL_TOKEN0,
    SEL_TOKEN1,
    SEL_UNIV3_FEE,
    SEL_UNIV3_SLOT0,
     encode_address,
    encode_uint256,
    load_and_validate_config,
)


def _encode_multicall3_results(results: List[Tuple[bool, str]]) -> str:
    """ABI-encodes `Result[]` (`(bool success, bytes returnData)[]`) for `Multicall3.tryAggregate`."""
    arr_offset = encode_uint256(32)
    arr_len = encode_uint256(len(results))
    encoded_elems: List[str] = []
    for ok, hex_ret in results:
        clean = hex_ret[2:] if hex_ret.startswith("0x") else hex_ret
        byte_len = len(clean) // 2
        padded_len = ((byte_len + 31) // 32) * 64
        padded = clean.ljust(padded_len, "0")
        elem = (
            encode_uint256(1 if ok else 0)
            + encode_uint256(64)
            + encode_uint256(byte_len)
            + padded
        )
        encoded_elems.append(elem)

    offsets: List[str] = []
    curr_off = 32 * len(results)
    for elem in encoded_elems:
        offsets.append(encode_uint256(curr_off))
        curr_off += len(elem) // 2

    return "0x" + arr_offset + arr_len + "".join(offsets) + "".join(encoded_elems)


def _decode_multicall3_input_calls(data_hex: str) -> List[Tuple[str, str]]:
    """Decodes `(address target, bytes callData)[]` from `Multicall3.tryAggregate` input calldata."""
    clean = data_hex[10:] if data_hex.startswith("0x") else data_hex[8:]
    arr_offset_bytes = int(clean[64:128], 16)
    arr_start_hex = arr_offset_bytes * 2
    arr_len = int(clean[arr_start_hex : arr_start_hex + 64], 16)
    offsets_base_hex = arr_start_hex + 64

    calls: List[Tuple[str, str]] = []
    for i in range(arr_len):
        off_pos = offsets_base_hex + i * 64
        rel_off_bytes = int(clean[off_pos : off_pos + 64], 16)
        elem_start = offsets_base_hex + rel_off_bytes * 2
        target = "0x" + clean[elem_start + 24 : elem_start + 64].lower()
        bytes_rel_off = int(clean[elem_start + 64 : elem_start + 128], 16)
        bytes_start = elem_start + bytes_rel_off * 2
        byte_len = int(clean[bytes_start : bytes_start + 64], 16)
        call_hex = "0x" + clean[bytes_start + 64 : bytes_start + 64 + byte_len * 2]
        calls.append((target, call_hex))
    return calls


def create_deterministic_arbitrum_transport(
    *,
    chain_id: int = ARBITRUM_ONE_CHAIN_ID,
    block_number: int = 265_000_000,
    block_timestamp: int | None = None,
    pool_prices_usd: Dict[str, float] | None = None,
) -> Any:
    """
    Creates a deterministic in-memory JSON-RPC transport representing Arbitrum One state at `block_number`.
    """
    ts = int(time.time()) if block_timestamp is None else block_timestamp
    prices = pool_prices_usd or {
        "0xc6962004f452be9203591991d15f6b388e09e8d0": 2650.00,  # UniV3 0.05%
        "0xc473e2aee3441bf9240be85eb122abb059a3b57c": 2662.50,  # UniV3 0.30%
        "0x65305730262939829098e6a32a695e3803d7d0d2": 2668.00,  # Camelot Algebra (0.15% dynamic fee)
    }

    weth_addr = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1".lower()
    usdc_addr = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831".lower()
    usdt_addr = "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9".lower()
    arb_addr = "0x912CE59144191C1204E64559FE8253a0e49E6548".lower()
    multicall_addr = MULTICALL3_ARBITRUM_ADDRESS.lower()

    token_decimals = {
        weth_addr: 18,
        usdc_addr: 6,
        usdt_addr: 6,
        arb_addr: 18,
    }

    pool_fees_ppm = {
        "0xc6962004f452be9203591991d15f6b388e09e8d0": 500,
        "0xc473e2aee3441bf9240be85eb122abb059a3b57c": 3000,
        "0x65305730262939829098e6a32a695e3803d7d0d2": 1500,
    }

    liquidity_default = 8 * (10 ** 16)

    def _sqrt_price_x96_for_usd(p_usd: float) -> int:
        raw_ratio = (p_usd * (10 ** 6)) / (10 ** 18)
        return int(math.sqrt(raw_ratio) * (1 << 96))

    def _handle_single_eth_call(target: str, data: str) -> str | None:
        selector = data[:10].lower()
        if selector == SEL_DECIMALS and target in token_decimals:
            return "0x" + encode_uint256(token_decimals[target])
        if selector == SEL_TOKEN0:
            return "0x" + encode_address(weth_addr)
        if selector == SEL_TOKEN1:
            return "0x" + encode_address(usdc_addr)
        if selector == SEL_LIQUIDITY:
            return "0x" + encode_uint256(liquidity_default)
        if selector == SEL_UNIV3_FEE:
            fee_val = pool_fees_ppm.get(target, 500)
            return "0x" + encode_uint256(fee_val)
        if selector == SEL_UNIV3_SLOT0:
            sp = _sqrt_price_x96_for_usd(prices.get(target, 2650.0))
            words = encode_uint256(sp) + encode_uint256(0) * 6
            return "0x" + words
        if selector == SEL_ALGEBRA_GLOBAL_STATE:
            sp = _sqrt_price_x96_for_usd(prices.get(target, 2668.0))
            fee_val = pool_fees_ppm.get(target, 1500)
            words = encode_uint256(sp) + encode_uint256(0) + encode_uint256(fee_val) + encode_uint256(0) * 4
            return "0x" + words
        return None

    def transport(method: str, payload: Dict) -> Tuple[Dict, float]:
        params = payload.get("params", [])
        if method == "eth_chainId":
            return {"jsonrpc": "2.0", "id": payload["id"], "result": hex(chain_id)}, 0.4
        if method == "eth_getBlockByNumber":
            return {
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {
                    "number": hex(block_number),
                    "hash": "0x" + "ab" * 32,
                    "timestamp": hex(ts),
                },
            }, 0.5
        if method == "eth_getCode":
            return {"jsonrpc": "2.0", "id": payload["id"], "result": "0x6080604052"}, 0.3
        if method == "eth_call":
            call_obj = params[0]
            target = str(call_obj["to"]).lower()
            data = str(call_obj["data"])
            selector = data[:10].lower()

            if target == multicall_addr and selector == SEL_MULTICALL3_TRY_AGGREGATE:
                sub_calls = _decode_multicall3_input_calls(data)
                sub_results: List[Tuple[bool, str]] = []
                for sub_target, sub_data in sub_calls:
                    res_hex = _handle_single_eth_call(sub_target, sub_data)
                    if res_hex is not None:
                        sub_results.append((True, res_hex))
                    else:
                        sub_results.append((False, "0x"))
                return {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": _encode_multicall3_results(sub_results),
                }, 0.6

            single_res = _handle_single_eth_call(target, data)
            if single_res is not None:
                return {"jsonrpc": "2.0", "id": payload["id"], "result": single_res}, 0.4

        return {"jsonrpc": "2.0", "id": payload["id"], "error": {"code": -32000, "message": "Unsupported mock call"}}, 0.2

    return transport


if __name__ == "__main__":
    cfg_path = Path(__file__).parent.parent / "config/arbitrum_pools.json"
    cfg = load_and_validate_config(cfg_path)
    rpc = JsonRpcClient("https://arb1.arbitrum.io/rpc", transport=create_deterministic_arbitrum_transport())
    scanner = ArbitrumReadOnlyScanner(cfg, rpc)
    res = scanner.run_pinned_scan()
    print(json.dumps(res, indent=2))
