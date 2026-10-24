#!/usr/bin/env python3
"""
bot/cross_chain_oev_monitor.py — Read-Only Cross-Chain Speculative OEV Lead-Lag Scanner (v3.5)
================================================================================================
Implements the empirical findings from:
1. "Signals and Spoils: Speculative Oracle Extractable Value in the Era of Cross-Chain Price Feeds"
   (arXiv:2606.03434v1, June 2026 — https://arxiv.org/html/2606.03434v1):
   - Across 63 Chainlink price feeds, 12,009 oracle updates, and 2,986 Aave V3 liquidations on
     Arbitrum, Base, and Optimism, Chainlink's Optimism feed publishes `NewTransmission` events
     up to ~6 seconds BEFORE Arbitrum and Base, leading more than twice as often as it lags.
   - By observing the leading price `P^{(OP)}_c(t)` on Optimism while Arbitrum still reflects
     stale price `P^{(ARB)}_c(t)`, a searcher computes the speculative projected Health Factor:
         H^{(OP)}(t) = H^{(ARB)}(t) * (P^{(OP)}_c(t) / P^{(ARB)}_c(t))   [Equation (2) for single-collateral]
     giving a ~6,000ms advance window to pre-stage Aave V3 / Morpho Blue flash liquidations on Arbitrum,
     completely neutralizing a ~200ms geographic network ping.
2. Compound DAO & Chainlink SVR Rollout Update (July 2025 — https://www.comp.xyz/t/oev-rfp-process-update-july-2025/6945):
   - Chainlink SVR (Smart Value Recapture) auctions are active only on Ethereum Mainnet (L1) and are
     NOT active on Arbitrum One, Base, or Optimism, meaning L2 liquidators retain 100% of the 5%-10%
     protocol liquidation bonus.
3. RedStone / API3 Pull-Oracle Atomic OEV Bundling (Delphi Digital — https://members.delphidigital.io/reports/api3-the-state-of-oev):
   - Evaluates whether a signed off-chain oracle payload (`updatePrice` + `liquidate` in one atomic
     transaction) yields positive net profit after L1+L2 calldata gas.
"""

from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Dict, List

from liquidation_and_keeper_scanner import (
    AAVE_V3_POOL_ARBITRUM,
    HF_LIQUIDATION_THRESHOLD_WAD,
    AaveBorrowerLiquidationCandidate,
    evaluate_aave_v3_liquidation_candidate,
)
from opportunity_scanner import (
    ARBITRUM_ONE_CHAIN_ID,
    ChainIdMismatchError,
    ConfigValidationError,
    JsonRpcClient,
    ScannerError,
    decode_int24_word,
    decode_uint_word,
     verify_address_format,
)

OPTIMISM_CHAIN_ID = 10
SEL_CHAINLINK_LATEST_ROUND_DATA = "0xfeaf968c"  # latestRoundData() -> (uint80,int256,uint256,uint256,uint80)
SEL_CHAINLINK_DECIMALS = "0x313ce567"           # decimals()

# Verified Mainnet Chainlink ETH/USD Aggregator Proxy Addresses
CHAINLINK_ETH_USD_OPTIMISM = "0x13e3Ee699D1909E989722E753853AE30b17e08c5"
CHAINLINK_ETH_USD_ARBITRUM = "0x639Fe6ab55C921f74e7fac1ee960C0B6293ba612"


@dataclass(frozen=True)
class ChainlinkFeedReading:
    chain_id: int
    chain_name: str
    feed_address: str
    pair_symbol: str
    decimals: int
    round_id: int
    answer_raw: int
    price_human: str
    updated_at: int
    pinned_block_number: int

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass(frozen=True)
class CrossChainOracleLeadSignal:
    classification: str               # "OPTIMISM_LEADING_DROP_TRIGGER", "OPTIMISM_LEADING_RISE", or "NO_LEAD_ADVANTAGE"
    pair_symbol: str
    optimism_reading: ChainlinkFeedReading
    arbitrum_reading: ChainlinkFeedReading
    optimism_leads_in_time: bool
    lead_time_seconds: int
    price_delta_bps: float
    min_trigger_drop_bps: float
    is_speculative_liquidation_trigger: bool

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass(frozen=True)
class SpeculativeOEVLiquidationProjection:
    classification: str               # "SPECULATIVE_OEV_PRE_STAGE_READY" or "NOT_TRIGGERED_OR_UNPROFITABLE"
    borrower: str
    current_arbitrum_hf_wad: int
    projected_optimism_hf_wad: int
    current_arbitrum_hf_float: float
    projected_optimism_hf_float: float
    lead_time_seconds: int
    price_delta_bps: float
    liquidation_evaluation: AaveBorrowerLiquidationCandidate

    def to_dict(self) -> Dict:
        return asdict(self)


def read_chainlink_latest_round(
    *,
    rpc: JsonRpcClient,
    expected_chain_id: int,
    chain_name: str,
    feed_address: str,
    pair_symbol: str = "ETH/USD",
    decimals: int = 8,
    pinned_block_number: int,
) -> ChainlinkFeedReading:
    """
    Reads `latestRoundData()` (`0xfeaf968c`) from a Chainlink Aggregator at `pinned_block_number`.
    """
    if pinned_block_number <= 0:
        raise ConfigValidationError("pinned_block_number must be > 0")
    f_addr = verify_address_format(feed_address, f"chainlink_feed[{chain_name}]")

    cid_hex = rpc.call("eth_chainId", [])
    cid = int(cid_hex, 16) if isinstance(cid_hex, str) else int(cid_hex)
    if cid != expected_chain_id:
        raise ChainIdMismatchError(
            f"Chainlink reader expected chain_id={expected_chain_id} ({chain_name}), got {cid}"
        )

    block_tag = hex(pinned_block_number)
    ret_hex = rpc.eth_call(f_addr, SEL_CHAINLINK_LATEST_ROUND_DATA, block_tag)
    if len(ret_hex) < 2 + 64 * 5:
        raise ScannerError(f"Malformed latestRoundData() response from {f_addr}: {ret_hex!r}")

    round_id = decode_uint_word(ret_hex, 0)
    answer_raw = decode_int24_word(ret_hex, 1)  # signed 256-bit int decoder
    updated_at = decode_uint_word(ret_hex, 3)

    if answer_raw <= 0:
        raise ScannerError(f"Non-positive Chainlink oracle answer ({answer_raw}) on {chain_name}")

    price_dec = Decimal(answer_raw) / Decimal(10 ** decimals)
    return ChainlinkFeedReading(
        chain_id=cid,
        chain_name=chain_name,
        feed_address=f_addr,
        pair_symbol=pair_symbol,
        decimals=decimals,
        round_id=round_id,
        answer_raw=answer_raw,
        price_human=f"{price_dec:.{min(decimals, 8)}f}",
        updated_at=updated_at,
        pinned_block_number=pinned_block_number,
    )


def evaluate_cross_chain_lead_lag_signal(
    *,
    optimism_reading: ChainlinkFeedReading,
    arbitrum_reading: ChainlinkFeedReading,
    min_trigger_drop_bps: float = 15.0,
) -> CrossChainOracleLeadSignal:
    """
    Compares a Chainlink feed reading on Optimism (`chain_id=10`) against Arbitrum (`chain_id=42161`)
    to detect when Optimism has published a fresher `updatedAt` timestamp with a significant price drop
    that has not yet landed on Arbitrum (`arXiv:2606.03434v1` Section 3 & Section 4).
    """
    if optimism_reading.decimals != arbitrum_reading.decimals:
        raise ConfigValidationError("Feed decimals must match between Optimism and Arbitrum readings")
    if arbitrum_reading.answer_raw <= 0 or optimism_reading.answer_raw <= 0:
        raise ConfigValidationError("Oracle answers must be strictly positive")

    lead_seconds = optimism_reading.updated_at - arbitrum_reading.updated_at
    op_leads = lead_seconds > 0

    delta_bps = float(
        (Decimal(optimism_reading.answer_raw - arbitrum_reading.answer_raw) / Decimal(arbitrum_reading.answer_raw))
        * Decimal(10_000)
    )
    is_trigger = op_leads and (delta_bps <= -abs(min_trigger_drop_bps))

    if is_trigger:
        classification = "OPTIMISM_LEADING_DROP_TRIGGER"
    elif op_leads and delta_bps > 0:
        classification = "OPTIMISM_LEADING_RISE"
    else:
        classification = "NO_LEAD_ADVANTAGE"

    return CrossChainOracleLeadSignal(
        classification=classification,
        pair_symbol=optimism_reading.pair_symbol,
        optimism_reading=optimism_reading,
        arbitrum_reading=arbitrum_reading,
        optimism_leads_in_time=op_leads,
        lead_time_seconds=lead_seconds,
        price_delta_bps=round(delta_bps, 4),
        min_trigger_drop_bps=min_trigger_drop_bps,
        is_speculative_liquidation_trigger=is_trigger,
    )


def project_speculative_arbitrum_liquidations(
    *,
    signal: CrossChainOracleLeadSignal,
    watched_borrowers: List[Dict],
    arbitrum_pinned_block: int,
    aave_pool: str = AAVE_V3_POOL_ARBITRUM,
) -> List[SpeculativeOEVLiquidationProjection]:
    """
    Applies Equation (2) of `arXiv:2606.03434v1` (`Signals and Spoils`):
        H^{(OP)}(t) = H^{(ARB)}(t) * (P^{(OP)}_c(t) / P^{(ARB)}_c(t))
    For borrowers who are currently healthy on Arbitrum (`H^{(ARB)}(t) >= 1e18`) ONLY because Arbitrum's
    Chainlink oracle lags Optimism by up to ~6 seconds, this projects their imminent Arbitrum Health Factor
    (`H^{(OP)}(t) < 1e18`) and computes exact net flash-liquidation profit in raw integer units.
    """
    op_ans = signal.optimism_reading.answer_raw
    arb_ans = signal.arbitrum_reading.answer_raw
    eth_to_debt_rate = Decimal(op_ans) / Decimal(10 ** signal.optimism_reading.decimals)

    projections: List[SpeculativeOEVLiquidationProjection] = []
    for b_cfg in watched_borrowers:
        b_addr = verify_address_format(b_cfg["address"], "watched_borrower.address")
        curr_hf_wad = int(b_cfg["current_arbitrum_hf_wad"])
        collat_base = int(b_cfg.get("total_collateral_base_8dec", 12_000 * 10**8))
        debt_base = int(b_cfg.get("total_debt_base_8dec", 10_000 * 10**8))
        liq_thresh_bps = int(b_cfg.get("current_liquidation_threshold_bps", 8250))
        debt_dec = int(b_cfg.get("debt_token_decimals", 6))
        debt_raw = int(b_cfg.get("debt_amount_raw", (debt_base * (10 ** debt_dec)) // (10 ** 8)))

        if signal.optimism_leads_in_time and op_ans < arb_ans:
            projected_hf_wad = (curr_hf_wad * op_ans) // arb_ans
            projected_collat_base = (collat_base * op_ans) // arb_ans
        else:
            projected_hf_wad = curr_hf_wad
            projected_collat_base = collat_base

        liq_eval = evaluate_aave_v3_liquidation_candidate(
            pinned_block_number=arbitrum_pinned_block,
            borrower=b_addr,
            total_collateral_base_8dec=projected_collat_base,
            total_debt_base_8dec=debt_base,
            current_liquidation_threshold_bps=liq_thresh_bps,
            health_factor_wad=projected_hf_wad,
            debt_token_symbol=b_cfg.get("debt_token_symbol", "USDC"),
            debt_token_decimals=debt_dec,
            debt_amount_raw=debt_raw,
            liquidation_bonus_bps=int(b_cfg.get("liquidation_bonus_bps", 500)),
            flash_loan_fee_bps=int(b_cfg.get("flash_loan_fee_bps", 0)),
            swap_slippage_bps=int(b_cfg.get("swap_slippage_bps", 20)),
            eth_to_debt_rate=eth_to_debt_rate,
            aave_pool=aave_pool,
        )

        is_pre_stage_ready = (
            signal.is_speculative_liquidation_trigger
            and curr_hf_wad >= HF_LIQUIDATION_THRESHOLD_WAD
            and projected_hf_wad < HF_LIQUIDATION_THRESHOLD_WAD
            and liq_eval.is_net_positive
        )

        projections.append(
            SpeculativeOEVLiquidationProjection(
                classification=(
                    "SPECULATIVE_OEV_PRE_STAGE_READY"
                    if is_pre_stage_ready
                    else "NOT_TRIGGERED_OR_UNPROFITABLE"
                ),
                borrower=b_addr,
                current_arbitrum_hf_wad=curr_hf_wad,
                projected_optimism_hf_wad=projected_hf_wad,
                current_arbitrum_hf_float=round(float(Decimal(curr_hf_wad) / Decimal(10**18)), 6),
                projected_optimism_hf_float=round(float(Decimal(projected_hf_wad) / Decimal(10**18)), 6),
                lead_time_seconds=signal.lead_time_seconds,
                price_delta_bps=signal.price_delta_bps,
                liquidation_evaluation=liq_eval,
            )
        )

    projections.sort(key=lambda p: p.liquidation_evaluation.net_profit_raw, reverse=True)
    return projections
