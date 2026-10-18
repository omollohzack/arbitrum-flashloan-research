"""Conservative arithmetic for candidate arbitrage routes; no trade execution."""
from dataclasses import dataclass
from decimal import Decimal

D = Decimal

@dataclass(frozen=True)
class ProfitEstimate:
    input_amount: Decimal
    gross_profit: Decimal
    pool_fees: Decimal
    flash_loan_fee: Decimal
    gas_cost: Decimal
    l1_data_fee: Decimal
    slippage_reserve: Decimal
    other_costs: Decimal
    estimated_net_profit: Decimal
    estimated_net_profit_pct: Decimal

def estimate_profit(
    input_amount,
    buy_price,
    sell_price,
    buy_fee_bps=0,
    sell_fee_bps=0,
    flash_loan_fee_bps=0,
    gas_cost=0,
    l1_data_fee=0,
    slippage_reserve=0,
    other_costs=0,
) -> ProfitEstimate:
    """Estimate profit in the same quote currency as prices and costs.

    buy_price and sell_price are quote-currency per unit of base token.
    This is a simplified spot model: it does not model concentrated-liquidity
    price impact. Treat output as a filter, never as proof a route is executable.
    """
    amount, bp, sp = D(str(input_amount)), D(str(buy_price)), D(str(sell_price))
    if amount <= 0 or bp <= 0 or sp <= 0:
        raise ValueError("input_amount and prices must be positive")
    if min(D(str(buy_fee_bps)), D(str(sell_fee_bps)), D(str(flash_loan_fee_bps))) < 0:
        raise ValueError("fee basis points cannot be negative")
    if min(D(str(gas_cost)), D(str(l1_data_fee)), D(str(slippage_reserve)), D(str(other_costs))) < 0:
        raise ValueError("cost estimates cannot be negative")
    # Spend input_amount to acquire base at buy_price, then value at sell_price.
    gross = amount * (sp / bp - 1)
    pool_fees = amount * (D(str(buy_fee_bps)) + D(str(sell_fee_bps))) / D(10000)
    loan_fee = amount * D(str(flash_loan_fee_bps)) / D(10000)
    costs = pool_fees + loan_fee + sum(map(lambda x: D(str(x)),
        (gas_cost, l1_data_fee, slippage_reserve, other_costs)))
    net = gross - costs
    return ProfitEstimate(
        input_amount=amount, gross_profit=gross, pool_fees=pool_fees,
        flash_loan_fee=loan_fee, gas_cost=D(str(gas_cost)),
        l1_data_fee=D(str(l1_data_fee)), slippage_reserve=D(str(slippage_reserve)),
        other_costs=D(str(other_costs)), estimated_net_profit=net,
        estimated_net_profit_pct=(net / amount * 100),
    )

if __name__ == "__main__":
    # Clearly labeled arithmetic example, not a live quote or forecast.
    result = estimate_profit(1000, 1, "1.02", 30, 5, 0, 2, 0.2, 3)
    print("ILLUSTRATIVE ONLY — no market data used")
    for key, value in result.__dict__.items():
        print(f"{key}: {value}")
