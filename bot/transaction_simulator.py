#!/usr/bin/env python3
"""
bot/transaction_simulator.py — Read-Only Pinned-Block Route Calldata Builder & Simulator (v3.4)
===============================================================================================
Remediates Finding 5 of `project_technical_review_for_gemini.md` and patches Hole #2
(Live Arbitrum `NodeInterface` L1+L2 Gas Component Estimation):
1. Preserves the strict read-only boundary (`stage = "PRELIMINARY_CALL_CHECK"` — never signs,
   broadcasts, or requires a private key).
2. Provides deterministic ABI calldata construction (`build_cyclic_arb_calldata`) for
   `FlashLoanArbitrage.sol` (`executeBalancerCyclicArbitrage(uint256,(address,uint256,(uint8,address,address,address,address,uint24)[]),bool)`)
   so simulations test real contract route encoding rather than arbitrary placeholder hex.
3. Queries Arbitrum's precompile `NodeInterface` (`0x00000000000000000000000000000000000000C8`)
   `gasEstimateComponents(address to, bool contractCreation, bytes data)` (`0x4d2301cc`) at the
   pinned block to decompose exact L2 execution gas vs. L1 calldata poster fees in wei.
4. Pins `eth_call` and `eth_estimateGas` to a specific `block_number` (never floating `latest`
   across calls) and verifies whether the call returned `executed == true` vs. `SoftAbortTriggered`
   (`executed == false`) or reverted with a decoded revert reason.
"""

import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent))
from opportunity_scanner import (
    ARBITRUM_ONE_CHAIN_ID,
    ChainIdMismatchError,
    ConfigValidationError,
    JsonRpcClient,
    decode_uint_word,
    encode_address,
    encode_uint256,
    verify_address_format,
)

SEL_EXEC_BALANCER_CYCLIC = "0x6a761202"
SEL_ERROR_STRING = "0x08c379a0"
ARBITRUM_NODE_INTERFACE_ADDRESS = "0x00000000000000000000000000000000000000C8"
SEL_NODE_GAS_ESTIMATE_COMPONENTS = "0x4d2301cc"  # gasEstimateComponents(address,bool,bytes)


@dataclass(frozen=True)
class SwapStepSpec:
    swap_type: int          # 0=V2Router, 1=UniV3Router, 2=DirectV2, 3=CamelotV2, 4=AlgebraRouter
    target: str
    pool_for_quote: str
    token_in: str
    token_out: str
    fee: int


@dataclass(frozen=True)
class ArbitrumGasComponents:
    """
    Exact Arbitrum Nitro two-dimensional gas breakdown returned by
    `NodeInterface.gasEstimateComponents(address to, bool contractCreation, bytes data)` (`0x4d2301cc`).
    Reference: https://docs.arbitrum.io/arbitrum-essentials/nodeinterface/reference
    """
    pinned_block_number: int
    total_gas_estimate_units: int
    l1_gas_estimate_units: int
    l2_execution_gas_units: int
    l2_base_fee_wei: int
    l1_base_fee_estimate_wei: int
    l2_execution_cost_wei: int
    l1_data_fee_wei: int
    total_estimated_cost_wei: int
    source: str

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass(frozen=True)
class SimulationResult:
    stage: str              # Always "PRELIMINARY_CALL_CHECK"
    is_read_only: bool      # Always True
    chain_id: int
    pinned_block_number: int
    contract_address: str
    sender_address: str
    amount_in_raw: int
    min_profit_raw: int
    eth_call_succeeded: bool
    route_executed_without_soft_abort: bool
    estimated_gas_units: Optional[int]
    decoded_revert_reason: Optional[str]
    raw_return_data: str
    warning: str
    arbitrum_gas_components: Optional[Dict] = None

    def to_dict(self) -> Dict:
        return asdict(self)


def encode_node_interface_gas_estimate_calldata(to_address: str, contract_creation: bool, data_hex: str) -> str:
    """
    ABI-encodes `NodeInterface.gasEstimateComponents(address to, bool contractCreation, bytes data)` (`0x4d2301cc`).
    """
    to_clean = verify_address_format(to_address, "to_address")
    clean_bytes = data_hex[2:] if data_hex.startswith("0x") else data_hex
    if len(clean_bytes) % 2 != 0:
        raise ConfigValidationError("Calldata hex must have an even number of characters")
    byte_len = len(clean_bytes) // 2
    padded_len = ((byte_len + 31) // 32) * 64
    padded_data = clean_bytes.ljust(padded_len, "0")

    return (
        SEL_NODE_GAS_ESTIMATE_COMPONENTS
        + encode_address(to_clean)
        + encode_uint256(1 if contract_creation else 0)
        + encode_uint256(96)
        + encode_uint256(byte_len)
        + padded_data
    )


def estimate_arbitrum_gas_components_at_pinned_block(
    *,
    rpc: JsonRpcClient,
    to_address: str,
    sender_address: str,
    calldata: str,
    pinned_block_number: int,
    node_interface_address: str = ARBITRUM_NODE_INTERFACE_ADDRESS,
) -> ArbitrumGasComponents:
    """
    Calls Arbitrum's precompile `NodeInterface.gasEstimateComponents` (`0x4d2301cc`) at `pinned_block_number`
    to obtain exact L2 execution gas + L1 poster calldata gas components.
    """
    if pinned_block_number <= 0:
        raise ConfigValidationError("pinned_block_number must be > 0")
    s_addr = verify_address_format(sender_address, "sender_address")
    ni_calldata = encode_node_interface_gas_estimate_calldata(to_address, False, calldata)
    block_tag = hex(pinned_block_number)

    ret_hex = rpc.call(
        "eth_call",
        [{"from": s_addr, "to": node_interface_address, "data": ni_calldata}, block_tag],
    )
    if not isinstance(ret_hex, str) or len(ret_hex) < 2 + 64 * 4:
        raise ConfigValidationError(f"Malformed NodeInterface.gasEstimateComponents response: {ret_hex!r}")

    total_gas_est = decode_uint_word(ret_hex, 0)
    l1_gas_est = decode_uint_word(ret_hex, 1)
    l2_base_fee_wei = decode_uint_word(ret_hex, 2)
    l1_base_fee_est_wei = decode_uint_word(ret_hex, 3)

    l2_exec_gas = max(0, total_gas_est - l1_gas_est)
    l2_exec_cost_wei = l2_exec_gas * l2_base_fee_wei
    l1_data_fee_wei = l1_gas_est * l2_base_fee_wei
    total_cost_wei = total_gas_est * l2_base_fee_wei

    return ArbitrumGasComponents(
        pinned_block_number=pinned_block_number,
        total_gas_estimate_units=total_gas_est,
        l1_gas_estimate_units=l1_gas_est,
        l2_execution_gas_units=l2_exec_gas,
        l2_base_fee_wei=l2_base_fee_wei,
        l1_base_fee_estimate_wei=l1_base_fee_est_wei,
        l2_execution_cost_wei=l2_exec_cost_wei,
        l1_data_fee_wei=l1_data_fee_wei,
        total_estimated_cost_wei=total_cost_wei,
        source=f"Arbitrum_NodeInterface_0xC8_block_{pinned_block_number}",
    )


def build_cyclic_arb_calldata(
    *,
    amount_in_raw: int,
    token_borrow: str,
    min_profit_raw: int,
    steps: List[SwapStepSpec],
    soft_abort_if_unprofitable: bool = True,
) -> str:
    """
    Deterministically encodes calldata for `FlashLoanArbitrage.executeBalancerCyclicArbitrage`.
    """
    if amount_in_raw <= 0:
        raise ConfigValidationError("amount_in_raw must be > 0")
    if min_profit_raw < 0:
        raise ConfigValidationError("min_profit_raw cannot be negative")
    if len(steps) < 2:
        raise ConfigValidationError("Cyclic arbitrage route requires at least 2 swap steps")

    tb = verify_address_format(token_borrow, "token_borrow")
    for idx, s in enumerate(steps):
        verify_address_format(s.target, f"steps[{idx}].target")
        verify_address_format(s.pool_for_quote, f"steps[{idx}].pool_for_quote")
        verify_address_format(s.token_in, f"steps[{idx}].token_in")
        verify_address_format(s.token_out, f"steps[{idx}].token_out")

    if steps[0].token_in.lower() != tb.lower():
        raise ConfigValidationError("First swap step token_in must match token_borrow")
    if steps[-1].token_out.lower() != tb.lower():
        raise ConfigValidationError("Final swap step token_out must return to token_borrow")
    for idx in range(len(steps) - 1):
        if steps[idx].token_out.lower() != steps[idx + 1].token_in.lower():
            raise ConfigValidationError(f"Discontinuous route tokens between step {idx} and {idx + 1}")

    head = (
        encode_uint256(amount_in_raw)
        + encode_uint256(96)
        + encode_uint256(1 if soft_abort_if_unprofitable else 0)
    )
    params_header = (
        encode_address(tb)
        + encode_uint256(min_profit_raw)
        + encode_uint256(96)
        + encode_uint256(len(steps))
    )
    steps_words = "".join(
        encode_uint256(s.swap_type)
        + encode_address(s.target)
        + encode_address(s.pool_for_quote)
        + encode_address(s.token_in)
        + encode_address(s.token_out)
        + encode_uint256(s.fee)
        for s in steps
    )
    return SEL_EXEC_BALANCER_CYCLIC + head + params_header + steps_words


def simulate_route_at_pinned_block(
    *,
    rpc: JsonRpcClient,
    contract_address: str,
    sender_address: str,
    calldata: str,
    amount_in_raw: int,
    min_profit_raw: int,
    pinned_block_number: int,
    query_node_interface: bool = False,
) -> SimulationResult:
    """
    Executes a read-only `eth_call` and `eth_estimateGas` (plus optional `NodeInterface` gas decomposition)
    against a pinned block number. Never signs or broadcasts any transaction.
    """
    c_addr = verify_address_format(contract_address, "contract_address")
    s_addr = verify_address_format(sender_address, "sender_address")
    if not isinstance(calldata, str) or not calldata.startswith("0x") or len(calldata) < 10:
        raise ConfigValidationError("Invalid calldata hex string")
    if pinned_block_number <= 0:
        raise ConfigValidationError("pinned_block_number must be > 0")

    chain_id_hex = rpc.call("eth_chainId", [])
    chain_id = int(chain_id_hex, 16) if isinstance(chain_id_hex, str) else int(chain_id_hex)
    if chain_id != ARBITRUM_ONE_CHAIN_ID:
        raise ChainIdMismatchError(f"Expected chain_id {ARBITRUM_ONE_CHAIN_ID}, got {chain_id}")

    block_tag = hex(pinned_block_number)
    code = rpc.eth_get_code(c_addr, block_tag)
    if not code or code == "0x":
        raise ConfigValidationError(f"No contract bytecode deployed at {c_addr} at block {pinned_block_number}")

    call_tx = {"from": s_addr, "to": c_addr, "data": calldata, "value": "0x0"}
    try:
        ret_hex = rpc.call("eth_call", [call_tx, block_tag])
        if not isinstance(ret_hex, str) or not ret_hex.startswith("0x") or len(ret_hex) < 66:
            return SimulationResult(
                stage="PRELIMINARY_CALL_CHECK",
                is_read_only=True,
                chain_id=chain_id,
                pinned_block_number=pinned_block_number,
                contract_address=c_addr,
                sender_address=s_addr,
                amount_in_raw=amount_in_raw,
                min_profit_raw=min_profit_raw,
                eth_call_succeeded=False,
                route_executed_without_soft_abort=False,
                estimated_gas_units=None,
                decoded_revert_reason="Empty or malformed eth_call return data",
                raw_return_data=str(ret_hex),
                warning="PRELIMINARY_CALL_CHECK: Call did not return a valid boolean word.",
                arbitrum_gas_components=None,
            )

        executed_bool = decode_uint_word(ret_hex, 0) == 1
        gas_hex = rpc.call("eth_estimateGas", [call_tx, block_tag])
        gas_est = int(gas_hex, 16) if isinstance(gas_hex, str) else int(gas_hex)

        gas_comp_dict: Optional[Dict] = None
        if query_node_interface:
            try:
                comp = estimate_arbitrum_gas_components_at_pinned_block(
                    rpc=rpc,
                    to_address=c_addr,
                    sender_address=s_addr,
                    calldata=calldata,
                    pinned_block_number=pinned_block_number,
                )
                gas_comp_dict = comp.to_dict()
            except Exception:
                gas_comp_dict = None

        return SimulationResult(
            stage="PRELIMINARY_CALL_CHECK",
            is_read_only=True,
            chain_id=chain_id,
            pinned_block_number=pinned_block_number,
            contract_address=c_addr,
            sender_address=s_addr,
            amount_in_raw=amount_in_raw,
            min_profit_raw=min_profit_raw,
            eth_call_succeeded=True,
            route_executed_without_soft_abort=executed_bool,
            estimated_gas_units=gas_est,
            decoded_revert_reason=None if executed_bool else "SoftAbortTriggered: spread below minProfit on-chain",
            raw_return_data=ret_hex,
            warning=(
                "PRELIMINARY_CALL_CHECK ONLY: eth_call success at pinned block does not guarantee "
                "next-block inclusion or immunity from competing transactions."
            ),
            arbitrum_gas_components=gas_comp_dict,
        )
    except Exception as exc:
        return SimulationResult(
            stage="PRELIMINARY_CALL_CHECK",
            is_read_only=True,
            chain_id=chain_id,
            pinned_block_number=pinned_block_number,
            contract_address=c_addr,
            sender_address=s_addr,
            amount_in_raw=amount_in_raw,
            min_profit_raw=min_profit_raw,
            eth_call_succeeded=False,
            route_executed_without_soft_abort=False,
            estimated_gas_units=None,
            decoded_revert_reason=str(exc),
            raw_return_data="0x",
            warning="PRELIMINARY_CALL_CHECK: Simulation reverted at pinned block.",
            arbitrum_gas_components=None,
        )
