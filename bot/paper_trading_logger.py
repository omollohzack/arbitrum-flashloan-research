#!/usr/bin/env python3
"""
bot/paper_trading_logger.py — Continuous Read-Only Paper-Trading & Next-Block Persistence Scorer (v3.4)
=======================================================================================================
Patches Hole #4 by providing a quantitative pre-deployment validation harness:
1. Executes `N` pinned-block read-only scan cycles and logs every cycle to a JSONL file.
2. Measures **Next-Block Persistence (`B -> B+1`)**:
   - In real L2 execution (Messias & Torres 2026, `arXiv:2509.22143v2`), you detect an opportunity at
     block `B`, but your transaction lands in block `B + 1` (or later).
   - Whenever a route is `is_net_positive=True` at cycle `k` (block `B`), this harness checks whether
     that exact `route_id` remains `is_net_positive=True` in cycle `k + 1` (block `B' > B`).
3. Produces an objective **Deployment Readiness Scorecard** (`READY_FOR_FORK_SIM` vs `KEEP_COLLECTING_DATA`)
   so you never deploy a contract or fund gas blindly.
"""

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent))
from opportunity_scanner import (
    ArbitrumReadOnlyScanner,
    JsonRpcClient,
    load_and_validate_config,
)


@dataclass(frozen=True)
class PaperTradingScorecard:
    schema_version: str
    total_cycles_run: int
    healthy_cycles: int
    unhealthy_cycles: int
    distinct_blocks_observed: int
    avg_scan_duration_ms: float
    multicall3_usage_rate: float
    cycles_with_indicative_spread: int
    cycles_with_net_positive_quote: int
    next_block_persistence_checks: int
    next_block_persisted_hits: int
    next_block_persistence_rate: float
    best_net_profit_raw: int
    best_net_profit_human: str
    best_route_id: Optional[str]
    verdict: str                      # "READY_FOR_FORK_SIM" or "KEEP_COLLECTING_DATA"
    verdict_rationale: str

    def to_dict(self) -> Dict:
        return asdict(self)


def run_paper_trading_campaign(
    *,
    config: Dict,
    rpc_factory: Callable[[int], JsonRpcClient],
    cycles: int = 5,
    interval_seconds: float = 0.0,
    output_jsonl_path: Optional[str | Path] = None,
    min_persisted_hits_for_ready: int = 1,
    min_persistence_rate_for_ready: float = 0.25,
) -> PaperTradingScorecard:
    """
    Runs `cycles` read-only pinned-block scans, tracks block-to-block route persistence,
    writes JSONL telemetry if `output_jsonl_path` is provided, and returns a `PaperTradingScorecard`.
    """
    if cycles < 1:
        raise ValueError("cycles must be >= 1")

    reports: List[Dict] = []
    unhealthy_count = 0
    jsonl_file = Path(output_jsonl_path) if output_jsonl_path else None
    if jsonl_file:
        jsonl_file.parent.mkdir(parents=True, exist_ok=True)

    for idx in range(cycles):
        rpc = rpc_factory(idx)
        scanner = ArbitrumReadOnlyScanner(config, rpc)
        try:
            rep = scanner.run_pinned_scan(use_multicall3=True)
            reports.append(rep)
            if jsonl_file:
                with jsonl_file.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rep) + "\n")
        except Exception as exc:
            unhealthy_count += 1
            if jsonl_file:
                err_rec = {
                    "cycle": idx,
                    "health_status": "UNHEALTHY",
                    "error_category": type(exc).__name__,
                    "error": str(exc),
                }
                with jsonl_file.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(err_rec) + "\n")

        if interval_seconds > 0 and idx + 1 < cycles:
            time.sleep(interval_seconds)

    healthy_count = len(reports)
    distinct_blocks = len({r["block_number"] for r in reports})
    avg_duration = (
        sum(r["telemetry"]["total_scan_duration_ms"] for r in reports) / healthy_count
        if healthy_count > 0
        else 0.0
    )
    multicall_rate = (
        sum(1 for r in reports if r["telemetry"].get("multicall3_batched")) / healthy_count
        if healthy_count > 0
        else 0.0
    )

    indicative_cycles = 0
    net_pos_cycles = 0
    best_profit_raw = -10**30
    best_profit_human = "0.000000"
    best_route_id: Optional[str] = None

    for r in reports:
        routes = r.get("route_evaluations", [])
        if any(rt["indicative_spot_screen"]["passes_fee_hurdle_for_quoting"] for rt in routes):
            indicative_cycles += 1
        if any(rt["exact_quote_profit_breakdown"]["is_net_positive"] for rt in routes):
            net_pos_cycles += 1
        for rt in routes:
            pb = rt["exact_quote_profit_breakdown"]
            if pb["net_profit_raw"] > best_profit_raw:
                best_profit_raw = pb["net_profit_raw"]
                best_profit_human = pb["net_profit_human"]
                best_route_id = rt["route_id"]

    if best_profit_raw == -10**30:
        best_profit_raw = 0

    # Evaluate block-to-block persistence (Cycle k -> Cycle k+1)
    persistence_checks = 0
    persisted_hits = 0
    for idx in range(len(reports) - 1):
        curr_rep = reports[idx]
        next_rep = reports[idx + 1]
        if next_rep["block_number"] <= curr_rep["block_number"]:
            continue

        pos_routes_curr = {
            rt["route_id"]
            for rt in curr_rep.get("route_evaluations", [])
            if rt["exact_quote_profit_breakdown"]["is_net_positive"]
        }
        if not pos_routes_curr:
            continue

        persistence_checks += 1
        pos_routes_next = {
            rt["route_id"]
            for rt in next_rep.get("route_evaluations", [])
            if rt["exact_quote_profit_breakdown"]["is_net_positive"]
        }
        if pos_routes_curr & pos_routes_next:
            persisted_hits += 1

    persistence_rate = (persisted_hits / persistence_checks) if persistence_checks > 0 else 0.0

    is_ready = (
        unhealthy_count == 0
        and persisted_hits >= min_persisted_hits_for_ready
        and persistence_rate >= min_persistence_rate_for_ready
    )
    verdict = "READY_FOR_FORK_SIM" if is_ready else "KEEP_COLLECTING_DATA"
    if is_ready:
        rationale = (
            f"Observed {persisted_hits}/{persistence_checks} ({persistence_rate*100:.1f}%) net-positive routes "
            f"persisting across consecutive Arbitrum blocks with 0 unhealthy cycles. Proceed to Anvil fork simulation."
        )
    else:
        rationale = (
            f"Insufficient multi-block persistence ({persisted_hits}/{persistence_checks} checks persisted, "
            f"unhealthy_cycles={unhealthy_count}). Do not deploy capital yet; continue read-only logging or "
            f"target higher-margin liquidation/keeper lanes."
        )

    return PaperTradingScorecard(
        schema_version="3.4-paper-trading-scorecard",
        total_cycles_run=cycles,
        healthy_cycles=healthy_count,
        unhealthy_cycles=unhealthy_count,
        distinct_blocks_observed=distinct_blocks,
        avg_scan_duration_ms=round(avg_duration, 3),
        multicall3_usage_rate=round(multicall_rate, 4),
        cycles_with_indicative_spread=indicative_cycles,
        cycles_with_net_positive_quote=net_pos_cycles,
        next_block_persistence_checks=persistence_checks,
        next_block_persisted_hits=persisted_hits,
        next_block_persistence_rate=round(persistence_rate, 4),
        best_net_profit_raw=best_profit_raw,
        best_net_profit_human=best_profit_human,
        best_route_id=best_route_id,
        verdict=verdict,
        verdict_rationale=rationale,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Continuous Read-Only Paper-Trading Logger & Next-Block Persistence Scorer (v3.4)"
    )
    parser.add_argument(
        "--config",
        default=str(Path(__file__).parent.parent / "config/arbitrum_pools.json"),
        help="Path to validated Arbitrum One pool configuration JSON",
    )
    parser.add_argument(
        "--rpc-url",
        default="https://arb1.arbitrum.io/rpc",
        help="Arbitrum One JSON-RPC endpoint",
    )
    parser.add_argument("--cycles", type=int, default=5, help="Number of scan cycles to run")
    parser.add_argument("--interval-sec", type=float, default=1.0, help="Seconds between scan cycles")
    parser.add_argument("--output-jsonl", default="", help="Optional path to append JSONL scan records")
    args = parser.parse_args()

    cfg = load_and_validate_config(args.config)
    scorecard = run_paper_trading_campaign(
        config=cfg,
        rpc_factory=lambda _: JsonRpcClient(args.rpc_url),
        cycles=args.cycles,
        interval_seconds=args.interval_sec,
        output_jsonl_path=args.output_jsonl or None,
    )
    print(json.dumps(scorecard.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
