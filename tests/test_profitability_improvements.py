#!/usr/bin/env python3
"""
tests/test_profitability_improvements.py — v3.6 profitability changes
======================================================================
Verifies the research-backed changes in `bot/route_optimizer.py`:
1. Optimal trade sizing beats a fixed size on a unimodal profit curve.
2. A route with no positive size is reported non-executable (the current
   major-pair reality), not dressed up as an opportunity.
3. Zero-fee flash providers (Balancer/Morpho) are preferred over Aave's
   5 bps, with Aave as fallback only when its liquidity is the one that
   covers the loan; unknown liquidity is never assumed.
4. The execution gate rejects net-negative routes, routes below the
   caller's minimum profit, and routes whose profit cannot cover the
   expected gas of failed (reverted) attempts.
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / "bot"))

import route_optimizer as opt
from profit_calculator import evaluate_exact_route_profit

USDC_DECIMALS = 6
PROFIT_KWARGS = dict(
    quote_token_symbol="USDC",
    quote_token_decimals=USDC_DECIMALS,
    quotes_are_fee_inclusive=True,
    hop_fees_bps=[5, 5],
    flash_loan_fee_bps=0,
    slippage_buffer_bps=5,
)

# Impact constant: with a 1% gross edge, net profit peaks near $5,000.
K_IMPACT = 1_052_631_578_947


def peaked_quote(amount_in_raw: int) -> int:
    """Simulated exact-input quote: +1% gross edge, quadratic price impact."""
    return amount_in_raw * 101 // 100 - (amount_in_raw * amount_in_raw) // K_IMPACT


def losing_quote(amount_in_raw: int) -> int:
    """Simulated quote that is worse than break-even at every size."""
    return amount_in_raw * 998 // 1000


class TestOptimalSizing(unittest.TestCase):
    def test_optimizer_beats_fixed_sizes(self):
        result = opt.find_optimal_amount(
            peaked_quote,
            min_amount_raw=10_000_000,          # $10
            max_amount_raw=50_000_000_000,      # $50,000
            profit_kwargs=PROFIT_KWARGS,
        )
        self.assertTrue(result.is_executable)
        # Analytic optimum is ~$5,000; allow a wide band for the search.
        self.assertTrue(2_500_000_000 < result.amount_in_raw < 10_000_000_000)

        fixed_small = evaluate_exact_route_profit(
            amount_in_raw=1_000_000_000,  # the repo's old fixed $1,000
            quoted_amount_out_raw=peaked_quote(1_000_000_000),
            **PROFIT_KWARGS,
        )
        fixed_huge = evaluate_exact_route_profit(
            amount_in_raw=50_000_000_000,
            quoted_amount_out_raw=peaked_quote(50_000_000_000),
            **PROFIT_KWARGS,
        )
        self.assertGreater(result.breakdown.net_profit_raw, fixed_small.net_profit_raw)
        self.assertGreater(result.breakdown.net_profit_raw, fixed_huge.net_profit_raw)
        self.assertLess(fixed_huge.net_profit_raw, 0)

    def test_no_positive_size_is_not_executable(self):
        result = opt.find_optimal_amount(
            losing_quote,
            min_amount_raw=10_000_000,
            max_amount_raw=50_000_000_000,
            profit_kwargs=PROFIT_KWARGS,
        )
        self.assertFalse(result.is_executable)
        self.assertLessEqual(result.breakdown.net_profit_raw, 0)


class TestFlashProviderSelection(unittest.TestCase):
    AMOUNT = 10_000_000_000  # $10,000 in USDC raw units

    def test_zero_fee_balancer_preferred_and_saves_five_dollars(self):
        liquidity = {"balancer_v2": self.AMOUNT, "morpho_blue": self.AMOUNT, "aave_v3": self.AMOUNT}
        provider = opt.cheapest_flash_provider(self.AMOUNT, liquidity)
        self.assertEqual(provider.name, "balancer_v2")
        self.assertEqual(opt.flash_loan_fee_raw("balancer_v2", self.AMOUNT), 0)
        # Aave at 5 bps on $10,000 = $5.00 exactly (5,000,000 raw USDC).
        self.assertEqual(opt.flash_loan_fee_raw("aave_v3", self.AMOUNT), 5_000_000)

    def test_fallback_order_and_unknown_liquidity(self):
        provider = opt.cheapest_flash_provider(
            self.AMOUNT, {"morpho_blue": self.AMOUNT, "aave_v3": self.AMOUNT}
        )
        self.assertEqual(provider.name, "morpho_blue")

        provider = opt.cheapest_flash_provider(self.AMOUNT, {"aave_v3": self.AMOUNT})
        self.assertEqual(provider.name, "aave_v3")

        # Balancer listed but too shallow, and nothing else known -> None.
        provider = opt.cheapest_flash_provider(self.AMOUNT, {"balancer_v2": self.AMOUNT - 1})
        self.assertIsNone(provider)


class TestExecutionGate(unittest.TestCase):
    def _breakdown(self, quoted_out: int, amount: int = 1_000_000_000):
        return evaluate_exact_route_profit(
            amount_in_raw=amount,
            quoted_amount_out_raw=quoted_out,
            **PROFIT_KWARGS,
        )

    def test_gate_accepts_clearly_positive_route(self):
        breakdown = self._breakdown(1_020_000_000)  # +2% gross on $1,000
        passed, reasons = opt.passes_execution_gate(
            breakdown, min_net_profit_raw=1_000_000, expected_attempts_per_success=1.0
        )
        self.assertTrue(passed, reasons)

    def test_gate_rejects_negative_and_below_minimum(self):
        breakdown = self._breakdown(999_000_000)
        passed, reasons = opt.passes_execution_gate(
            breakdown, min_net_profit_raw=1_000_000
        )
        self.assertFalse(passed)
        self.assertTrue(any("not positive" in r for r in reasons))

    def test_gate_rejects_profit_that_cannot_cover_revert_drain(self):
        breakdown = self._breakdown(1_020_000_000)
        self.assertGreater(breakdown.net_profit_raw, 0)
        # At 1,000 attempts per success the expected revert gas dwarfs profit.
        passed, reasons = opt.passes_execution_gate(
            breakdown, min_net_profit_raw=0, expected_attempts_per_success=1000.0
        )
        self.assertFalse(passed)
        self.assertTrue(any("revert gas" in r for r in reasons))


if __name__ == "__main__":
    unittest.main()
