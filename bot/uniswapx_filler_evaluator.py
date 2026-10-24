#!/usr/bin/env python3
"""
bot/uniswapx_filler_evaluator.py — Read-Only UniswapX Dutch-Auction Intent & V4 Singleton Evaluator (v3.5)
===========================================================================================================
Implements the zero-capital intent-filling and singleton flash-accounting models from:
1. `Uniswap/UniswapX` (`V2DutchOrderReactor` / `IReactorCallback.reactorCallback` — https://github.com/Uniswap/UniswapX)
   and Chitra et al., "An Analysis of Intent-Based Markets" (`arXiv:2403.02525v1` — https://arxiv.org/html/2403.02525v1):
   - In UniswapX Dutch auctions, a user signs an off-chain order specifying `inputAmount`,
     `decayStartTime`, `decayEndTime`, `startOutputAmount`, and `endOutputAmount` (minimum user receive).
   - As block timestamp `t` progresses from `decayStartTime` to `decayEndTime`, the required output
     decays linearly.
   - When a filler calls `executeWithCallback(SignedOrder, callbackData)`, the Reactor transfers the
     user's `inputToken` to the filler contract FIRST and invokes `reactorCallback(...)`. The filler
     swaps `inputToken -> outputToken` on Arbitrum DEXs (Uniswap V3, Camelot Algebra, Fluid) using the
     user's own input tokens (ZERO flash-loan fee, ZERO upfront capital!), satisfies `requiredOutput`,
     and keeps the surplus.
2. Uniswap V4 `PoolManager` Singleton Flash Accounting (`unlockCallback` + EIP-1153 Transient Storage —
   https://updraft.cyfrin.io/courses/uniswap-v4/pool-manager/swap-contract-calls):
   - Evaluates the gas savings of executing multi-hop swaps inside a single `PoolManager.unlock()` frame
     using transient storage (`TSTORE` / `TLOAD`, ~100 gas vs 20,000 gas for `SSTORE`) where intermediate
     ERC-20 transfers are netted out until final settlement.
"""

from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Dict, Tuple

from opportunity_scanner import ConfigValidationError, verify_address_format
from profit_calculator import convert_wei_to_quote_raw

UNISWAPX_V2_DUTCH_REACTOR_ARBITRUM = "0x1bd1aAdc9E230626C44a139d7E70d842749351eb"


@dataclass(frozen=True)
class DutchOrderSpec:
    order_hash: str
    reactor_address: str
    swapper: str
    input_token_symbol: str
    input_token_address: str
    input_token_decimals: int
    output_token_symbol: str
    output_token_address: str
    output_token_decimals: int
    input_amount_raw: int
    start_output_amount_raw: int
    end_output_amount_raw: int
    decay_start_time: int
    decay_end_time: int
    deadline: int


@dataclass(frozen=True)
class UniswapXFillEvaluation:
    classification: str               # "EXECUTABLE_UNISWAPX_INTENT_UNVERIFIED", "EXPIRED_ORDER", or "UNDERWATER_WAIT_FOR_DECAY"
    order_hash: str
    evaluation_timestamp: int
    is_expired: bool
    decay_progress_bps: int           # 0 bps (at start) to 10000 bps (fully decayed)
    required_output_raw: int
    onchain_dex_quoted_output_raw: int
    gross_filler_surplus_raw: int
    slippage_safety_buffer_raw: int
    total_gas_wei: int
    gas_cost_output_raw: int
    net_filler_profit_raw: int
    net_filler_profit_human: str
    optimal_fill_timestamp: int       # Earliest timestamp where DEX quote >= required_output + gas + buffer
    is_net_positive_now: bool

    def to_dict(self) -> Dict:
        return asdict(self)


def compute_dutch_decayed_output_raw(order: DutchOrderSpec, current_timestamp: int) -> Tuple[int, int]:
    """
    Computes the exact linearly decayed `required_output_raw` and `decay_progress_bps` at `current_timestamp`
    matching `DutchDecayLib.decay()` in `Uniswap/UniswapX`.
    """
    if order.start_output_amount_raw < order.end_output_amount_raw:
        raise ConfigValidationError("DutchOrder start_output_amount_raw must be >= end_output_amount_raw")
    if order.decay_end_time < order.decay_start_time:
        raise ConfigValidationError("DutchOrder decay_end_time must be >= decay_start_time")

    if current_timestamp <= order.decay_start_time or order.decay_end_time == order.decay_start_time:
        return order.start_output_amount_raw, 0
    if current_timestamp >= order.decay_end_time:
        return order.end_output_amount_raw, 10_000

    elapsed = current_timestamp - order.decay_start_time
    duration = order.decay_end_time - order.decay_start_time
    total_decay = order.start_output_amount_raw - order.end_output_amount_raw

    # Ceiling rounding in favor of the swapper (matching UniswapX Solidity math)
    decayed_delta = (total_decay * elapsed) // duration
    required_out = order.start_output_amount_raw - decayed_delta
    progress_bps = (elapsed * 10_000) // duration
    return required_out, progress_bps


def evaluate_uniswapx_dutch_order_fill(
    *,
    order: DutchOrderSpec,
    current_timestamp: int,
    onchain_dex_quoted_output_raw: int,
    l2_gas_units: int = 195_000,
    l2_gas_price_wei: int = 20_000_000,
    l1_data_fee_wei: int = 12_000_000_000_000,
    eth_to_output_rate: Decimal = Decimal("2650.00"),
    slippage_buffer_bps: int = 3,
) -> UniswapXFillEvaluation:
    """
    Evaluates whether a signed UniswapX Dutch-auction order can be profitably filled right now via
    `reactorCallback` (zero upfront capital, zero flash-loan fee) and calculates the exact timestamp
    at which the decay curve crosses into net profitability.
    """
    verify_address_format(order.reactor_address, "order.reactor_address")
    verify_address_format(order.swapper, "order.swapper")
    verify_address_format(order.input_token_address, "order.input_token_address")
    verify_address_format(order.output_token_address, "order.output_token_address")

    if order.input_amount_raw <= 0 or onchain_dex_quoted_output_raw < 0:
        raise ConfigValidationError("Order input_amount_raw must be > 0 and quoted output >= 0")

    is_expired = current_timestamp > order.deadline
    required_out_raw, progress_bps = compute_dutch_decayed_output_raw(order, current_timestamp)

    total_gas_wei = (l2_gas_units * l2_gas_price_wei) + l1_data_fee_wei
    gas_cost_out_raw = convert_wei_to_quote_raw(
        wei_amount=total_gas_wei,
        quote_token_decimals=order.output_token_decimals,
        eth_to_quote_rate=eth_to_output_rate,
    )
    buffer_raw = (onchain_dex_quoted_output_raw * slippage_buffer_bps) // 10_000

    gross_surplus_raw = onchain_dex_quoted_output_raw - required_out_raw
    net_profit_raw = gross_surplus_raw - gas_cost_out_raw - buffer_raw
    is_pos_now = (not is_expired) and (net_profit_raw > 0)

    # Calculate earliest timestamp where required_out_raw <= max_payable_out
    max_payable_out = onchain_dex_quoted_output_raw - gas_cost_out_raw - buffer_raw
    if max_payable_out >= order.start_output_amount_raw:
        opt_ts = order.decay_start_time
    elif max_payable_out < order.end_output_amount_raw:
        opt_ts = order.deadline + 1  # Never profitable even at full decay
    else:
        needed_decay = order.start_output_amount_raw - max_payable_out
        total_decay = max(1, order.start_output_amount_raw - order.end_output_amount_raw)
        duration = order.decay_end_time - order.decay_start_time
        wait_sec = ((needed_decay * duration) + total_decay - 1) // total_decay
        opt_ts = order.decay_start_time + wait_sec

    if is_expired:
        classification = "EXPIRED_ORDER"
    elif is_pos_now:
        classification = "EXECUTABLE_UNISWAPX_INTENT_UNVERIFIED"
    else:
        classification = "UNDERWATER_WAIT_FOR_DECAY"

    scale = Decimal(10 ** order.output_token_decimals)
    net_human = f"{(Decimal(net_profit_raw) / scale):.{min(order.output_token_decimals, 8)}f}"

    return UniswapXFillEvaluation(
        classification=classification,
        order_hash=order.order_hash,
        evaluation_timestamp=current_timestamp,
        is_expired=is_expired,
        decay_progress_bps=progress_bps,
        required_output_raw=required_out_raw,
        onchain_dex_quoted_output_raw=onchain_dex_quoted_output_raw,
        gross_filler_surplus_raw=gross_surplus_raw,
        slippage_safety_buffer_raw=buffer_raw,
        total_gas_wei=total_gas_wei,
        gas_cost_output_raw=gas_cost_out_raw,
        net_filler_profit_raw=net_profit_raw,
        net_filler_profit_human=net_human,
        optimal_fill_timestamp=opt_ts,
        is_net_positive_now=is_pos_now,
    )


def compare_v3_vs_v4_singleton_flash_gas(
    *,
    hops_count: int = 3,
    l2_gas_price_wei: int = 20_000_000,
    l1_data_fee_wei: int = 12_000_000_000_000,
) -> Dict:
    """
    Quantifies the execution gas savings of Uniswap V4 `PoolManager.unlockCallback` (EIP-1153 transient
    storage flash accounting) vs classic V3 multi-pool ERC-20 storage transfers across `hops_count` hops.
    """
    if hops_count < 2:
        raise ConfigValidationError("hops_count must be >= 2")

    # Classic V3: ~110k base + ~65k per hop (separate pool contracts + ERC20 SSTORE balance updates)
    v3_l2_gas = 110_000 + (hops_count * 65_000)
    # V4 Singleton: ~85k base + ~34k per hop (single PoolManager + EIP-1153 TSTORE/TLOAD delta netting)
    v4_l2_gas = 85_000 + (hops_count * 34_000)

    v3_total_wei = (v3_l2_gas * l2_gas_price_wei) + l1_data_fee_wei
    v4_total_wei = (v4_l2_gas * l2_gas_price_wei) + l1_data_fee_wei
    saved_gas_units = v3_l2_gas - v4_l2_gas
    saved_pct = round(((v3_l2_gas - v4_l2_gas) / v3_l2_gas) * 100.0, 2)

    return {
        "hops_count": hops_count,
        "v3_multi_pool_l2_gas_units": v3_l2_gas,
        "v4_singleton_unlock_l2_gas_units": v4_l2_gas,
        "l2_gas_units_saved": saved_gas_units,
        "l2_gas_reduction_pct": saved_pct,
        "v3_total_cost_wei": v3_total_wei,
        "v4_total_cost_wei": v4_total_wei,
    }
