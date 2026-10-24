#!/usr/bin/env python3
"""
bot/liquidation_and_keeper_scanner.py — Read-Only Zero-Capital Liquidation & Keeper Bounty Scanner (v3.4)
==========================================================================================================
Patches Hole #3 by adding read-only, block-pinned scanners for the two highest-margin zero-starting-capital
smart-contract revenue lanes identified in empirical DeFi literature & GitHub repositories:

1. Atomic Flash-Loan Liquidations (`Aave V3 Pool` `0x794a61358D6845594F94dc1DB02A252b5b4814aD`):
   - Grounded in Qin et al. (`arXiv:2106.06389v2`), `morpho-org/morpho-blue-liquidation-bot`, and
     `0xnavarro/Aave-Liquiditor`.
   - Calls `getUserAccountData(address user)` (`0xbf92857c`) at a pinned Arbitrum One block, decodes
     all 6 256-bit words (`totalCollateralBase`, `totalDebtBase`, `availableBorrowsBase`,
     `currentLiquidationThreshold`, `ltv`, `healthFactor`), enforces Aave V3's exact close factor
     (`50%` when `0.95e18 <= HF < 1.00e18`, `100%` when `HF < 0.95e18`), and calculates net profit
     in raw integer debt-token units after flash-loan fee, DEX swap slippage, and L1+L2 gas.

2. Permissionless Yield-Vault Harvest Keepers (`Beefy Finance` `callReward()` `0x6b29a679`):
   - Grounded in `beefyfinance/beefy-contracts` (`StratFeeManagerInitializable.sol` / `callReward()`).
   - Requires ZERO flash loan and ZERO custom smart contract deployment (can be triggered from an EOA),
     paying a deterministic native/`WETH` harvest bounty when `callReward() > total_gas_wei + safety_buffer_wei`.
"""

from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Dict, List

from opportunity_scanner import (
    ARBITRUM_ONE_CHAIN_ID,
    ChainIdMismatchError,
    ConfigValidationError,
    JsonRpcClient,
    decode_uint_word,
    encode_address,
    verify_address_format,
)
from profit_calculator import convert_wei_to_quote_raw

AAVE_V3_POOL_ARBITRUM = "0x794a61358D6845594F94dc1DB02A252b5b4814aD"
SEL_AAVE_GET_USER_ACCOUNT_DATA = "0xbf92857c"  # getUserAccountData(address)
SEL_STRATEGY_CALL_REWARD = "0x6b29a679"        # callReward()
SEL_STRATEGY_PAUSED = "0x5c975abb"             # paused()
HF_LIQUIDATION_THRESHOLD_WAD = 10 ** 18        # 1.00 in 1e18 WAD
HF_FULL_LIQUIDATION_WAD = 95 * (10 ** 16)      # 0.95 in 1e18 WAD


@dataclass(frozen=True)
class AaveBorrowerLiquidationCandidate:
    classification: str               # "ESTIMATED_LIQUIDATABLE_UNVERIFIED" or "HEALTHY_OR_UNPROFITABLE"
    pinned_block_number: int
    borrower: str
    aave_pool: str
    total_collateral_base_8dec: int
    total_debt_base_8dec: int
    current_liquidation_threshold_bps: int
    health_factor_wad: int
    health_factor_float: float
    is_health_factor_below_one: bool
    close_factor_bps: int             # 5000 (50%) or 10000 (100%)
    debt_token_symbol: str
    debt_token_decimals: int
    max_debt_to_cover_raw: int
    liquidation_bonus_bps: int
    gross_bonus_raw: int
    flash_loan_fee_bps: int
    flash_loan_fee_raw: int
    swap_slippage_bps: int
    swap_slippage_raw: int
    total_gas_wei: int
    gas_cost_debt_raw: int
    net_profit_raw: int
    net_profit_human: str
    is_net_positive: bool

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass(frozen=True)
class KeeperHarvestCandidate:
    classification: str               # "ESTIMATED_HARVESTABLE_UNVERIFIED" or "UNPROFITABLE_OR_PAUSED"
    pinned_block_number: int
    strategy_name: str
    strategy_address: str
    is_paused: bool
    call_reward_wei: int
    total_gas_wei: int
    safety_buffer_wei: int
    net_reward_wei: int
    net_reward_eth: str
    is_net_positive: bool

    def to_dict(self) -> Dict:
        return asdict(self)


def evaluate_aave_v3_liquidation_candidate(
    *,
    pinned_block_number: int,
    borrower: str,
    total_collateral_base_8dec: int,
    total_debt_base_8dec: int,
    current_liquidation_threshold_bps: int,
    health_factor_wad: int,
    debt_token_symbol: str = "USDC",
    debt_token_decimals: int = 6,
    debt_amount_raw: int,
    liquidation_bonus_bps: int = 500,    # 5.00% standard Aave V3 bonus on WETH/USDC
    flash_loan_fee_bps: int = 0,         # 0 bps via Balancer V2 or Morpho Blue
    swap_slippage_bps: int = 20,         # 20 bps (0.20%) swap + price impact buffer
    l2_gas_units: int = 380_000,
    l2_gas_price_wei: int = 20_000_000,
    l1_data_fee_wei: int = 18_000_000_000_000,
    eth_to_debt_rate: Decimal = Decimal("2650.00"),
    aave_pool: str = AAVE_V3_POOL_ARBITRUM,
) -> AaveBorrowerLiquidationCandidate:
    """
    Pure integer accounting evaluation of an Aave V3 borrower's liquidation profitability.
    """
    b_addr = verify_address_format(borrower, "borrower")
    p_addr = verify_address_format(aave_pool, "aave_pool")
    if debt_amount_raw < 0:
        raise ConfigValidationError("debt_amount_raw cannot be negative")

    is_underwater = health_factor_wad < HF_LIQUIDATION_THRESHOLD_WAD and debt_amount_raw > 0
    if not is_underwater:
        close_factor_bps = 0
        max_debt_to_cover_raw = 0
        gross_bonus_raw = 0
        flash_fee_raw = 0
        slippage_raw = 0
    else:
        close_factor_bps = 10_000 if health_factor_wad < HF_FULL_LIQUIDATION_WAD else 5_000
        max_debt_to_cover_raw = (debt_amount_raw * close_factor_bps) // 10_000
        gross_bonus_raw = int(
            (Decimal(max_debt_to_cover_raw) * Decimal(liquidation_bonus_bps) / Decimal(10_000)).to_integral_value(
                rounding=ROUND_FLOOR
            )
        )
        flash_fee_raw = int(
            (Decimal(max_debt_to_cover_raw) * Decimal(flash_loan_fee_bps) / Decimal(10_000)).to_integral_value(
                rounding=ROUND_CEILING
            )
        )
        seized_equiv_raw = max_debt_to_cover_raw + gross_bonus_raw
        slippage_raw = int(
            (Decimal(seized_equiv_raw) * Decimal(swap_slippage_bps) / Decimal(10_000)).to_integral_value(
                rounding=ROUND_CEILING
            )
        )

    total_gas_wei = (l2_gas_units * l2_gas_price_wei) + l1_data_fee_wei
    gas_cost_debt_raw = convert_wei_to_quote_raw(
        wei_amount=total_gas_wei,
        quote_token_decimals=debt_token_decimals,
        eth_to_quote_rate=eth_to_debt_rate,
    )

    if not is_underwater:
        net_profit_raw = -gas_cost_debt_raw
    else:
        net_profit_raw = gross_bonus_raw - flash_fee_raw - slippage_raw - gas_cost_debt_raw

    is_net_positive = is_underwater and net_profit_raw > 0
    scale = Decimal(10 ** debt_token_decimals)
    net_human = f"{(Decimal(net_profit_raw) / scale):.{min(debt_token_decimals, 8)}f}"

    return AaveBorrowerLiquidationCandidate(
        classification=(
            "ESTIMATED_LIQUIDATABLE_UNVERIFIED"
            if is_net_positive
            else "HEALTHY_OR_UNPROFITABLE"
        ),
        pinned_block_number=pinned_block_number,
        borrower=b_addr,
        aave_pool=p_addr,
        total_collateral_base_8dec=total_collateral_base_8dec,
        total_debt_base_8dec=total_debt_base_8dec,
        current_liquidation_threshold_bps=current_liquidation_threshold_bps,
        health_factor_wad=health_factor_wad,
        health_factor_float=round(float(Decimal(health_factor_wad) / Decimal(10 ** 18)), 6),
        is_health_factor_below_one=is_underwater,
        close_factor_bps=close_factor_bps,
        debt_token_symbol=debt_token_symbol,
        debt_token_decimals=debt_token_decimals,
        max_debt_to_cover_raw=max_debt_to_cover_raw,
        liquidation_bonus_bps=liquidation_bonus_bps,
        gross_bonus_raw=gross_bonus_raw,
        flash_loan_fee_bps=flash_loan_fee_bps,
        flash_loan_fee_raw=flash_fee_raw,
        swap_slippage_bps=swap_slippage_bps,
        swap_slippage_raw=slippage_raw,
        total_gas_wei=total_gas_wei,
        gas_cost_debt_raw=gas_cost_debt_raw,
        net_profit_raw=net_profit_raw,
        net_profit_human=net_human,
        is_net_positive=is_net_positive,
    )


def scan_aave_v3_borrowers_readonly(
    *,
    rpc: JsonRpcClient,
    borrowers: List[Dict],
    pinned_block_number: int,
    aave_pool: str = AAVE_V3_POOL_ARBITRUM,
    eth_to_debt_rate: Decimal = Decimal("2650.00"),
) -> List[AaveBorrowerLiquidationCandidate]:
    """
    Queries `AaveV3Pool.getUserAccountData(address)` (`0xbf92857c`) at `pinned_block_number`
    for each configured borrower dict (`{"address": ..., "debt_token_symbol": "USDC", "debt_token_decimals": 6, "liquidation_bonus_bps": 500}`).
    """
    if pinned_block_number <= 0:
        raise ConfigValidationError("pinned_block_number must be > 0")
    chain_id_hex = rpc.call("eth_chainId", [])
    chain_id = int(chain_id_hex, 16) if isinstance(chain_id_hex, str) else int(chain_id_hex)
    if chain_id != ARBITRUM_ONE_CHAIN_ID:
        raise ChainIdMismatchError(f"Expected Arbitrum One (42161), got {chain_id}")

    p_addr = verify_address_format(aave_pool, "aave_pool")
    block_tag = hex(pinned_block_number)
    candidates: List[AaveBorrowerLiquidationCandidate] = []

    for b_entry in borrowers:
        b_addr = verify_address_format(b_entry["address"], "borrower.address")
        calldata = SEL_AAVE_GET_USER_ACCOUNT_DATA + encode_address(b_addr)
        ret_hex = rpc.eth_call(p_addr, calldata, block_tag)

        collat_base = decode_uint_word(ret_hex, 0)
        debt_base = decode_uint_word(ret_hex, 1)
        liq_threshold_bps = decode_uint_word(ret_hex, 3)
        hf_wad = decode_uint_word(ret_hex, 5)

        debt_dec = int(b_entry.get("debt_token_decimals", 6))
        # Convert 8-decimal USD base debt into debt token raw units if explicit debt_amount_raw not supplied
        if "debt_amount_raw" in b_entry:
            debt_raw = int(b_entry["debt_amount_raw"])
        else:
            debt_raw = (debt_base * (10 ** debt_dec)) // (10 ** 8)

        cand = evaluate_aave_v3_liquidation_candidate(
            pinned_block_number=pinned_block_number,
            borrower=b_addr,
            total_collateral_base_8dec=collat_base,
            total_debt_base_8dec=debt_base,
            current_liquidation_threshold_bps=liq_threshold_bps,
            health_factor_wad=hf_wad,
            debt_token_symbol=b_entry.get("debt_token_symbol", "USDC"),
            debt_token_decimals=debt_dec,
            debt_amount_raw=debt_raw,
            liquidation_bonus_bps=int(b_entry.get("liquidation_bonus_bps", 500)),
            flash_loan_fee_bps=int(b_entry.get("flash_loan_fee_bps", 0)),
            swap_slippage_bps=int(b_entry.get("swap_slippage_bps", 20)),
            eth_to_debt_rate=eth_to_debt_rate,
            aave_pool=p_addr,
        )
        candidates.append(cand)

    candidates.sort(key=lambda c: c.net_profit_raw, reverse=True)
    return candidates


def scan_permissionless_keeper_bounties_readonly(
    *,
    rpc: JsonRpcClient,
    strategies: List[Dict],
    pinned_block_number: int,
    l2_gas_units: int = 280_000,
    l2_gas_price_wei: int = 20_000_000,
    l1_data_fee_wei: int = 10_000_000_000_000,
    safety_buffer_wei: int = 5_000_000_000_000,
) -> List[KeeperHarvestCandidate]:
    """
    Queries `callReward()` (`0x6b29a679`) and `paused()` (`0x5c975abb`) on permissionless yield
    strategies (such as Beefy Finance vaults on Arbitrum One) at `pinned_block_number`.
    """
    if pinned_block_number <= 0:
        raise ConfigValidationError("pinned_block_number must be > 0")
    block_tag = hex(pinned_block_number)
    total_gas_wei = (l2_gas_units * l2_gas_price_wei) + l1_data_fee_wei

    results: List[KeeperHarvestCandidate] = []
    for s_cfg in strategies:
        s_addr = verify_address_format(s_cfg["address"], "strategy.address")
        s_name = str(s_cfg.get("name", s_addr))
        paused_hex = rpc.eth_call(s_addr, SEL_STRATEGY_PAUSED, block_tag)
        reward_hex = rpc.eth_call(s_addr, SEL_STRATEGY_CALL_REWARD, block_tag)

        is_paused = decode_uint_word(paused_hex, 0) == 1
        reward_wei = decode_uint_word(reward_hex, 0)
        net_wei = reward_wei - total_gas_wei - safety_buffer_wei if not is_paused else -total_gas_wei
        is_pos = (not is_paused) and (net_wei > 0)

        net_eth = f"{(Decimal(net_wei) / Decimal(10 ** 18)):.8f}"
        results.append(
            KeeperHarvestCandidate(
                classification="ESTIMATED_HARVESTABLE_UNVERIFIED" if is_pos else "UNPROFITABLE_OR_PAUSED",
                pinned_block_number=pinned_block_number,
                strategy_name=s_name,
                strategy_address=s_addr,
                is_paused=is_paused,
                call_reward_wei=reward_wei,
                total_gas_wei=total_gas_wei,
                safety_buffer_wei=safety_buffer_wei,
                net_reward_wei=net_wei,
                net_reward_eth=net_eth,
                is_net_positive=is_pos,
            )
        )

    results.sort(key=lambda r: r.net_reward_wei, reverse=True)
    return results
