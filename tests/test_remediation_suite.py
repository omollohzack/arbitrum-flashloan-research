#!/usr/bin/env python3
"""
tests/test_remediation_suite.py — Systematic Regression Test Suite for Findings 1–10 + v3.5 Modules
====================================================================================================
Tests every remediation required by `project_technical_review_for_gemini.md`, the v3.4 patches,
and the v3.5 modules:
- Patch 1: Single-RPC `Multicall3.tryAggregate` (`0xbce38bd7`) state batching & automatic fallback
- Patch 2: Live Arbitrum `NodeInterface.gasEstimateComponents` (`0x4d2301cc`) L1+L2 gas decomposition
- Patch 3: Read-only Aave V3 (`getUserAccountData` `0xbf92857c`) liquidation scanner & permissionless
  yield-vault (`callReward()` `0x6b29a679`) keeper bounty scanner
- Patch 4: Multi-block (`B -> B+1`) spread persistence validator & Go/No-Go paper-trading scorecard
- Patch 5 (v3.5): Cross-Chain Speculative OEV Monitor (`arXiv:2606.03434v1` Optimism-leads-Arbitrum
  6-second Chainlink window & projected Health Factor Equation 2)
- Patch 6 (v3.5): Zero-Capital UniswapX Dutch-Auction Intent Filler Evaluator (`Uniswap/UniswapX`
  `reactorCallback` & `arXiv:2403.02525v1`) + Uniswap V4 `unlockCallback` singleton flash accounting
"""

import copy
import sys
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / "bot"))
sys.path.insert(0, str(Path(__file__).parent))

import cross_chain_oev_monitor as oev_mod
import liquidation_and_keeper_scanner as liq_mod
import opportunity_scanner as scanner_mod
import paper_trading_logger as paper_mod
import profit_calculator as calc_mod
import scanner_stub as stub_mod
import transaction_simulator as sim_mod
import uniswapx_filler_evaluator as unix_mod


class TestTechnicalReviewRemediationSuite(unittest.TestCase):

    def setUp(self) -> None:
        self.valid_config_path = REPO_ROOT / "config/arbitrum_pools.json"
        self.example_config_path = REPO_ROOT / "config/arbitrum_pools.example.json"
        self.valid_cfg = scanner_mod.load_and_validate_config(self.valid_config_path)

    def test_01_readme_removes_unsupported_profit_promises_and_risk_free_claims(self) -> None:
        readme_path = REPO_ROOT / "README.md"
        text = readme_path.read_text(encoding="utf-8")
        for forbidden in [
            "Average $124.94/Day",
            "How to Reach $100/Day",
            "your principal is never at risk",
        ]:
            self.assertNotIn(forbidden, text, f"Forbidden claim still present in README: {forbidden}")

        for required_section in [
            "Observed & Tested",
            "Assumed",
            "Not Implemented",
            "Operational & Smart-Contract Risk Disclosure",
        ]:
            self.assertIn(required_section, text)

    def test_02_rejects_example_template_config(self) -> None:
        with self.assertRaises(scanner_mod.ConfigValidationError):
            scanner_mod.load_and_validate_config(self.example_config_path)

    def test_03_rejects_placeholder_and_zero_addresses(self) -> None:
        bad_cfg = copy.deepcopy(self.valid_cfg)
        bad_cfg["pools"][0]["address"] = "0x0000000000000000000000000000000000000011"
        with self.assertRaises(scanner_mod.ConfigValidationError):
            scanner_mod.validate_pool_config_dict(bad_cfg)

        bad_cfg2 = copy.deepcopy(self.valid_cfg)
        bad_cfg2["tokens"]["USDC"]["address"] = "0x0000000000000000000000000000000000000000"
        with self.assertRaises(scanner_mod.ConfigValidationError):
            scanner_mod.validate_pool_config_dict(bad_cfg2)

    def test_04_rejects_pair_orientation_mismatch_and_duplicate_pools(self) -> None:
        bad_cfg = copy.deepcopy(self.valid_cfg)
        bad_cfg["pools"][1]["token0"] = "USDC"
        bad_cfg["pools"][1]["token1"] = "WETH"
        with self.assertRaises(scanner_mod.ConfigValidationError):
            scanner_mod.validate_pool_config_dict(bad_cfg)

        dup_cfg = copy.deepcopy(self.valid_cfg)
        dup_cfg["pools"][1]["address"] = dup_cfg["pools"][0]["address"]
        with self.assertRaises(scanner_mod.ConfigValidationError):
            scanner_mod.validate_pool_config_dict(dup_cfg)

    def test_05_fails_closed_on_wrong_chain_id(self) -> None:
        transport = stub_mod.create_deterministic_arbitrum_transport(chain_id=1)
        rpc = scanner_mod.JsonRpcClient("https://arb1.arbitrum.io/rpc", transport=transport)
        scanner = scanner_mod.ArbitrumReadOnlyScanner(self.valid_cfg, rpc)
        with self.assertRaises(scanner_mod.ChainIdMismatchError):
            scanner.run_pinned_scan()

    def test_06_fails_closed_on_missing_bytecode(self) -> None:
        base_transport = stub_mod.create_deterministic_arbitrum_transport()
        def missing_code_transport(method: str, payload: dict):
            if method == "eth_getCode":
                return {"jsonrpc": "2.0", "id": payload["id"], "result": "0x"}, 0.2
            return base_transport(method, payload)

        rpc = scanner_mod.JsonRpcClient("https://arb1.arbitrum.io/rpc", transport=missing_code_transport)
        scanner = scanner_mod.ArbitrumReadOnlyScanner(self.valid_cfg, rpc)
        with self.assertRaises(scanner_mod.ConfigValidationError):
            scanner.run_pinned_scan()

    def test_07_fails_closed_on_onchain_decimals_mismatch(self) -> None:
        bad_cfg = copy.deepcopy(self.valid_cfg)
        bad_cfg["tokens"]["USDC"]["decimals"] = 18
        transport = stub_mod.create_deterministic_arbitrum_transport()
        rpc = scanner_mod.JsonRpcClient("https://arb1.arbitrum.io/rpc", transport=transport)
        scanner = scanner_mod.ArbitrumReadOnlyScanner(bad_cfg, rpc)
        with self.assertRaises(scanner_mod.ConfigValidationError):
            scanner.run_pinned_scan()

    def test_08_pinned_block_and_dex_specific_adapters_execution(self) -> None:
        transport = stub_mod.create_deterministic_arbitrum_transport(block_number=265_123_456)
        rpc = scanner_mod.JsonRpcClient("https://arb1.arbitrum.io/rpc", transport=transport)
        scanner = scanner_mod.ArbitrumReadOnlyScanner(self.valid_cfg, rpc)
        report = scanner.run_pinned_scan(trade_token_sym="USDC", amount_in_raw=1_000_000_000)

        self.assertEqual(report["health_status"], "HEALTHY")
        self.assertEqual(report["chain_id"], 42161)
        self.assertEqual(report["block_number"], 265_123_456)
        self.assertEqual(len(report["pool_snapshots"]), 3)
        self.assertTrue(report["telemetry"]["multicall3_batched"])

        for snap in report["pool_snapshots"]:
            self.assertEqual(snap["block_number"], 265_123_456)
            self.assertEqual(snap["block_hash"], report["block_hash"])

        top_route = report["route_evaluations"][0]
        self.assertEqual(
            top_route["indicative_spot_screen"]["classification"],
            "INDICATIVE_ONLY_SPOT_SCREEN",
        )
        self.assertFalse(top_route["indicative_spot_screen"]["executable"])

        pb = top_route["exact_quote_profit_breakdown"]
        self.assertEqual(pb["amount_in_raw"], 1_000_000_000)
        self.assertGreater(pb["gross_amount_out_raw"], 1_000_000_000)
        self.assertEqual(pb["classification"], "ESTIMATED_NET_POSITIVE_UNVERIFIED")

    def test_09_large_trade_size_flips_net_profit_negative_due_to_price_impact(self) -> None:
        transport = stub_mod.create_deterministic_arbitrum_transport()
        rpc = scanner_mod.JsonRpcClient("https://arb1.arbitrum.io/rpc", transport=transport)
        scanner = scanner_mod.ArbitrumReadOnlyScanner(self.valid_cfg, rpc)

        small_report = scanner.run_pinned_scan(trade_token_sym="USDC", amount_in_raw=1_000 * 10**6)
        best_small = small_report["route_evaluations"][0]["exact_quote_profit_breakdown"]
        self.assertTrue(best_small["is_net_positive"])

        huge_report = scanner.run_pinned_scan(trade_token_sym="USDC", amount_in_raw=5_000_000 * 10**6)
        for route in huge_report["route_evaluations"]:
            self.assertFalse(route["exact_quote_profit_breakdown"]["is_net_positive"])

    def test_10_profit_calculator_prevents_fee_double_counting(self) -> None:
        res_inclusive = calc_mod.evaluate_exact_route_profit(
            quote_token_symbol="USDC",
            quote_token_decimals=6,
            amount_in_raw=1_000_000_000,
            quoted_amount_out_raw=1_003_500_000,
            quotes_are_fee_inclusive=True,
            hop_fees_bps=[5, 15],
            flash_loan_fee_bps=0,
            l2_gas_units=240_000,
            l2_gas_price_wei=20_000_000,
            l1_data_fee_wei=13_920_000_000_000,
            eth_to_quote_rate=Decimal("2650.00"),
            slippage_buffer_bps=5,
        )
        self.assertEqual(res_inclusive.explicit_hop_fee_deduction_raw, 0)
        self.assertEqual(res_inclusive.effective_amount_out_raw, 1_003_500_000)
        self.assertTrue(res_inclusive.is_net_positive)

        res_gross = calc_mod.evaluate_exact_route_profit(
            quote_token_symbol="USDC",
            quote_token_decimals=6,
            amount_in_raw=1_000_000_000,
            quoted_amount_out_raw=1_003_500_000,
            quotes_are_fee_inclusive=False,
            hop_fees_bps=[5, 15],
            flash_loan_fee_bps=0,
            l2_gas_units=240_000,
            l2_gas_price_wei=20_000_000,
            l1_data_fee_wei=13_920_000_000_000,
            eth_to_quote_rate=Decimal("2650.00"),
            slippage_buffer_bps=5,
        )
        self.assertGreater(res_gross.explicit_hop_fee_deduction_raw, 1_900_000)
        self.assertLess(res_gross.net_profit_raw, res_inclusive.net_profit_raw)

    def test_11_profit_calculator_rejects_invalid_and_negative_inputs(self) -> None:
        with self.assertRaises(calc_mod.ProfitCalculatorError):
            calc_mod.evaluate_exact_route_profit(
                quote_token_symbol="USDC",
                quote_token_decimals=6,
                amount_in_raw=0,
                quoted_amount_out_raw=100,
                quotes_are_fee_inclusive=True,
                hop_fees_bps=[5, 5],
            )
        with self.assertRaises(calc_mod.ProfitCalculatorError):
            calc_mod.evaluate_exact_route_profit(
                quote_token_symbol="USDC",
                quote_token_decimals=6,
                amount_in_raw=1000,
                quoted_amount_out_raw=-1,
                quotes_are_fee_inclusive=True,
                hop_fees_bps=[5, 5],
            )

    def test_12_profit_calculator_handles_18_decimal_weth_and_6_decimal_usdc(self) -> None:
        res_weth = calc_mod.evaluate_exact_route_profit(
            quote_token_symbol="WETH",
            quote_token_decimals=18,
            amount_in_raw=10**18,
            quoted_amount_out_raw=1_002_000_000_000_000_000,
            quotes_are_fee_inclusive=True,
            hop_fees_bps=[5, 5],
            flash_loan_fee_bps=5,
            eth_to_quote_rate=Decimal("1.0"),
            slippage_buffer_bps=5,
        )
        self.assertEqual(res_weth.flash_loan_fee_raw, 5 * 10**14)
        self.assertTrue(res_weth.is_net_positive)

    def test_13_calldata_builder_validates_route_continuity_and_simulates_pinned_block(self) -> None:
        weth = self.valid_cfg["tokens"]["WETH"]["address"]
        usdc = self.valid_cfg["tokens"]["USDC"]["address"]
        router_v3 = "0xE592427A0AEce92De3Edee1F18E0157C05861564"
        pool_a = self.valid_cfg["pools"][0]["address"]
        pool_b = self.valid_cfg["pools"][1]["address"]

        steps = [
            sim_mod.SwapStepSpec(1, router_v3, pool_a, usdc, weth, 500),
            sim_mod.SwapStepSpec(1, router_v3, pool_b, weth, usdc, 3000),
        ]
        calldata = sim_mod.build_cyclic_arb_calldata(
            amount_in_raw=1_000_000_000,
            token_borrow=usdc,
            min_profit_raw=1_000_000,
            steps=steps,
            soft_abort_if_unprofitable=True,
        )
        self.assertTrue(calldata.startswith(sim_mod.SEL_EXEC_BALANCER_CYCLIC))

        bad_steps = [
            sim_mod.SwapStepSpec(1, router_v3, pool_a, usdc, weth, 500),
            sim_mod.SwapStepSpec(1, router_v3, pool_b, usdc, weth, 3000),
        ]
        with self.assertRaises(scanner_mod.ConfigValidationError):
            sim_mod.build_cyclic_arb_calldata(
                amount_in_raw=1_000_000_000,
                token_borrow=usdc,
                min_profit_raw=1_000_000,
                steps=bad_steps,
            )

        def sim_transport(return_bool: int):
            def _t(method: str, payload: dict):
                if method == "eth_chainId":
                    return {"jsonrpc": "2.0", "id": payload["id"], "result": "0xa4b1"}, 0.2
                if method == "eth_getCode":
                    return {"jsonrpc": "2.0", "id": payload["id"], "result": "0x60806040"}, 0.2
                if method == "eth_call":
                    return {"jsonrpc": "2.0", "id": payload["id"], "result": "0x" + scanner_mod.encode_uint256(return_bool)}, 0.5
                if method == "eth_estimateGas":
                    return {"jsonrpc": "2.0", "id": payload["id"], "result": hex(235_000 if return_bool else 46_200)}, 0.4
                raise RuntimeError(method)
            return _t

        rpc_ok = scanner_mod.JsonRpcClient("https://arb1.arbitrum.io/rpc", transport=sim_transport(1))
        sim_ok = sim_mod.simulate_route_at_pinned_block(
            rpc=rpc_ok,
            contract_address="0xBA12222222228d8Ba445958a75a0704d566BF2C8",
            sender_address="0xcA11bde05977b3631167028862bE2a173976CA11",
            calldata=calldata,
            amount_in_raw=1_000_000_000,
            min_profit_raw=1_000_000,
            pinned_block_number=265_000_000,
        )
        self.assertEqual(sim_ok.stage, "PRELIMINARY_CALL_CHECK")
        self.assertTrue(sim_ok.is_read_only)
        self.assertTrue(sim_ok.route_executed_without_soft_abort)
        self.assertEqual(sim_ok.estimated_gas_units, 235_000)

        rpc_abort = scanner_mod.JsonRpcClient("https://arb1.arbitrum.io/rpc", transport=sim_transport(0))
        sim_abort = sim_mod.simulate_route_at_pinned_block(
            rpc=rpc_abort,
            contract_address="0xBA12222222228d8Ba445958a75a0704d566BF2C8",
            sender_address="0xcA11bde05977b3631167028862bE2a173976CA11",
            calldata=calldata,
            amount_in_raw=1_000_000_000,
            min_profit_raw=1_000_000,
            pinned_block_number=265_000_000,
        )
        self.assertTrue(sim_abort.eth_call_succeeded)
        self.assertFalse(sim_abort.route_executed_without_soft_abort)
        self.assertIn("SoftAbortTriggered", sim_abort.decoded_revert_reason or "")

    def test_14_redacts_rpc_credentials_and_detects_stale_blocks(self) -> None:
        raw_url = "https://arb-mainnet.g.alchemy.com/v2/AbCdEf1234567890SecretKey"
        redacted = scanner_mod.redact_rpc_url(raw_url)
        self.assertNotIn("AbCdEf1234567890SecretKey", redacted)
        self.assertIn("***REDACTED***", redacted)

        stale_ts = int(time.time()) - 3600
        transport = stub_mod.create_deterministic_arbitrum_transport(block_timestamp=stale_ts)
        rpc = scanner_mod.JsonRpcClient(raw_url, transport=transport)
        scanner = scanner_mod.ArbitrumReadOnlyScanner(self.valid_cfg, rpc)
        with self.assertRaises(scanner_mod.ScanUnhealthyError):
            scanner.run_pinned_scan()

    def test_15_multicall3_batching_reduces_rpc_roundtrips_and_matches_single_calls(self) -> None:
        transport_batch = stub_mod.create_deterministic_arbitrum_transport(block_number=265_500_000)
        rpc_batch = scanner_mod.JsonRpcClient("https://arb1.arbitrum.io/rpc", transport=transport_batch)
        scanner_batch = scanner_mod.ArbitrumReadOnlyScanner(self.valid_cfg, rpc_batch)
        rep_batch = scanner_batch.run_pinned_scan(use_multicall3=True)

        transport_single = stub_mod.create_deterministic_arbitrum_transport(block_number=265_500_000)
        rpc_single = scanner_mod.JsonRpcClient("https://arb1.arbitrum.io/rpc", transport=transport_single)
        scanner_single = scanner_mod.ArbitrumReadOnlyScanner(self.valid_cfg, rpc_single)
        rep_single = scanner_single.run_pinned_scan(use_multicall3=False)

        self.assertTrue(rep_batch["telemetry"]["multicall3_batched"])
        self.assertFalse(rep_single["telemetry"]["multicall3_batched"])
        self.assertEqual(
            rep_single["telemetry"]["rpc_calls_count"] - rep_batch["telemetry"]["rpc_calls_count"],
            13,
        )
        self.assertEqual(
            rep_batch["route_evaluations"][0]["exact_quote_profit_breakdown"]["net_profit_raw"],
            rep_single["route_evaluations"][0]["exact_quote_profit_breakdown"]["net_profit_raw"],
        )

    def test_16_arbitrum_node_interface_gas_estimate_components_decoding(self) -> None:
        def ni_transport(method: str, payload: dict):
            if method == "eth_call":
                words = (
                    scanner_mod.encode_uint256(640_000)
                    + scanner_mod.encode_uint256(400_000)
                    + scanner_mod.encode_uint256(20_000_000)
                    + scanner_mod.encode_uint256(1_500_000_000)
                )
                return {"jsonrpc": "2.0", "id": payload["id"], "result": "0x" + words}, 0.4
            raise RuntimeError(method)

        rpc = scanner_mod.JsonRpcClient("https://arb1.arbitrum.io/rpc", transport=ni_transport)
        comp = sim_mod.estimate_arbitrum_gas_components_at_pinned_block(
            rpc=rpc,
            to_address="0xBA12222222228d8Ba445958a75a0704d566BF2C8",
            sender_address="0xcA11bde05977b3631167028862bE2a173976CA11",
            calldata="0x6a761202" + "00" * 64,
            pinned_block_number=265_000_111,
        )
        self.assertEqual(comp.total_gas_estimate_units, 640_000)
        self.assertEqual(comp.l1_gas_estimate_units, 400_000)
        self.assertEqual(comp.l2_execution_gas_units, 240_000)
        self.assertEqual(comp.l2_execution_cost_wei, 240_000 * 20_000_000)
        self.assertEqual(comp.l1_data_fee_wei, 400_000 * 20_000_000)
        self.assertEqual(comp.total_estimated_cost_wei, 640_000 * 20_000_000)

    def test_17_aave_v3_readonly_liquidation_scanner_50pct_and_100pct_close_factor(self) -> None:
        borrower_half = "0x1111111111111111111111111111111111111111"
        borrower_full = "0x2222222222222222222222222222222222222222"
        borrower_safe = "0x3333333333333333333333333333333333333333"

        def aave_transport(method: str, payload: dict):
            if method == "eth_chainId":
                return {"jsonrpc": "2.0", "id": payload["id"], "result": "0xa4b1"}, 0.2
            if method == "eth_call":
                data = payload["params"][0]["data"].lower()
                if borrower_half[2:] in data:
                    words = (
                        scanner_mod.encode_uint256(11_500 * 10**8)
                        + scanner_mod.encode_uint256(10_000 * 10**8)
                        + scanner_mod.encode_uint256(0)
                        + scanner_mod.encode_uint256(8250)
                        + scanner_mod.encode_uint256(8000)
                        + scanner_mod.encode_uint256(97 * 10**16)
                    )
                    return {"jsonrpc": "2.0", "id": payload["id"], "result": "0x" + words}, 0.4
                if borrower_full[2:] in data:
                    words = (
                        scanner_mod.encode_uint256(10_800 * 10**8)
                        + scanner_mod.encode_uint256(10_000 * 10**8)
                        + scanner_mod.encode_uint256(0)
                        + scanner_mod.encode_uint256(8250)
                        + scanner_mod.encode_uint256(8000)
                        + scanner_mod.encode_uint256(92 * 10**16)
                    )
                    return {"jsonrpc": "2.0", "id": payload["id"], "result": "0x" + words}, 0.4
                if borrower_safe[2:] in data:
                    words = (
                        scanner_mod.encode_uint256(15_000 * 10**8)
                        + scanner_mod.encode_uint256(10_000 * 10**8)
                        + scanner_mod.encode_uint256(1_000 * 10**8)
                        + scanner_mod.encode_uint256(8250)
                        + scanner_mod.encode_uint256(8000)
                        + scanner_mod.encode_uint256(125 * 10**16)
                    )
                    return {"jsonrpc": "2.0", "id": payload["id"], "result": "0x" + words}, 0.4
            raise RuntimeError(method)

        rpc = scanner_mod.JsonRpcClient("https://arb1.arbitrum.io/rpc", transport=aave_transport)
        candidates = liq_mod.scan_aave_v3_borrowers_readonly(
            rpc=rpc,
            borrowers=[
                {"address": borrower_half, "debt_token_symbol": "USDC", "debt_token_decimals": 6, "liquidation_bonus_bps": 500},
                {"address": borrower_full, "debt_token_symbol": "USDC", "debt_token_decimals": 6, "liquidation_bonus_bps": 500},
                {"address": borrower_safe, "debt_token_symbol": "USDC", "debt_token_decimals": 6, "liquidation_bonus_bps": 500},
            ],
            pinned_block_number=265_000_200,
        )
        self.assertEqual(len(candidates), 3)
        self.assertEqual(candidates[0].borrower.lower(), borrower_full.lower())
        self.assertEqual(candidates[0].close_factor_bps, 10_000)
        self.assertTrue(candidates[0].is_net_positive)
        self.assertGreater(candidates[0].net_profit_raw, 450 * 10**6)

        self.assertEqual(candidates[1].borrower.lower(), borrower_half.lower())
        self.assertEqual(candidates[1].close_factor_bps, 5_000)
        self.assertTrue(candidates[1].is_net_positive)
        self.assertGreater(candidates[1].net_profit_raw, 220 * 10**6)

        self.assertEqual(candidates[2].borrower.lower(), borrower_safe.lower())
        self.assertFalse(candidates[2].is_net_positive)

    def test_18_permissionless_keeper_harvest_bounty_scanner(self) -> None:
        strat_profitable = "0x4444444444444444444444444444444444444444"
        strat_paused = "0x5555555555555555555555555555555555555555"

        def keeper_transport(method: str, payload: dict):
            if method == "eth_call":
                to_addr = payload["params"][0]["to"].lower()
                sel = payload["params"][0]["data"][:10].lower()
                if sel == liq_mod.SEL_STRATEGY_PAUSED:
                    paused_val = 1 if to_addr == strat_paused else 0
                    return {"jsonrpc": "2.0", "id": payload["id"], "result": "0x" + scanner_mod.encode_uint256(paused_val)}, 0.3
                if sel == liq_mod.SEL_STRATEGY_CALL_REWARD:
                    return {"jsonrpc": "2.0", "id": payload["id"], "result": "0x" + scanner_mod.encode_uint256(2 * 10**15)}, 0.3
            raise RuntimeError(method)

        rpc = scanner_mod.JsonRpcClient("https://arb1.arbitrum.io/rpc", transport=keeper_transport)
        res = liq_mod.scan_permissionless_keeper_bounties_readonly(
            rpc=rpc,
            strategies=[
                {"name": "Beefy_Arb_WETH_USDC", "address": strat_profitable},
                {"name": "Beefy_Paused_Vault", "address": strat_paused},
            ],
            pinned_block_number=265_000_300,
        )
        self.assertEqual(len(res), 2)
        self.assertTrue(res[0].is_net_positive)
        self.assertEqual(res[0].classification, "ESTIMATED_HARVESTABLE_UNVERIFIED")
        self.assertFalse(res[1].is_net_positive)
        self.assertTrue(res[1].is_paused)

    def test_19_paper_trading_logger_measures_next_block_persistence_and_scores_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            jsonl_path = Path(tmpdir) / "paper_log.jsonl"

            def persistent_rpc_factory(cycle_idx: int) -> scanner_mod.JsonRpcClient:
                t = stub_mod.create_deterministic_arbitrum_transport(
                    block_number=265_000_000 + cycle_idx
                )
                return scanner_mod.JsonRpcClient("https://arb1.arbitrum.io/rpc", transport=t)

            scorecard_ready = paper_mod.run_paper_trading_campaign(
                config=self.valid_cfg,
                rpc_factory=persistent_rpc_factory,
                cycles=4,
                output_jsonl_path=jsonl_path,
                min_persisted_hits_for_ready=2,
                min_persistence_rate_for_ready=0.50,
            )
            self.assertEqual(scorecard_ready.verdict, "READY_FOR_FORK_SIM")
            self.assertEqual(scorecard_ready.next_block_persisted_hits, 3)
            self.assertEqual(scorecard_ready.next_block_persistence_rate, 1.0)
            self.assertEqual(len(jsonl_path.read_text(encoding="utf-8").strip().splitlines()), 4)

            def fleeting_rpc_factory(cycle_idx: int) -> scanner_mod.JsonRpcClient:
                if cycle_idx % 2 == 0:
                    prices = None
                else:
                    prices = {
                        "0xc6962004f452be9203591991d15f6b388e09e8d0": 2650.00,
                        "0xc473e2aee3441bf9240be85eb122abb059a3b57c": 2650.00,
                        "0x65305730262939829098e6a32a695e3803d7d0d2": 2650.00,
                    }
                t = stub_mod.create_deterministic_arbitrum_transport(
                    block_number=265_001_000 + cycle_idx,
                    pool_prices_usd=prices,
                )
                return scanner_mod.JsonRpcClient("https://arb1.arbitrum.io/rpc", transport=t)

            scorecard_fleeting = paper_mod.run_paper_trading_campaign(
                config=self.valid_cfg,
                rpc_factory=fleeting_rpc_factory,
                cycles=4,
                min_persisted_hits_for_ready=1,
            )
            self.assertEqual(scorecard_fleeting.verdict, "KEEP_COLLECTING_DATA")
            self.assertEqual(scorecard_fleeting.next_block_persisted_hits, 0)

    # =========================================================================
    #       v3.5 CROSS-CHAIN SPECULATIVE OEV & UNISWAPX INTENT TESTS
    # =========================================================================

    def test_20_cross_chain_optimism_leads_arbitrum_chainlink_oev_signal(self) -> None:
        """
        Verifies Patch 5A (`arXiv:2606.03434v1`): Reads Optimism (chain_id=10) and Arbitrum (chain_id=42161)
        Chainlink ETH/USD `latestRoundData()` and detects a 6-second leading price drop on Optimism.
        """
        now_ts = 1_750_000_000

        def make_chainlink_transport(cid: int, price_8dec: int, updated_at: int):
            def _t(method: str, payload: dict):
                if method == "eth_chainId":
                    return {"jsonrpc": "2.0", "id": payload["id"], "result": hex(cid)}, 0.3
                if method == "eth_call":
                    words = (
                        scanner_mod.encode_uint256(18446744073709562301)
                        + scanner_mod.encode_uint256(price_8dec)
                        + scanner_mod.encode_uint256(updated_at)
                        + scanner_mod.encode_uint256(updated_at)
                        + scanner_mod.encode_uint256(18446744073709562301)
                    )
                    return {"jsonrpc": "2.0", "id": payload["id"], "result": "0x" + words}, 0.4
                raise RuntimeError(method)
            return _t

        # Optimism just updated at `now_ts` to $2,610.25 (-150 bps drop), while Arbitrum is 6s behind at $2,650.00
        rpc_op = scanner_mod.JsonRpcClient(
            "https://mainnet.optimism.io",
            transport=make_chainlink_transport(10, 261_025_000_000, now_ts),
        )
        rpc_arb = scanner_mod.JsonRpcClient(
            "https://arb1.arbitrum.io/rpc",
            transport=make_chainlink_transport(42161, 265_000_000_000, now_ts - 6),
        )

        op_read = oev_mod.read_chainlink_latest_round(
            rpc=rpc_op,
            expected_chain_id=10,
            chain_name="Optimism",
            feed_address=oev_mod.CHAINLINK_ETH_USD_OPTIMISM,
            pinned_block_number=135_000_000,
        )
        arb_read = oev_mod.read_chainlink_latest_round(
            rpc=rpc_arb,
            expected_chain_id=42161,
            chain_name="Arbitrum One",
            feed_address=oev_mod.CHAINLINK_ETH_USD_ARBITRUM,
            pinned_block_number=265_000_500,
        )

        signal = oev_mod.evaluate_cross_chain_lead_lag_signal(
            optimism_reading=op_read,
            arbitrum_reading=arb_read,
            min_trigger_drop_bps=15.0,
        )
        self.assertTrue(signal.optimism_leads_in_time)
        self.assertEqual(signal.lead_time_seconds, 6)
        self.assertAlmostEqual(signal.price_delta_bps, -150.0, places=2)
        self.assertTrue(signal.is_speculative_liquidation_trigger)
        self.assertEqual(signal.classification, "OPTIMISM_LEADING_DROP_TRIGGER")

    def test_21_speculative_oev_projected_health_factor_equation_2(self) -> None:
        """
        Verifies Patch 5B (`arXiv:2606.03434v1` Equation 2): Projects an Arbitrum borrower whose current
        Arbitrum HF is 1.008 (`1.008e18` — healthy on stale Arbitrum price) into `H^{(OP)}(t) = 0.99288`
        using the leading Optimism price, flagging `SPECULATIVE_OEV_PRE_STAGE_READY`.
        """
        now_ts = 1_750_000_000
        op_read = oev_mod.ChainlinkFeedReading(
            chain_id=10,
            chain_name="Optimism",
            feed_address=oev_mod.CHAINLINK_ETH_USD_OPTIMISM,
            pair_symbol="ETH/USD",
            decimals=8,
            round_id=1002,
            answer_raw=261_025_000_000,
            price_human="2610.25000000",
            updated_at=now_ts,
            pinned_block_number=135_000_000,
        )
        arb_read = oev_mod.ChainlinkFeedReading(
            chain_id=42161,
            chain_name="Arbitrum One",
            feed_address=oev_mod.CHAINLINK_ETH_USD_ARBITRUM,
            pair_symbol="ETH/USD",
            decimals=8,
            round_id=1001,
            answer_raw=265_000_000_000,
            price_human="2650.00000000",
            updated_at=now_ts - 6,
            pinned_block_number=265_000_500,
        )
        signal = oev_mod.evaluate_cross_chain_lead_lag_signal(
            optimism_reading=op_read,
            arbitrum_reading=arb_read,
            min_trigger_drop_bps=15.0,
        )

        projections = oev_mod.project_speculative_arbitrum_liquidations(
            signal=signal,
            watched_borrowers=[
                {
                    "address": "0x7777777777777777777777777777777777777777",
                    "current_arbitrum_hf_wad": int(1.008 * 10**18),
                    "total_collateral_base_8dec": 12_200 * 10**8,
                    "total_debt_base_8dec": 10_000 * 10**8,
                    "debt_token_symbol": "USDC",
                    "debt_token_decimals": 6,
                    "liquidation_bonus_bps": 500,
                }
            ],
            arbitrum_pinned_block=265_000_500,
        )
        self.assertEqual(len(projections), 1)
        proj = projections[0]
        self.assertEqual(proj.classification, "SPECULATIVE_OEV_PRE_STAGE_READY")
        self.assertGreaterEqual(proj.current_arbitrum_hf_wad, 10**18)
        self.assertLess(proj.projected_optimism_hf_wad, 10**18)
        self.assertTrue(proj.liquidation_evaluation.is_net_positive)
        self.assertGreater(proj.liquidation_evaluation.net_profit_raw, 220 * 10**6)

    def test_22_uniswapx_dutch_auction_decay_and_zero_capital_fill_evaluator(self) -> None:
        """
        Verifies Patch 6A (`Uniswap/UniswapX` & `arXiv:2403.02525v1`): Evaluates a decaying Dutch auction
        order across its decay window and pinpoints the exact timestamp where filling becomes profitable.
        """
        order = unix_mod.DutchOrderSpec(
            order_hash="0x" + "12" * 32,
            reactor_address=unix_mod.UNISWAPX_V2_DUTCH_REACTOR_ARBITRUM,
            swapper="0x8888888888888888888888888888888888888888",
            input_token_symbol="WETH",
            input_token_address=self.valid_cfg["tokens"]["WETH"]["address"],
            input_token_decimals=18,
            output_token_symbol="USDC",
            output_token_address=self.valid_cfg["tokens"]["USDC"]["address"],
            output_token_decimals=6,
            input_amount_raw=2 * 10**18,              # 2 WETH input
            start_output_amount_raw=5_320 * 10**6,    # Starts above market ($5,320 USDC)
            end_output_amount_raw=5_270 * 10**6,      # Decays to $5,270 USDC over 20 seconds
            decay_start_time=1_000,
            decay_end_time=1_020,
            deadline=1_025,
        )

        # On-chain DEX quote for 2 WETH -> USDC is $5,300.00 (5,300,000,000 raw USDC)
        dex_quote_raw = 5_300 * 10**6

        # At t=1002 (only 10% decayed -> required output = $5,315), filling is underwater
        eval_early = unix_mod.evaluate_uniswapx_dutch_order_fill(
            order=order,
            current_timestamp=1_002,
            onchain_dex_quoted_output_raw=dex_quote_raw,
        )
        self.assertEqual(eval_early.classification, "UNDERWATER_WAIT_FOR_DECAY")
        self.assertFalse(eval_early.is_net_positive_now)

        # At t=1015 (75% decayed -> required output = $5,282.50), filling yields > +$15 net profit!
        eval_profitable = unix_mod.evaluate_uniswapx_dutch_order_fill(
            order=order,
            current_timestamp=1_015,
            onchain_dex_quoted_output_raw=dex_quote_raw,
        )
        self.assertEqual(eval_profitable.classification, "EXECUTABLE_UNISWAPX_INTENT_UNVERIFIED")
        self.assertTrue(eval_profitable.is_net_positive_now)
        self.assertGreater(eval_profitable.net_filler_profit_raw, 15 * 10**6)
        self.assertLessEqual(eval_profitable.optimal_fill_timestamp, 1_015)

    def test_23_uniswap_v4_singleton_unlock_gas_savings_and_solidity_callbacks(self) -> None:
        """
        Verifies Patch 6B: Uniswap V4 `unlockCallback` singleton gas comparison and static presence of
        `reactorCallback` and `unlockCallback` in `contracts/FlashLoanArbitrage.sol`.
        """
        gas_cmp = unix_mod.compare_v3_vs_v4_singleton_flash_gas(hops_count=3)
        self.assertGreater(gas_cmp["l2_gas_units_saved"], 100_000)
        self.assertGreater(gas_cmp["l2_gas_reduction_pct"], 35.0)

        sol_text = (REPO_ROOT / "contracts/FlashLoanArbitrage.sol").read_text(encoding="utf-8")
        for expected_sig in [
            "IReactorCallback",
            "function reactorCallback(",
            "executeUniswapXIntentFill(",
            "IUnlockCallback",
            "function unlockCallback(",
            "executeUniswapV4SingletonArb(",
        ]:
            self.assertIn(expected_sig, sol_text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
