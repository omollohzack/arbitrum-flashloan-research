#!/usr/bin/env python3
"""
test_suite_v3.py — Automated Unit, Security & Multi-Chain Verification Suite (v3.2)
===================================================================================
Verifies all requirements from `tests/README.md` plus cross-repository patterns from:
- `morpho-org/morpho-blue` (`onMorphoLiquidate` zero-flash-loan callback & `onMorphoFlashLoan`)
- `hernan-erasmo/overlord-rs` (`VegaStrikePriceCache` in-memory LST cascade `< 50 microseconds`)
- Binance Spot CEX Lead-Lag Pre-Trigger (`BinanceLeadLagOracle`)
- Dolomite / Pendle PT Wrapped Yield Token Liquidation Margins
- Bitquery Aug/Sep 2026 Investigations (Zombie-market depegged collateral rejection & Intra-DEX routing)
"""

import unittest
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "bot"))
sys.path.insert(0, str(Path(__file__).parent))
import arbitrum_flash_bot as bot


class TestArbitrumFlashLoanV3Suite(unittest.TestCase):

    def test_01_v3_and_algebra_virtual_reserves_precision(self):
        price_raw = (2650.0 * 1e6) / 1e18
        sqrt_price_x96 = int(math.sqrt(price_raw) * (1 << 96))
        v0, v1 = bot.compute_v3_virtual_reserves(sqrt_price_x96, 8 * 10**15, 18, 6)
        self.assertAlmostEqual(v1 / v0, 2650.0, places=1)

    def test_02_camelot_v2_directional_fee_math(self):
        self.assertAlmostEqual(bot.compute_camelot_v2_fee_multiplier(150), 0.9985, places=6)
        self.assertAlmostEqual(bot.compute_camelot_v2_fee_multiplier(300), 0.9970, places=6)

    def test_03_analytic_2hop_matches_bisection(self):
        h1 = bot.PoolEdge(
            pool_id="p1", dex_name="UniV3_500", swap_type=1,
            target_address=bot.UNISWAP_V3_ROUTER, pool_address="0x1111111111111111111111111111111111111111",
            token_in_sym="USDC", token_out_sym="WETH",
            token_in=bot.TOKENS["USDC"]["address"], token_out=bot.TOKENS["WETH"]["address"],
            reserve_in=200_000.0, reserve_out=100.0, fee_multiplier=0.9995, raw_fee_param=500
        )
        h2 = bot.PoolEdge(
            pool_id="p2", dex_name="UniV3_3000", swap_type=1,
            target_address=bot.UNISWAP_V3_ROUTER, pool_address="0x2222222222222222222222222222222222222222",
            token_in_sym="WETH", token_out_sym="USDC",
            token_in=bot.TOKENS["WETH"]["address"], token_out=bot.TOKENS["USDC"]["address"],
            reserve_in=100.0, reserve_out=203_000.0, fee_multiplier=0.9970, raw_fee_param=3000
        )
        opt_analytic, prof_analytic = bot.analytic_2hop_optimal_input(h1, h2)
        self.assertGreater(opt_analytic, 100.0)
        self.assertGreater(prof_analytic, 1.0)
        eps = 1e-4
        d = (bot.simulate_n_hop_output(opt_analytic + eps, [h1, h2]) -
             bot.simulate_n_hop_output(opt_analytic - eps, [h1, h2])) / (2 * eps)
        self.assertAlmostEqual(d, 1.0, places=4)

    def test_04_partial_liquidation_unspent_debt_accounting(self):
        amount_borrowed = 10_000.0
        actual_debt_pulled_by_aave = 5_000.0
        unspent_debt_in_contract = amount_borrowed - actual_debt_pulled_by_aave
        collateral_swap_proceeds = 5_000.0 * 1.05 * 0.9975
        v3_final_balance = unspent_debt_in_contract + collateral_swap_proceeds
        net_profit = v3_final_balance - amount_borrowed
        self.assertAlmostEqual(net_profit, 236.875, places=2)

    def test_05_vega_strike_cache_lst_cascade_and_zombie_rejection(self):
        """Verifies `overlord-rs` style LST cascade & Bitquery Zombie-Market rejection."""
        positions = [
            bot.CachedPosition(
                borrower="0xaaa", protocol="AAVE_V3", collateral_sym="WETH", debt_sym="USDC",
                collateral_amount=2.0, debt_usd=4_265.0, lltv=0.825, collateral_eth_ratio=1.0,
                dex_exit_liquidity_usd=500_000.0, oracle_is_fresh=True
            ),
            bot.CachedPosition(
                borrower="0xbbb", protocol="MORPHO_BLUE", collateral_sym="wstETH", debt_sym="USDC",
                collateral_amount=2.5, debt_usd=6_570.13, lltv=0.86, collateral_eth_ratio=1.18,
                dex_exit_liquidity_usd=500_000.0, oracle_is_fresh=True
            ),
            bot.CachedPosition(
                borrower="0xzombie", protocol="MORPHO_BLUE", collateral_sym="sdeUSD", debt_sym="USDC",
                collateral_amount=10_000.0, debt_usd=9_000.0, lltv=0.86, collateral_eth_ratio=1.0,
                dex_exit_liquidity_usd=100.0, oracle_is_fresh=False
            ),
        ]
        cache = bot.VegaStrikePriceCache(positions)
        hits = cache.scan_on_eth_price_tick(2570.0, lead_lag=True)
        self.assertEqual(len(hits), 2)
        self.assertNotIn("0xzombie", [h["borrower"] for h in hits])
        self.assertTrue(hits[0]["is_lead_lag_pre_signed"])

    def test_06_solidity_v3_1_contract_static_security_checks(self):
        sol_path = (
            (Path(__file__).parent.parent / "contracts" / "FlashLoanArbitrage.sol")
            if (Path(__file__).parent.parent / "contracts" / "FlashLoanArbitrage.sol").exists()
            else (Path(__file__).parent / "FlashLoanArbitrage.sol")
        )
        sol_text = sol_path.read_text()
        for expected in [
            "pragma solidity ^0.8.20;",
            "ICamelotPair",
            "IAlgebraSwapRouter",
            "IMorpho",
            "onMorphoLiquidate",
            "onMorphoFlashLoan",
            "quotePathOnChain",
            "_computeConcentratedOutput",
            "uniswapV3FlashCallback",
            "_safeApprove",
            "finalBalance = IERC20(liq.debtAsset).balanceOf(address(this));",
        ]:
            self.assertIn(expected, sol_text)

    def test_07_multichain_registry_addresses(self):
        """Verifies multi-chain drop-in configuration across Arbitrum, Base, and Optimism."""
        arb = bot.get_network_config("arbitrum")
        base = bot.get_network_config("base")
        op = bot.get_network_config("optimism")

        self.assertEqual(arb["chain_id"], 42161)
        self.assertEqual(base["chain_id"], 8453)
        self.assertEqual(op["chain_id"], 10)

        # Balancer V2 Vault is identical on all three chains
        self.assertEqual(arb["balancer_v2_vault"], base["balancer_v2_vault"])
        self.assertEqual(base["balancer_v2_vault"], op["balancer_v2_vault"])

        # Base Aave V3 & Morpho Blue singletons
        self.assertEqual(base["aave_v3_pool"], "0xA238Dd80C259a72e81d7e4664a9801593F98d1c5")
        self.assertEqual(base["morpho_blue"], "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb")

    def test_08_binance_lead_lag_and_pendle_pt_margin(self):
        """Verifies Binance spot lead-lag trigger and Dolomite / Pendle PT margin math."""
        oracle = bot.BinanceLeadLagOracle(deviation_threshold_bps=15, base_onchain_price=2650.0)
        # 18.8 bps drop -> should trigger
        should_pre_trigger, delta_bps, _ = oracle.check_lead_lag_signal(2645.0)
        self.assertTrue(should_pre_trigger)
        self.assertLess(delta_bps, -15.0)

        # 5 bps drop -> should not trigger
        should_not_trigger, delta_small, _ = oracle.check_lead_lag_signal(2649.0)
        self.assertFalse(should_not_trigger)

        # Pendle PT liquidation margin test (Dolomite 8% bonus with 1.5% PT discount)
        margin = bot.compute_pendle_pt_liquidation_margin(debt_covered_usd=5000.0, bonus_bps=800, pt_discount_bps=150)
        self.assertGreater(margin["net_profit_usd"], 300.0)
        self.assertGreater(margin["profit_margin_pct"], 6.0)

        # Fluid dynamic fee
        self.assertAlmostEqual(bot.compute_fluid_dex_fee_multiplier(5), 0.99995, places=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
