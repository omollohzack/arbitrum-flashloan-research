#!/usr/bin/env python3
"""
bot/route_optimizer.py — Profitability improvements (research-backed, v3.6)
============================================================================
Implements the three highest-leverage *off-chain* changes identified in the
2025-2026 Arbitrum flash-loan profitability research:

1. **Optimal trade sizing** — net profit as a function of trade size is
   unimodal: too small and fixed costs (gas) dominate; too large and your own
   price impact eats the spread. A fixed trade size is therefore
   systematically wrong. `find_optimal_amount()` ternary-searches the size
   that maximises net profit, evaluating every candidate with the existing
   fee-consistent model in `bot/profit_calculator.py` (executable quotes in,
   never spot prices).

2. **Zero-fee flash-loan source selection** — Balancer V2 and Morpho Blue
   charge 0% for flash loans; Aave V3 charges 0.05% (5 bps = $5 per $10k),
   which is often the entire gross margin on a major-pair route.
   `cheapest_flash_provider()` picks the cheapest provider whose *known*
   available liquidity covers the loan, preferring Balancer, then Morpho,
   then Aave as the deep fallback. Liquidity is an explicit input: callers
   must supply balances they have actually read on-chain. Unknown liquidity
   is treated as unavailable, never assumed.

3. **Execution gate (EV threshold)** — `passes_execution_gate()` refuses to
   execute unless net profit clears a minimum *and* covers the expected
   gas cost of failed attempts. With observed L2 cyclic-arb probe success
   rates of roughly 6-12%, a trade that only just breaks even on success is
   negative-EV once revert drain is counted. This mirrors the contract's
   on-chain `minProfit` requirement in `contracts/FlashLoanArbitrage.sol`:
   the off-chain gate decides whether to submit at all; the on-chain gate
   is the last line of defence.

Nothing in this module signs or broadcasts transactions, loads a private
key, or guarantees profit. A route that fails the gate is a *correct*
outcome: the expected result of these changes is a bot that trades less
often and loses less, and only acts when the measured edge is real.
"""

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).parent))

from profit_calculator import RouteProfitBreakdown, evaluate_exact_route_profit


# ---------------------------------------------------------------------------
# Flash-loan providers (fees verified 2026-11-09; see research report)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FlashProvider:
    name: str
    fee_bps: int
    # Preference order among equal fees: Balancer's multi-asset Vault is the
    # simplest integration, Morpho Blue is a low-overhead singleton, Aave V3
    # is the deepest fallback.
    preference: int


FLASH_PROVIDERS: Dict[str, FlashProvider] = {
    "balancer_v2": FlashProvider(name="balancer_v2", fee_bps=0, preference=0),
    "morpho_blue": FlashProvider(name="morpho_blue", fee_bps=0, preference=1),
    "aave_v3": FlashProvider(name="aave_v3", fee_bps=5, preference=2),
}


def flash_loan_fee_raw(provider_name: str, amount_in_raw: int) -> int:
    """Exact flash-loan fee in raw units (ceiling division, never undercounts)."""
    if provider_name not in FLASH_PROVIDERS:
        raise ValueError(f"Unknown flash provider: {provider_name}")
    if amount_in_raw < 0:
        raise ValueError("amount_in_raw cannot be negative")
    fee_bps = FLASH_PROVIDERS[provider_name].fee_bps
    return -(-amount_in_raw * fee_bps // 10_000)  # ceil


def cheapest_flash_provider(
    amount_in_raw: int,
    available_liquidity_raw: Dict[str, int],
) -> Optional[FlashProvider]:
    """
    Return the cheapest provider whose known liquidity covers `amount_in_raw`.

    `available_liquidity_raw` maps provider name -> balance the caller has
    actually verified on-chain. Providers absent from the mapping, or with a
    balance below the loan size, are not eligible. Returns None when no
    provider can fund the loan at that size.
    """
    if amount_in_raw <= 0:
        raise ValueError("amount_in_raw must be > 0")
    eligible = [
        provider
        for name, provider in FLASH_PROVIDERS.items()
        if available_liquidity_raw.get(name, 0) >= amount_in_raw
    ]
    if not eligible:
        return None
    return min(eligible, key=lambda p: (p.fee_bps, p.preference))


# ---------------------------------------------------------------------------
# Optimal trade sizing (ternary search over net profit)
# ---------------------------------------------------------------------------

# A quote function maps amount_in_raw -> quoted_amount_out_raw for the full
# route (both/all legs), e.g. a QuoterV2 exact-input quote. It must reflect
# price impact; spot prices are not a valid quote.
QuoteFn = Callable[[int], int]


@dataclass(frozen=True)
class OptimalAmountResult:
    amount_in_raw: int
    breakdown: RouteProfitBreakdown
    quote_evaluations: int
    is_executable: bool  # True only if the best found profit is > 0


def _evaluate(
    quote_fn: QuoteFn,
    amount_in_raw: int,
    profit_kwargs: Dict,
) -> RouteProfitBreakdown:
    return evaluate_exact_route_profit(
        amount_in_raw=amount_in_raw,
        quoted_amount_out_raw=quote_fn(amount_in_raw),
        **profit_kwargs,
    )


def find_optimal_amount(
    quote_fn: QuoteFn,
    *,
    min_amount_raw: int,
    max_amount_raw: int,
    profit_kwargs: Dict,
    iterations: int = 40,
) -> OptimalAmountResult:
    """
    Ternary-search the trade size that maximises net profit.

    Net profit(amount) is treated as unimodal (single-peaked), which holds
    for a fixed route when the quote function includes price impact: profit
    first rises as the spread is harvested, then falls as impact dominates.
    Endpoints are always evaluated too, so a monotone profit curve still
    returns its best endpoint.
    """
    if min_amount_raw <= 0 or max_amount_raw <= min_amount_raw:
        raise ValueError("Require 0 < min_amount_raw < max_amount_raw")
    if iterations < 1:
        raise ValueError("iterations must be >= 1")

    evaluations = 0
    cache: Dict[int, RouteProfitBreakdown] = {}

    def evaluate(amount: int) -> RouteProfitBreakdown:
        nonlocal evaluations
        if amount not in cache:
            cache[amount] = _evaluate(quote_fn, amount, profit_kwargs)
            evaluations += 1
        return cache[amount]

    lo, hi = min_amount_raw, max_amount_raw
    evaluate(lo)
    evaluate(hi)
    for _ in range(iterations):
        if hi - lo < 3:
            break
        third = (hi - lo) // 3
        m1 = lo + third
        m2 = hi - third
        if evaluate(m1).net_profit_raw < evaluate(m2).net_profit_raw:
            lo = m1 + 1
        else:
            hi = m2 - 1

    best_amount = max(cache, key=lambda a: cache[a].net_profit_raw)
    best = cache[best_amount]
    return OptimalAmountResult(
        amount_in_raw=best_amount,
        breakdown=best,
        quote_evaluations=evaluations,
        is_executable=best.net_profit_raw > 0,
    )


# ---------------------------------------------------------------------------
# Execution gate (expected-value threshold, incl. revert drain)
# ---------------------------------------------------------------------------

def passes_execution_gate(
    breakdown: RouteProfitBreakdown,
    *,
    min_net_profit_raw: int,
    expected_attempts_per_success: float = 1.0,
) -> Tuple[bool, List[str]]:
    """
    Decide whether a route is worth submitting at all.

    Conditions:
    - the breakdown itself is net positive;
    - net profit is at least `min_net_profit_raw` (the caller's buffer,
      mirroring the on-chain `minProfit`);
    - net profit covers the gas expected to be burned by failed attempts:
      with `expected_attempts_per_success` = A, a successful trade must pay
      for the gas of (A - 1) reverted attempts as well as its own, i.e.
      net_profit (which is already net of one gas charge) must be at least
      (A - 1) x gas_cost. At A=1 (every attempt succeeds) this adds nothing.

    Returns (passed, reasons) — reasons is empty when the gate passes.
    """
    if min_net_profit_raw < 0:
        raise ValueError("min_net_profit_raw cannot be negative")
    if expected_attempts_per_success < 1.0:
        raise ValueError("expected_attempts_per_success must be >= 1.0")

    reasons: List[str] = []
    if not breakdown.is_net_positive:
        reasons.append(
            f"net profit is not positive ({breakdown.net_profit_raw} raw)"
        )
    if breakdown.net_profit_raw < min_net_profit_raw:
        reasons.append(
            f"net profit {breakdown.net_profit_raw} raw is below the minimum "
            f"{min_net_profit_raw} raw"
        )
    expected_revert_gas_raw = int(
        (expected_attempts_per_success - 1.0) * breakdown.gas_cost_quote_raw
    )
    if breakdown.net_profit_raw < expected_revert_gas_raw:
        reasons.append(
            f"net profit {breakdown.net_profit_raw} raw does not cover expected "
            f"revert gas {expected_revert_gas_raw} raw at "
            f"{expected_attempts_per_success} attempts per success"
        )
    return (len(reasons) == 0, reasons)
