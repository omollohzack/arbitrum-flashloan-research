"""Read-only eth_call / eth_estimateGas simulator for prepared calldata.

This does not build calldata, sign, broadcast, or execute a transaction. The
caller must supply the exact target, calldata and sender for a candidate call.
An eth_call success is only a preliminary check; it is not a guarantee of
inclusion or profitability.
"""
from __future__ import annotations
import json
import os
import sys
import urllib.request
from typing import Any

class RpcError(RuntimeError):
    pass

class Rpc:
    def __init__(self, url: str, timeout: int = 20):
        self.url, self.timeout, self.id = url, timeout, 0

    def request(self, method: str, params: list[Any]) -> Any:
        self.id += 1
        body = json.dumps({"jsonrpc":"2.0","id":self.id,"method":method,"params":params}).encode()
        req = urllib.request.Request(self.url, data=body,
            headers={"Content-Type":"application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                result = json.loads(response.read().decode())
        except Exception as exc:
            raise RpcError(f"RPC request failed: {exc}") from exc
        if "error" in result:
            raise RpcError(f'{method} failed: {result["error"]}')
        return result.get("result")

def simulate_call(rpc: Rpc, tx: dict[str, Any]) -> dict[str, Any]:
    if not tx.get("to") or not tx.get("data", "").startswith("0x"):
        raise ValueError("transaction requires 'to' and 0x-prefixed 'data'")
    call = {"to": tx["to"], "data": tx["data"]}
    for key in ("from", "value", "gas", "maxFeePerGas", "maxPriorityFeePerGas"):
        if key in tx:
            call[key] = tx[key]
    result: dict[str, Any] = {"mode":"READ_ONLY", "submitted":False}
    try:
        result["eth_call"] = rpc.request("eth_call", [call, "latest"])
        result["call_succeeded"] = True
    except Exception as exc:
        result["call_succeeded"] = False
        result["call_error"] = str(exc)
        return result
    try:
        result["estimated_gas"] = int(rpc.request("eth_estimateGas", [call]), 16)
    except Exception as exc:
        result["gas_estimate_error"] = str(exc)
    result["warning"] = "No transaction was signed or sent. State can change before any real inclusion."
    return result

def main() -> int:
    url = os.getenv("ARBITRUM_RPC_URL")
    tx_json = os.getenv("SIMULATION_TX_JSON")
    if not url or not tx_json:
        print("Set ARBITRUM_RPC_URL and SIMULATION_TX_JSON (JSON with to, data, optional from/value).", file=sys.stderr)
        print("No transaction was sent.", file=sys.stderr)
        return 2
    try:
        tx = json.loads(tx_json)
        print(json.dumps(simulate_call(Rpc(url), tx), indent=2))
    except Exception as exc:
        print(f"Simulation failed safely: {exc}", file=sys.stderr)
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
