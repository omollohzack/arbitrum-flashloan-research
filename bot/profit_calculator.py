#!/usr/bin/env python3
"""
bot/profit_calculator.py — Raw-Unit & Fee-Consistent Arbitrage Cost & Profit Model
===================================================================================
Remediates Finding 4 of `project_technical_review_for_gemini.md`:
1. Operates on raw integer token amounts (`int`) and verified token `decimals` rather
   than naive floating-point spot-price ratios.
2. Prevents double-charging pool swap fees:
   - When `quotes_are_fee_inclusive=True` (e.g. Uniswap V3 `QuoterV2`, Camelot Algebra Quoter,
     or V2 `getAmountsOut`), pool fees are already deducted inside the quoted output and are
     NOT subtracted a second time.
   - When `quotes_are_fee_inclusive=False` (e.g. pre-fee virtual reserve quotes), each leg's
     `hop_fees_bps` is deducted sequentially at that leg.
3. Converts Arbitrum L2 execution gas (`gas_units * l2_gas_price_wei`) and Arbitrum L1 data
   fee (`l1_data_fee_wei` from `NodeInterface` `0x...C8`) into the quote token's raw integer
   units using an explicit `eth_to_quote_rate` and documented rate source/timestamp.
4. Applies flash-loan fee (`flash_loan_fee_bps`) and a conservative execution/slippage margin
   (`slippage_buffer_bps`), returning an explicit `RouteProfitBreakdown`.
5. Retains `screen_indicative_spot_spread()` strictly as an `INDICATIVE_ONLY` pre-filter that
   never labels spot differences as executable profit.
"""

from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Dict, List


class ProfitCalculatorError(ValueError):
    """Raised when inputs to the profit calculator are invalid or inconsistent."""


@dataclass(frozen=True)
class RouteProfitBreakdown:
    """Complete, auditable unit-consistent breakdown of a 2-leg or N-leg route evaluation."""
    classification: str               # "ESTIMATED_NET_POSITIVE_UNVERIFIED" or "NET_NEGATIVE_OR_BREAKEVEN"
    quote_token_symbol: str
    quote_token_decimals: int
    amount_in_raw: int
    gross_amount_out_raw: int
    effective_amount_out_raw: int
    quotes_are_fee_inclusive: bool
    hop_fees_bps: List[int]
    explicit_hop_fee_deduction_raw: int
    flash_loan_fee_bps: int
    flash_loan_fee_raw: int
    slippage_buffer_bps: int
    slippage_buffer_raw: int
    l2_gas_units: int
    l2_gas_price_wei: int
    l1_data_fee_wei: int
    total_gas_wei: int
    eth_to_quote_rate: str
    rate_source: str
    gas_cost_quote_raw: int
    flat_operational_cost_raw: int
    total_repayment_required_raw: int
    net_profit_raw: int
    net_profit_human: str
    net_margin_bps: float
    is_net_positive: bool
    assumptions: List[str]

    def to_dict(self) -> Dict:
        return asdict(self)


def convert_wei_to_quote_raw(
    wei_amount: int,
    quote_token_decimals: int,
    eth_to_quote_rate: Decimal,
) -> int:
    """
    Converts an ETH wei cost (`1e18` precision) into raw units of `quote_token`
    using ceiling rounding so gas costs are never underestimated.
    """
    if wei_amount < 0:
        raise ProfitCalculatorError("wei_amount cannot be negative")
    if quote_token_decimals < 0 or quote_token_decimals > 36:
        raise ProfitCalculatorError(f"Invalid quote_token_decimals: {quote_token_decimals}")
    if eth_to_quote_rate <= Decimal("0"):
        raise ProfitCalculatorError("eth_to_quote_rate must be strictly positive")

    if wei_amount == 0:
        return 0

    eth_decimal = Decimal(wei_amount) / Decimal(10 ** 18)
    quote_human = eth_decimal * eth_to_quote_rate
    quote_raw_dec = quote_human * Decimal(10 ** quote_token_decimals)
    return int(quote_raw_dec.to_integral_value(rounding=ROUND_CEILING))


def evaluate_exact_route_profit(
    *,
    quote_token_symbol: str,
    quote_token_decimals: int,
    amount_in_raw: int,
    quoted_amount_out_raw: int,
    quotes_are_fee_inclusive: bool,
    hop_fees_bps: List[int],
    flash_loan_fee_bps: int = 0,
    l2_gas_units: int = 240_000,
    l2_gas_price_wei: int = 20_000_000,       # 0.02 gwei on Arbitrum One
    l1_data_fee_wei: int = 13_920_000_000_000, # ~0.00001392 ETH from NodeInterface
    eth_to_quote_rate: float | str | Decimal = Decimal("2650.00"),
    rate_source: str = "pinned_onchain_pool_mid",
    slippage_buffer_bps: int = 5,
    flat_operational_cost_raw: int = 0,
) -> RouteProfitBreakdown:
    """
    Evaluates net arbitrage profit from an exact-input route quote in raw token units.
    """
    if amount_in_raw <= 0:
        raise ProfitCalculatorError("amount_in_raw must be > 0")
    if quoted_amount_out_raw < 0:
        raise ProfitCalculatorError("quoted_amount_out_raw cannot be negative")
    if quote_token_decimals < 0 or quote_token_decimals > 36:
        raise ProfitCalculatorError(f"Invalid quote_token_decimals: {quote_token_decimals}")
    if flash_loan_fee_bps < 0 or flash_loan_fee_bps >= 10_000:
        raise ProfitCalculatorError(f"Invalid flash_loan_fee_bps: {flash_loan_fee_bps}")
    if slippage_buffer_bps < 0 or slippage_buffer_bps >= 10_000:
        raise ProfitCalculatorError(f"Invalid slippage_buffer_bps: {slippage_buffer_bps}")
    if l2_gas_units < 0 or l2_gas_price_wei < 0 or l1_data_fee_wei < 0:
        raise ProfitCalculatorError("Gas parameters cannot be negative")
    if flat_operational_cost_raw < 0:
        raise ProfitCalculatorError("flat_operational_cost_raw cannot be negative")
    if not hop_fees_bps or any(f < 0 or f >= 10_000 for f in hop_fees_bps):
        raise ProfitCalculatorError(f"Invalid hop_fees_bps: {hop_fees_bps}")

    rate_dec = Decimal(str(eth_to_quote_rate))
    if rate_dec <= Decimal("0"):
        raise ProfitCalculatorError("eth_to_quote_rate must be > 0")

    # 1. Handle pool swap fees without double-counting (Finding 4.3)
    if quotes_are_fee_inclusive:
        effective_out_raw = quoted_amount_out_raw
        explicit_hop_fee_deduction_raw = 0
        fee_note = "Pool swap fees already embedded in exact-input quote (no second fee subtraction)."
    else:
        curr = Decimal(quoted_amount_out_raw)
        for fee_bps in hop_fees_bps:
            curr = (curr * Decimal(10_000 - fee_bps)) / Decimal(10_000)
        effective_out_raw = int(curr.to_integral_value(rounding=ROUND_FLOOR))
        explicit_hop_fee_deduction_raw = quoted_amount_out_raw - effective_out_raw
        fee_note = f"Pre-fee quote adjusted sequentially across {len(hop_fees_bps)} hops: {hop_fees_bps} bps."

    # 2. Flash-loan fee in raw integer units (ceiling rounded)
    flash_loan_fee_raw = int(
        (Decimal(amount_in_raw) * Decimal(flash_loan_fee_bps) / Decimal(10_000)).to_integral_value(
            rounding=ROUND_CEILING
        )
    )

    # 3. Slippage / competition safety margin in raw units (ceiling rounded on principal)
    slippage_buffer_raw = int(
        (Decimal(amount_in_raw) * Decimal(slippage_buffer_bps) / Decimal(10_000)).to_integral_value(
            rounding=ROUND_CEILING
        )
    )

    # 4. Arbitrum L2 execution gas + L1 calldata fee in wei -> quote token raw units
    total_gas_wei = (l2_gas_units * l2_gas_price_wei) + l1_data_fee_wei
    gas_cost_quote_raw = convert_wei_to_quote_raw(
        wei_amount=total_gas_wei,
        quote_token_decimals=quote_token_decimals,
        eth_to_quote_rate=rate_dec,
    )

    # 5. Net profit in raw units
    total_repayment_required_raw = amount_in_raw + flash_loan_fee_raw
    total_costs_raw = (
        flash_loan_fee_raw
        + slippage_buffer_raw
        + gas_cost_quote_raw
        + flat_operational_cost_raw
    )
    net_profit_raw = effective_out_raw - amount_in_raw - total_costs_raw

    scale = Decimal(10 ** quote_token_decimals)
    net_profit_human_dec = Decimal(net_profit_raw) / scale
    net_margin_bps = float((Decimal(net_profit_raw) / Decimal(amount_in_raw)) * Decimal(10_000))
    is_net_positive = net_profit_raw > 0

    classification = (
        "ESTIMATED_NET_POSITIVE_UNVERIFIED"
        if is_net_positive
        else "NET_NEGATIVE_OR_BREAKEVEN"
    )

    assumptions = [
        fee_note,
        f"Flash-loan fee charged at {flash_loan_fee_bps} bps ({flash_loan_fee_raw} raw {quote_token_symbol}).",
        f"L2 gas ({l2_gas_units} units @ {l2_gas_price_wei} wei) + L1 data fee ({l1_data_fee_wei} wei) converted at {rate_dec} {quote_token_symbol}/ETH (source: {rate_source}).",
        f"Slippage/competition buffer of {slippage_buffer_bps} bps ({slippage_buffer_raw} raw {quote_token_symbol}) deducted.",
        "Unguaranteed until verified by pinned-block transaction simulation (eth_call / local fork).",
    ]

    return RouteProfitBreakdown(
        classification=classification,
        quote_token_symbol=quote_token_symbol,
        quote_token_decimals=quote_token_decimals,
        amount_in_raw=amount_in_raw,
        gross_amount_out_raw=quoted_amount_out_raw,
        effective_amount_out_raw=effective_out_raw,
        quotes_are_fee_inclusive=quotes_are_fee_inclusive,
        hop_fees_bps=list(hop_fees_bps),
        explicit_hop_fee_deduction_raw=explicit_hop_fee_deduction_raw,
        flash_loan_fee_bps=flash_loan_fee_bps,
        flash_loan_fee_raw=flash_loan_fee_raw,
        slippage_buffer_bps=slippage_buffer_bps,
        slippage_buffer_raw=slippage_buffer_raw,
        l2_gas_units=l2_gas_units,
        l2_gas_price_wei=l2_gas_price_wei,
        l1_data_fee_wei=l1_data_fee_wei,
        total_gas_wei=total_gas_wei,
        eth_to_quote_rate=str(rate_dec),
        rate_source=rate_source,
        gas_cost_quote_raw=gas_cost_quote_raw,
        flat_operational_cost_raw=flat_operational_cost_raw,
        total_repayment_required_raw=total_repayment_required_raw,
        net_profit_raw=net_profit_raw,
        net_profit_human=f"{net_profit_human_dec:.{min(quote_token_decimals, 8)}f}",
        net_margin_bps=round(net_margin_bps, 4),
        is_net_positive=is_net_positive,
        assumptions=assumptions,
    )


def screen_indicative_spot_spread(
    *,
    buy_pool_spot_price: float,
    sell_pool_spot_price: float,
    buy_fee_bps: int,
    sell_fee_bps: int,
    flash_loan_fee_bps: int = 0,
) -> Dict:
    """
    Fast `INDICATIVE_ONLY` spot-price pre-screening filter (Finding 2.1 & Finding 4.1).
    Never reports a dollar profit or labels a candidate as executable arbitrage.
    """
    if buy_pool_spot_price <= 0 or sell_pool_spot_price <= 0:
        raise ProfitCalculatorError("Spot prices must be > 0")
    if buy_fee_bps < 0 or sell_fee_bps < 0 or flash_loan_fee_bps < 0:
        raise ProfitCalculatorError("Fee bps cannot be negative")

    raw_spread_bps = ((sell_pool_spot_price / buy_pool_spot_price) - 1.0) * 10_000.0
    total_fee_hurdle_bps = float(buy_fee_bps + sell_fee_bps + flash_loan_fee_bps)
    indicative_excess_bps = raw_spread_bps - total_fee_hurdle_bps

    return {
        "classification": "INDICATIVE_ONLY_SPOT_SCREEN",
        "executable": False,
        "raw_spot_spread_bps": round(raw_spread_bps, 4),
        "total_fee_hurdle_bps": round(total_fee_hurdle_bps, 4),
        "indicative_excess_bps": round(indicative_excess_bps, 4),
        "passes_fee_hurdle_for_quoting": indicative_excess_bps > 0.0,
        "warning": (
            "INDICATIVE ONLY: Spot-price spread excludes tick liquidity, price impact, "
            "L2 gas, and L1 calldata fees. Must be verified via exact-input Quoter."
        ),
    }
