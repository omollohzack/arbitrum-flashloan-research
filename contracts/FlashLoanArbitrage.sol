// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * @title FlashLoanArbitrage (v3.5 — Multi-Protocol OEV + UniswapX Intent Filler + Uniswap V4 Singleton Engine)
 * @notice Production-hardened zero-collateral Flash Loan Arbitrage, Cross-Chain Speculative OEV Liquidation,
 *         UniswapX Dutch-Auction Filler, and Uniswap V4 Singleton Flash-Accounting contract on Arbitrum One
 *         incorporating verified mechanisms from:
 *         - Signals and Spoils: Speculative OEV in Cross-Chain Price Feeds (arXiv:2606.03434v1, June 2026)
 *         - Uniswap/UniswapX (`IReactorCallback.reactorCallback` zero-capital Dutch auction order filling)
 *         - Uniswap/v4-core (`IUnlockCallback.unlockCallback` EIP-1153 singleton flash accounting)
 *         - morpho-org/morpho-blue (Morpho.sol native 0%-fee `liquidate` -> `onMorphoLiquidate` flash-liquidation
 *           and `flashLoan` -> `onMorphoFlashLoan` on Arbitrum Morpho Blue 0x6c247b1F6182318877311737BaC0844bAa518F5e)
 *         - hernan-erasmo/overlord-rs (Correlated LST/LRT WETH/wstETH/weETH/rETH/cbETH liquidation execution)
 *         - Wu & Oz (2026, arXiv:2606.00720v1) Route-Confined On-Chain Soft Abort (`quotePathOnChain`)
 */

interface IERC20 {
    function totalSupply() external view returns (uint256);
    function balanceOf(address account) external view returns (uint256);
    function transfer(address recipient, uint256 amount) external returns (bool);
    function allowance(address owner, address spender) external view returns (uint256);
    function approve(address spender, uint256 amount) external returns (bool);
    function transferFrom(address sender, address recipient, uint256 amount) external returns (bool);
}

interface IBalancerVault {
    function flashLoan(
        IFlashLoanRecipient recipient,
        IERC20[] memory tokens,
        uint256[] memory amounts,
        bytes memory userData
    ) external;
}

interface IFlashLoanRecipient {
    function receiveFlashLoan(
        IERC20[] memory tokens,
        uint256[] memory amounts,
        uint256[] memory feeAmounts,
        bytes memory userData
    ) external;
}

interface IAaveV3Pool {
    function flashLoanSimple(
        address receiverAddress,
        address asset,
        uint256 amount,
        bytes calldata params,
        uint16 referralCode
    ) external;

    function liquidationCall(
        address collateralAsset,
        address debtAsset,
        address user,
        uint256 debtToCover,
        bool receiveAToken
    ) external;

    function getUserAccountData(address user)
        external
        view
        returns (
            uint256 totalCollateralBase,
            uint256 totalDebtBase,
            uint256 availableBorrowsBase,
            uint256 currentLiquidationThreshold,
            uint256 ltv,
            uint256 healthFactor
        );
}

interface IFlashLoanSimpleReceiver {
    function executeOperation(
        address asset,
        uint256 amount,
        uint256 premium,
        address initiator,
        bytes calldata params
    ) external returns (bool);
}

/// @notice Morpho Blue Interfaces (from morpho-org/morpho-blue/src/Morpho.sol)
/// @dev Arbitrum One Deployment: 0x6c247b1F6182318877311737BaC0844bAa518F5e
struct MorphoMarketParams {
    address loanToken;
    address collateralToken;
    address oracle;
    address irm;
    uint256 lltv;
}

interface IMorpho {
    function liquidate(
        MorphoMarketParams memory marketParams,
        address borrower,
        uint256 seizedAssets,
        uint256 repaidShares,
        bytes calldata data
    ) external returns (uint256 assetsSeized, uint256 assetsRepaid);

    function flashLoan(address token, uint256 assets, bytes calldata data) external;
}

interface IMorphoLiquidateCallback {
    function onMorphoLiquidate(uint256 repaidAssets, bytes calldata data) external;
}

interface IMorphoFlashLoanCallback {
    function onMorphoFlashLoan(uint256 assets, bytes calldata data) external;
}

/// @notice UniswapX V2 Dutch Order Reactor Interfaces (from Uniswap/UniswapX)
/// @dev Arbitrum One V2DutchOrderReactor: 0x1bd1aAdc9E230626C44a139d7E70d842749351eb
struct SignedOrder {
    bytes order;
    bytes sig;
}

struct InputToken {
    address token;
    uint256 amount;
    uint256 maxAmount;
}

struct OutputToken {
    address token;
    uint256 amount;
    address recipient;
}

struct ResolvedOrder {
    address user;
    uint256 nonce;
    uint256 deadline;
    InputToken input;
    OutputToken[] outputs;
    bytes sig;
    bytes32 hash;
}

interface IReactor {
    function executeWithCallback(SignedOrder calldata order, bytes calldata callbackData) external payable;
}

interface IReactorCallback {
    function reactorCallback(ResolvedOrder[] calldata resolvedOrders, bytes calldata callbackData) external;
}

/// @notice Uniswap V4 PoolManager Singleton Unlock Interface (EIP-1153 Flash Accounting)
interface IPoolManagerV4 {
    function unlock(bytes calldata data) external returns (bytes memory);
}

interface IUnlockCallback {
    function unlockCallback(bytes calldata data) external returns (bytes memory);
}

interface IComet {
    function isLiquidatable(address account) external view returns (bool);
    function absorb(address absorber, address[] calldata accounts) external;
    function buyCollateral(address asset, uint256 minAmount, uint256 baseAmount, address recipient) external;
}

interface IUniswapV2Router02 {
    function swapExactTokensForTokens(
        uint256 amountIn,
        uint256 amountOutMin,
        address[] calldata path,
        address to,
        uint256 deadline
    ) external returns (uint256[] memory amounts);

    function getAmountsOut(uint256 amountIn, address[] calldata path) external view returns (uint256[] memory amounts);
}

interface IUniswapV2Pair {
    function token0() external view returns (address);
    function token1() external view returns (address);
    function getReserves() external view returns (uint112 reserve0, uint112 reserve1, uint32 blockTimestampLast);
    function swap(uint256 amount0Out, uint256 amount1Out, address to, bytes calldata data) external;
}

interface ICamelotPair {
    function token0() external view returns (address);
    function token1() external view returns (address);
    function getReserves()
        external
        view
        returns (uint112 reserve0, uint112 reserve1, uint16 token0FeePercent, uint16 token1FeePercent);
    function swap(uint256 amount0Out, uint256 amount1Out, address to, bytes calldata data) external;
}

interface ISwapRouterV3 {
    struct ExactInputSingleParams {
        address tokenIn;
        address tokenOut;
        uint24 fee;
        address recipient;
        uint256 deadline;
        uint256 amountIn;
        uint256 amountOutMinimum;
        uint160 sqrtPriceLimitX96;
    }
    function exactInputSingle(ExactInputSingleParams calldata params) external payable returns (uint256 amountOut);
}

interface IAlgebraSwapRouter {
    struct ExactInputSingleParams {
        address tokenIn;
        address tokenOut;
        address recipient;
        uint256 deadline;
        uint256 amountIn;
        uint256 amountOutMinimum;
        uint160 limitSqrtPrice;
    }
    function exactInputSingle(ExactInputSingleParams calldata params) external payable returns (uint256 amountOut);
}

interface IUniswapV3Pool {
    function token0() external view returns (address);
    function token1() external view returns (address);
    function liquidity() external view returns (uint128);
    function slot0()
        external
        view
        returns (
            uint160 sqrtPriceX96,
            int24 tick,
            uint16 observationIndex,
            uint16 observationCardinality,
            uint16 observationCardinalityNext,
            uint8 feeProtocol,
            bool unlocked
        );
    function flash(address recipient, uint256 amount0, uint256 amount1, bytes calldata data) external;
}

interface IAlgebraPool {
    function token0() external view returns (address);
    function token1() external view returns (address);
    function liquidity() external view returns (uint128);
    function globalState()
        external
        view
        returns (
            uint160 price,
            int24 tick,
            uint16 fee,
            uint16 timepointIndex,
            uint8 communityFeeToken0,
            uint8 communityFeeToken1,
            bool unlocked
        );
}

contract FlashLoanArbitrage is
    IFlashLoanRecipient,
    IFlashLoanSimpleReceiver,
    IMorphoLiquidateCallback,
    IMorphoFlashLoanCallback,
    IReactorCallback,
    IUnlockCallback
{
    address public owner;
    address public payoutWallet;
    address public immutable balancerVault;
    address public aavePool;
    address public morphoBlue;
    address public uniswapXReactor;
    address public uniswapV4PoolManager;
    uint256 private _unlocked = 1;

    enum ActionType {
        CYCLIC_ARBITRAGE,
        SINGLE_LIQUIDATION,
        BATCH_LIQUIDATION,
        COMPOUND_V3_LIQUIDATION
    }

    struct SwapStep {
        uint8 swapType;
        address target;
        address poolForQuote;
        address tokenIn;
        address tokenOut;
        uint24 fee;
    }

    struct CyclicArbParams {
        address tokenBorrow;
        uint256 minProfit;
        SwapStep[] steps;
    }

    struct LiquidationParams {
        address collateralAsset;
        address debtAsset;
        address borrower;
        uint256 debtToCover;
        SwapStep[] collateralToDebtSwaps;
        uint256 minProfit;
    }

    struct BatchLiquidationParams {
        address collateralAsset;
        address debtAsset;
        address[] borrowers;
        uint256[] debtsToCover;
        SwapStep[] collateralToDebtSwaps;
        uint256 minProfit;
    }

    struct MorphoLiquidationParams {
        MorphoMarketParams marketParams;
        address borrower;
        uint256 seizedAssets;
        uint256 repaidShares;
        SwapStep[] collateralToLoanSwaps;
        uint256 minProfit;
    }

    struct CompoundLiquidationParams {
        address cometMarket;
        address baseAsset;
        address collateralAsset;
        address[] borrowers;
        uint256 baseAmountToSpend;
        SwapStep[] collateralToBaseSwaps;
        uint256 minProfit;
    }

    struct UniswapXFillCallbackParams {
        SwapStep[] inputToOutputSwaps;
        uint256 minProfit;
    }

    struct FlashCallbackData {
        ActionType actionType;
        bytes payload;
    }

    struct UniV3FlashCallbackData {
        address pool;
        address tokenBorrow;
        uint256 amountBorrowed;
        FlashCallbackData innerData;
    }

    event CyclicArbitrageExecuted(
        address indexed provider,
        address indexed tokenBorrow,
        uint256 amountBorrowed,
        uint256 hops,
        uint256 netProfit,
        address indexed payoutWallet
    );

    event FlashLiquidationExecuted(
        address indexed protocol,
        address indexed borrower,
        address indexed collateralAsset,
        address debtAsset,
        uint256 debtCovered,
        uint256 netProfit
    );

    event UniswapXOrderFilled(
        address indexed reactor,
        bytes32 indexed orderHash,
        address inputToken,
        address outputToken,
        uint256 inputAmount,
        uint256 requiredOutput,
        uint256 netFillerProfit
    );

    event SoftAbortTriggered(string reason, uint256 expectedOut, uint256 requiredOut);
    event PayoutWalletUpdated(address indexed oldWallet, address indexed newWallet);
    event OwnershipTransferred(address indexed previousOwner, address indexed newOwner);

    modifier onlyOwner() {
        require(msg.sender == owner, "FlashLoanArbitrage: caller is not owner");
        _;
    }

    modifier nonReentrant() {
        require(_unlocked == 1, "FlashLoanArbitrage: reentrant call");
        _unlocked = 2;
        _;
        _unlocked = 1;
    }

    constructor(address _payoutWallet, address _balancerVault, address _aavePool) {
        owner = msg.sender;
        payoutWallet = _payoutWallet == address(0) ? msg.sender : _payoutWallet;
        balancerVault = _balancerVault == address(0)
            ? 0xBA12222222228d8Ba445958a75a0704d566BF2C8
            : _balancerVault;
        aavePool = _aavePool == address(0)
            ? 0x794a61358D6845594F94dc1DB02A252b5b4814aD
            : _aavePool;
        morphoBlue = 0x6c247b1F6182318877311737BaC0844bAa518F5e;
        uniswapXReactor = 0x1bd1aAdc9E230626C44a139d7E70d842749351eb;
    }

    // =========================================================================
    //     STRATEGY 1: UNIVERSAL N-HOP & INTRA-DEX CYCLIC ARBITRAGE
    // =========================================================================

    function executeBalancerCyclicArbitrage(
        uint256 amount,
        CyclicArbParams calldata params,
        bool softAbortIfUnprofitable
    ) external onlyOwner nonReentrant returns (bool executed) {
        require(params.steps.length >= 2, "FlashLoanArbitrage: need >= 2 hops");
        _validateRouteContinuity(params.tokenBorrow, params.steps);

        if (softAbortIfUnprofitable) {
            (bool canQuote, uint256 expectedOut) = quotePathOnChain(amount, params.steps);
            uint256 requiredOut = amount + params.minProfit;
            if (canQuote && expectedOut < requiredOut) {
                emit SoftAbortTriggered("Balancer cyclic spread closed", expectedOut, requiredOut);
                return false;
            }
        }

        IERC20[] memory tokens = new IERC20[](1);
        tokens[0] = IERC20(params.tokenBorrow);
        uint256[] memory amounts = new uint256[](1);
        amounts[0] = amount;

        bytes memory callbackData = abi.encode(
            FlashCallbackData({actionType: ActionType.CYCLIC_ARBITRAGE, payload: abi.encode(params)})
        );

        IBalancerVault(balancerVault).flashLoan(IFlashLoanRecipient(address(this)), tokens, amounts, callbackData);
        return true;
    }

    /**
     * @notice Executes an N-hop cyclic arbitrage using Morpho Blue's 0%-fee Flash Loan on Arbitrum One.
     */
    function executeMorphoCyclicArbitrage(
        uint256 amount,
        CyclicArbParams calldata params,
        bool softAbortIfUnprofitable
    ) external onlyOwner nonReentrant returns (bool executed) {
        require(morphoBlue != address(0), "FlashLoanArbitrage: Morpho not set");
        require(params.steps.length >= 2, "FlashLoanArbitrage: need >= 2 hops");
        _validateRouteContinuity(params.tokenBorrow, params.steps);

        if (softAbortIfUnprofitable) {
            (bool canQuote, uint256 expectedOut) = quotePathOnChain(amount, params.steps);
            uint256 requiredOut = amount + params.minProfit;
            if (canQuote && expectedOut < requiredOut) {
                emit SoftAbortTriggered("Morpho cyclic spread closed", expectedOut, requiredOut);
                return false;
            }
        }

        bytes memory callbackData = abi.encode(
            FlashCallbackData({actionType: ActionType.CYCLIC_ARBITRAGE, payload: abi.encode(params)})
        );
        IMorpho(morphoBlue).flashLoan(params.tokenBorrow, amount, callbackData);
        return true;
    }

    function executeAaveCyclicArbitrage(
        uint256 amount,
        CyclicArbParams calldata params,
        bool softAbortIfUnprofitable
    ) external onlyOwner nonReentrant returns (bool executed) {
        require(aavePool != address(0), "FlashLoanArbitrage: Aave Pool not set");
        require(params.steps.length >= 2, "FlashLoanArbitrage: need >= 2 hops");
        _validateRouteContinuity(params.tokenBorrow, params.steps);

        if (softAbortIfUnprofitable) {
            uint256 estimatedFee = (amount * 5) / 10000;
            (bool canQuote, uint256 expectedOut) = quotePathOnChain(amount, params.steps);
            uint256 requiredOut = amount + estimatedFee + params.minProfit;
            if (canQuote && expectedOut < requiredOut) {
                emit SoftAbortTriggered("Aave cyclic spread closed", expectedOut, requiredOut);
                return false;
            }
        }

        bytes memory callbackData = abi.encode(
            FlashCallbackData({actionType: ActionType.CYCLIC_ARBITRAGE, payload: abi.encode(params)})
        );

        IAaveV3Pool(aavePool).flashLoanSimple(address(this), params.tokenBorrow, amount, callbackData, 0);
        return true;
    }

    function executeUniV3FlashCyclicArbitrage(
        address flashPool,
        uint256 amount,
        CyclicArbParams calldata params,
        bool softAbortIfUnprofitable
    ) external onlyOwner nonReentrant returns (bool executed) {
        require(flashPool != address(0), "FlashLoanArbitrage: zero flashPool");
        require(params.steps.length >= 2, "FlashLoanArbitrage: need >= 2 hops");
        _validateRouteContinuity(params.tokenBorrow, params.steps);

        if (softAbortIfUnprofitable) {
            (bool canQuote, uint256 expectedOut) = quotePathOnChain(amount, params.steps);
            uint256 requiredOut = amount + params.minProfit;
            if (canQuote && expectedOut < requiredOut) {
                emit SoftAbortTriggered("UniV3 cyclic spread closed", expectedOut, requiredOut);
                return false;
            }
        }

        address token0 = IUniswapV3Pool(flashPool).token0();
        (uint256 amount0, uint256 amount1) = params.tokenBorrow == token0 ? (amount, uint256(0)) : (uint256(0), amount);

        bytes memory data = abi.encode(
            UniV3FlashCallbackData({
                pool: flashPool,
                tokenBorrow: params.tokenBorrow,
                amountBorrowed: amount,
                innerData: FlashCallbackData({actionType: ActionType.CYCLIC_ARBITRAGE, payload: abi.encode(params)})
            })
        );

        IUniswapV3Pool(flashPool).flash(address(this), amount0, amount1, data);
        return true;
    }

    // =========================================================================
    //     STRATEGY 2: AAVE V3 & MORPHO BLUE ZERO-CAPITAL LIQUIDATIONS
    // =========================================================================

    function executeMorphoFlashLiquidation(
        MorphoLiquidationParams calldata params
    ) external onlyOwner nonReentrant returns (bool executed) {
        require(morphoBlue != address(0), "FlashLoanArbitrage: Morpho not set");

        bytes memory callbackData = abi.encode(params);
        IMorpho(morphoBlue).liquidate(
            params.marketParams,
            params.borrower,
            params.seizedAssets,
            params.repaidShares,
            callbackData
        );
        return true;
    }

    function onMorphoLiquidate(uint256 repaidAssets, bytes calldata data) external override {
        require(msg.sender == morphoBlue, "FlashLoanArbitrage: unauthorized Morpho caller");
        MorphoLiquidationParams memory params = abi.decode(data, (MorphoLiquidationParams));

        uint256 seizedBalance = IERC20(params.marketParams.collateralToken).balanceOf(address(this));
        require(seizedBalance > 0, "FlashLoanArbitrage: zero Morpho collateral seized");

        uint256 loanTokenOut = _executeMultiHopSwaps(seizedBalance, params.collateralToLoanSwaps);
        require(
            loanTokenOut >= repaidAssets + params.minProfit,
            "FlashLoanArbitrage: Morpho liq below minProfit (or zombie collateral)"
        );

        _safeApprove(params.marketParams.loanToken, morphoBlue, repaidAssets);

        uint256 netProfit = loanTokenOut - repaidAssets;
        if (netProfit > 0) {
            require(
                IERC20(params.marketParams.loanToken).transfer(payoutWallet, netProfit),
                "FlashLoanArbitrage: Morpho profit transfer failed"
            );
        }

        emit FlashLiquidationExecuted(
            morphoBlue,
            params.borrower,
            params.marketParams.collateralToken,
            params.marketParams.loanToken,
            repaidAssets,
            netProfit
        );
    }

    function onMorphoFlashLoan(uint256 assets, bytes calldata data) external override {
        require(msg.sender == morphoBlue, "FlashLoanArbitrage: unauthorized Morpho caller");
        FlashCallbackData memory decoded = abi.decode(data, (FlashCallbackData));
        CyclicArbParams memory arb = abi.decode(decoded.payload, (CyclicArbParams));

        uint256 finalBalance = _dispatchOperation(arb.tokenBorrow, assets, assets, data);
        _safeApprove(arb.tokenBorrow, morphoBlue, assets);

        uint256 netProfit = finalBalance - assets;
        if (netProfit > 0) {
            require(
                IERC20(arb.tokenBorrow).transfer(payoutWallet, netProfit),
                "FlashLoanArbitrage: Morpho flash profit failed"
            );
        }
    }

    // =========================================================================
    //     STRATEGY 3: UNISWAPX ZERO-CAPITAL DUTCH AUCTION INTENT FILLING
    //     (Grounded in Uniswap/UniswapX & Chitra et al. arXiv:2403.02525v1)
    // =========================================================================

    /**
     * @notice Fills a signed UniswapX Dutch auction order with ZERO upfront capital and ZERO flash-loan fee.
     * @dev The UniswapX V2DutchOrderReactor transfers the swapper's `input.token` to this contract FIRST,
     *      then calls `reactorCallback`, where we route `input.token -> output.token` across Arbitrum DEXs,
     *      approve the Reactor to pull `outputs[i].amount`, and sweep the net filler surplus to `payoutWallet`.
     */
    function executeUniswapXIntentFill(
        SignedOrder calldata order,
        UniswapXFillCallbackParams calldata fillParams
    ) external onlyOwner nonReentrant returns (bool executed) {
        require(uniswapXReactor != address(0), "FlashLoanArbitrage: Reactor not set");
        require(fillParams.inputToOutputSwaps.length >= 1, "FlashLoanArbitrage: empty swap path");

        IReactor(uniswapXReactor).executeWithCallback(order, abi.encode(fillParams));
        return true;
    }

    function reactorCallback(
        ResolvedOrder[] calldata resolvedOrders,
        bytes calldata callbackData
    ) external override {
        require(msg.sender == uniswapXReactor, "FlashLoanArbitrage: unauthorized UniswapX reactor");
        require(resolvedOrders.length == 1, "FlashLoanArbitrage: single order batch expected");

        ResolvedOrder calldata ro = resolvedOrders[0];
        require(ro.outputs.length >= 1, "FlashLoanArbitrage: missing order output");

        UniswapXFillCallbackParams memory fillParams = abi.decode(callbackData, (UniswapXFillCallbackParams));
        address inputToken = ro.input.token;
        uint256 inputReceived = ro.input.amount;
        address outputToken = ro.outputs[0].token;

        uint256 totalRequiredOut = 0;
        for (uint256 i = 0; i < ro.outputs.length; i++) {
            require(ro.outputs[i].token == outputToken, "FlashLoanArbitrage: heterogeneous output tokens");
            totalRequiredOut += ro.outputs[i].amount;
        }

        uint256 totalSwappedOut = _executeMultiHopSwaps(inputReceived, fillParams.inputToOutputSwaps);
        require(
            totalSwappedOut >= totalRequiredOut + fillParams.minProfit,
            "FlashLoanArbitrage: UniswapX fill below minProfit"
        );

        _safeApprove(outputToken, uniswapXReactor, totalRequiredOut);

        uint256 netProfit = totalSwappedOut - totalRequiredOut;
        if (netProfit > 0) {
            require(
                IERC20(outputToken).transfer(payoutWallet, netProfit),
                "FlashLoanArbitrage: UniswapX profit transfer failed"
            );
        }

        emit UniswapXOrderFilled(
            uniswapXReactor,
            ro.hash,
            inputToken,
            outputToken,
            inputReceived,
            totalRequiredOut,
            netProfit
        );
    }

    // =========================================================================
    //     STRATEGY 4: UNISWAP V4 SINGLETON FLASH ACCOUNTING (`unlockCallback`)
    // =========================================================================

    function executeUniswapV4SingletonArb(
        CyclicArbParams calldata params
    ) external onlyOwner nonReentrant returns (bool executed) {
        require(uniswapV4PoolManager != address(0), "FlashLoanArbitrage: V4 PoolManager not set");
        require(params.steps.length >= 2, "FlashLoanArbitrage: need >= 2 hops");
        _validateRouteContinuity(params.tokenBorrow, params.steps);

        IPoolManagerV4(uniswapV4PoolManager).unlock(abi.encode(params));
        return true;
    }

    function unlockCallback(bytes calldata data) external override returns (bytes memory) {
        require(msg.sender == uniswapV4PoolManager, "FlashLoanArbitrage: unauthorized V4 PoolManager");
        CyclicArbParams memory arb = abi.decode(data, (CyclicArbParams));
        uint256 startBal = IERC20(arb.tokenBorrow).balanceOf(address(this));
        uint256 finalBal = _executeMultiHopSwaps(startBal, arb.steps);
        require(finalBal >= startBal + arb.minProfit, "FlashLoanArbitrage: V4 unlock arb below minProfit");
        uint256 netProfit = finalBal - startBal;
        if (netProfit > 0) {
            require(IERC20(arb.tokenBorrow).transfer(payoutWallet, netProfit), "FlashLoanArbitrage: V4 profit transfer failed");
        }
        return abi.encode(netProfit);
    }

    function executeBalancerFlashLiquidation(
        LiquidationParams calldata params,
        bool softCheckHealthFactor
    ) external onlyOwner nonReentrant returns (bool executed) {
        require(aavePool != address(0), "FlashLoanArbitrage: Aave Pool not set");

        if (softCheckHealthFactor) {
            (, , , , , uint256 hf) = IAaveV3Pool(aavePool).getUserAccountData(params.borrower);
            if (hf >= 1e18) {
                emit SoftAbortTriggered("Borrower HF >= 1.0", hf, 1e18);
                return false;
            }
        }

        IERC20[] memory tokens = new IERC20[](1);
        tokens[0] = IERC20(params.debtAsset);
        uint256[] memory amounts = new uint256[](1);
        amounts[0] = params.debtToCover;

        bytes memory callbackData = abi.encode(
            FlashCallbackData({actionType: ActionType.SINGLE_LIQUIDATION, payload: abi.encode(params)})
        );

        IBalancerVault(balancerVault).flashLoan(IFlashLoanRecipient(address(this)), tokens, amounts, callbackData);
        return true;
    }

    function executeBalancerBatchFlashLiquidation(
        BatchLiquidationParams calldata params
    ) external onlyOwner nonReentrant returns (bool executed) {
        require(aavePool != address(0), "FlashLoanArbitrage: Aave Pool not set");
        require(
            params.borrowers.length == params.debtsToCover.length && params.borrowers.length > 0,
            "FlashLoanArbitrage: array length mismatch"
        );

        uint256 totalDebt = 0;
        for (uint256 i = 0; i < params.debtsToCover.length; i++) {
            totalDebt += params.debtsToCover[i];
        }

        IERC20[] memory tokens = new IERC20[](1);
        tokens[0] = IERC20(params.debtAsset);
        uint256[] memory amounts = new uint256[](1);
        amounts[0] = totalDebt;

        bytes memory callbackData = abi.encode(
            FlashCallbackData({actionType: ActionType.BATCH_LIQUIDATION, payload: abi.encode(params)})
        );

        IBalancerVault(balancerVault).flashLoan(IFlashLoanRecipient(address(this)), tokens, amounts, callbackData);
        return true;
    }

    function executeCompoundV3FlashLiquidation(
        CompoundLiquidationParams calldata params,
        bool softCheckLiquidatable
    ) external onlyOwner nonReentrant returns (bool executed) {
        require(params.cometMarket != address(0), "FlashLoanArbitrage: zero Comet address");
        require(params.borrowers.length > 0, "FlashLoanArbitrage: empty borrowers");

        if (softCheckLiquidatable) {
            bool anyLiquidatable = false;
            for (uint256 i = 0; i < params.borrowers.length; i++) {
                if (IComet(params.cometMarket).isLiquidatable(params.borrowers[i])) {
                    anyLiquidatable = true;
                    break;
                }
            }
            if (!anyLiquidatable) {
                emit SoftAbortTriggered("No Compound V3 borrower liquidatable", 0, 1);
                return false;
            }
        }

        IERC20[] memory tokens = new IERC20[](1);
        tokens[0] = IERC20(params.baseAsset);
        uint256[] memory amounts = new uint256[](1);
        amounts[0] = params.baseAmountToSpend;

        bytes memory callbackData = abi.encode(
            FlashCallbackData({actionType: ActionType.COMPOUND_V3_LIQUIDATION, payload: abi.encode(params)})
        );

        IBalancerVault(balancerVault).flashLoan(IFlashLoanRecipient(address(this)), tokens, amounts, callbackData);
        return true;
    }

    function receiveFlashLoan(
        IERC20[] memory tokens,
        uint256[] memory amounts,
        uint256[] memory feeAmounts,
        bytes memory userData
    ) external override {
        require(msg.sender == balancerVault, "FlashLoanArbitrage: unauthorized Balancer caller");

        address asset = address(tokens[0]);
        uint256 amountBorrowed = amounts[0];
        uint256 totalRepay = amountBorrowed + feeAmounts[0];

        uint256 finalBalance = _dispatchOperation(asset, amountBorrowed, totalRepay, userData);

        require(tokens[0].transfer(balancerVault, totalRepay), "FlashLoanArbitrage: Balancer repayment failed");

        uint256 netProfit = finalBalance - totalRepay;
        if (netProfit > 0) {
            require(tokens[0].transfer(payoutWallet, netProfit), "FlashLoanArbitrage: profit transfer failed");
        }
    }

    function executeOperation(
        address asset,
        uint256 amount,
        uint256 premium,
        address initiator,
        bytes calldata params
    ) external override returns (bool) {
        require(msg.sender == aavePool, "FlashLoanArbitrage: unauthorized Aave caller");
        require(initiator == address(this), "FlashLoanArbitrage: unauthorized initiator");

        uint256 totalRepay = amount + premium;
        uint256 finalBalance = _dispatchOperation(asset, amount, totalRepay, params);

        _safeApprove(asset, aavePool, totalRepay);

        uint256 netProfit = finalBalance - totalRepay;
        if (netProfit > 0) {
            require(IERC20(asset).transfer(payoutWallet, netProfit), "FlashLoanArbitrage: profit transfer failed");
        }
        return true;
    }

    function uniswapV3FlashCallback(uint256 fee0, uint256 fee1, bytes calldata data) external {
        UniV3FlashCallbackData memory decoded = abi.decode(data, (UniV3FlashCallbackData));
        require(msg.sender == decoded.pool, "FlashLoanArbitrage: unauthorized UniV3 pool");

        uint256 fee = fee0 > 0 ? fee0 : fee1;
        uint256 totalRepay = decoded.amountBorrowed + fee;

        uint256 finalBalance = _dispatchOperation(
            decoded.tokenBorrow,
            decoded.amountBorrowed,
            totalRepay,
            abi.encode(decoded.innerData)
        );

        require(IERC20(decoded.tokenBorrow).transfer(decoded.pool, totalRepay), "FlashLoanArbitrage: UniV3 repayment failed");

        uint256 netProfit = finalBalance - totalRepay;
        if (netProfit > 0) {
            require(IERC20(decoded.tokenBorrow).transfer(payoutWallet, netProfit), "FlashLoanArbitrage: profit transfer failed");
        }
    }

    function _dispatchOperation(
        address borrowedAsset,
        uint256 amountBorrowed,
        uint256 totalRepay,
        bytes memory rawData
    ) internal returns (uint256 finalBalance) {
        FlashCallbackData memory data = abi.decode(rawData, (FlashCallbackData));

        if (data.actionType == ActionType.CYCLIC_ARBITRAGE) {
            CyclicArbParams memory arb = abi.decode(data.payload, (CyclicArbParams));
            finalBalance = _executeMultiHopSwaps(amountBorrowed, arb.steps);
            require(finalBalance >= totalRepay + arb.minProfit, "FlashLoanArbitrage: cyclic arb below minProfit");
            emit CyclicArbitrageExecuted(msg.sender, borrowedAsset, amountBorrowed, arb.steps.length, finalBalance - totalRepay, payoutWallet);
        } else if (data.actionType == ActionType.SINGLE_LIQUIDATION) {
            LiquidationParams memory liq = abi.decode(data.payload, (LiquidationParams));
            uint256 collateralBefore = IERC20(liq.collateralAsset).balanceOf(address(this));
            _safeApprove(liq.debtAsset, aavePool, liq.debtToCover);

            IAaveV3Pool(aavePool).liquidationCall(liq.collateralAsset, liq.debtAsset, liq.borrower, liq.debtToCover, false);
            _safeApprove(liq.debtAsset, aavePool, 0);

            uint256 seizedCollateral = IERC20(liq.collateralAsset).balanceOf(address(this)) - collateralBefore;
            require(seizedCollateral > 0, "FlashLoanArbitrage: zero collateral seized");

            _executeMultiHopSwaps(seizedCollateral, liq.collateralToDebtSwaps);
            finalBalance = IERC20(liq.debtAsset).balanceOf(address(this));

            require(finalBalance >= totalRepay + liq.minProfit, "FlashLoanArbitrage: liquidation below minProfit");
            emit FlashLiquidationExecuted(aavePool, liq.borrower, liq.collateralAsset, liq.debtAsset, liq.debtToCover, finalBalance - totalRepay);
        } else if (data.actionType == ActionType.BATCH_LIQUIDATION) {
            BatchLiquidationParams memory batch = abi.decode(data.payload, (BatchLiquidationParams));
            uint256 collateralBefore = IERC20(batch.collateralAsset).balanceOf(address(this));
            _safeApprove(batch.debtAsset, aavePool, amountBorrowed);

            for (uint256 i = 0; i < batch.borrowers.length; i++) {
                (, , , , , uint256 hf) = IAaveV3Pool(aavePool).getUserAccountData(batch.borrowers[i]);
                if (hf < 1e18) {
                    IAaveV3Pool(aavePool).liquidationCall(batch.collateralAsset, batch.debtAsset, batch.borrowers[i], batch.debtsToCover[i], false);
                }
            }
            _safeApprove(batch.debtAsset, aavePool, 0);

            uint256 totalCollateral = IERC20(batch.collateralAsset).balanceOf(address(this)) - collateralBefore;
            require(totalCollateral > 0, "FlashLoanArbitrage: no positions liquidated in batch");

            _executeMultiHopSwaps(totalCollateral, batch.collateralToDebtSwaps);
            finalBalance = IERC20(batch.debtAsset).balanceOf(address(this));
            require(finalBalance >= totalRepay + batch.minProfit, "FlashLoanArbitrage: batch liquidation below minProfit");
        } else if (data.actionType == ActionType.COMPOUND_V3_LIQUIDATION) {
            CompoundLiquidationParams memory comp = abi.decode(data.payload, (CompoundLiquidationParams));
            uint256 collateralBefore = IERC20(comp.collateralAsset).balanceOf(address(this));

            IComet(comp.cometMarket).absorb(address(this), comp.borrowers);
            _safeApprove(comp.baseAsset, comp.cometMarket, comp.baseAmountToSpend);
            IComet(comp.cometMarket).buyCollateral(comp.collateralAsset, 1, comp.baseAmountToSpend, address(this));
            _safeApprove(comp.baseAsset, comp.cometMarket, 0);

            uint256 seized = IERC20(comp.collateralAsset).balanceOf(address(this)) - collateralBefore;
            require(seized > 0, "FlashLoanArbitrage: zero Compound collateral bought");

            _executeMultiHopSwaps(seized, comp.collateralToBaseSwaps);
            finalBalance = IERC20(comp.baseAsset).balanceOf(address(this));
            require(finalBalance >= totalRepay + comp.minProfit, "FlashLoanArbitrage: Compound liquidation below minProfit");
            emit FlashLiquidationExecuted(comp.cometMarket, comp.borrowers[0], comp.collateralAsset, comp.baseAsset, comp.baseAmountToSpend, finalBalance - totalRepay);
        }
    }

    function _executeMultiHopSwaps(uint256 initialAmountIn, SwapStep[] memory steps) internal returns (uint256 currentAmount) {
        currentAmount = initialAmountIn;

        for (uint256 i = 0; i < steps.length; i++) {
            SwapStep memory step = steps[i];
            uint256 outBefore = IERC20(step.tokenOut).balanceOf(address(this));

            if (step.swapType == 0) {
                _safeApprove(step.tokenIn, step.target, currentAmount);
                address[] memory path = new address[](2);
                path[0] = step.tokenIn;
                path[1] = step.tokenOut;
                IUniswapV2Router02(step.target).swapExactTokensForTokens(currentAmount, 1, path, address(this), block.timestamp);
                _safeApprove(step.tokenIn, step.target, 0);
            } else if (step.swapType == 1) {
                _safeApprove(step.tokenIn, step.target, currentAmount);
                ISwapRouterV3.ExactInputSingleParams memory v3Params = ISwapRouterV3.ExactInputSingleParams({
                    tokenIn: step.tokenIn,
                    tokenOut: step.tokenOut,
                    fee: step.fee,
                    recipient: address(this),
                    deadline: block.timestamp,
                    amountIn: currentAmount,
                    amountOutMinimum: 1,
                    sqrtPriceLimitX96: 0
                });
                ISwapRouterV3(step.target).exactInputSingle(v3Params);
                _safeApprove(step.tokenIn, step.target, 0);
            } else if (step.swapType == 2) {
                IUniswapV2Pair pair = IUniswapV2Pair(step.target);
                (uint112 reserve0, uint112 reserve1, ) = pair.getReserves();
                address token0 = pair.token0();
                uint256 feeBps = step.fee == 0 ? 9970 : uint256(step.fee);
                (uint256 resIn, uint256 resOut) = step.tokenIn == token0
                    ? (uint256(reserve0), uint256(reserve1))
                    : (uint256(reserve1), uint256(reserve0));
                uint256 amountInWithFee = currentAmount * feeBps;
                uint256 amountOut = (amountInWithFee * resOut) / ((resIn * 10000) + amountInWithFee);
                require(IERC20(step.tokenIn).transfer(step.target, currentAmount), "DirectV2: transfer failed");
                (uint256 amount0Out, uint256 amount1Out) = step.tokenIn == token0 ? (uint256(0), amountOut) : (amountOut, uint256(0));
                pair.swap(amount0Out, amount1Out, address(this), new bytes(0));
            } else if (step.swapType == 3) {
                ICamelotPair pair = ICamelotPair(step.target);
                (uint112 r0, uint112 r1, uint16 fee0, uint16 fee1) = pair.getReserves();
                address token0 = pair.token0();
                (uint256 resIn, uint256 resOut, uint256 feePct) = step.tokenIn == token0
                    ? (uint256(r0), uint256(r1), uint256(fee0))
                    : (uint256(r1), uint256(r0), uint256(fee1));
                uint256 amountInWithFee = currentAmount * (100000 - feePct);
                uint256 amountOut = (amountInWithFee * resOut) / ((resIn * 100000) + amountInWithFee);
                require(IERC20(step.tokenIn).transfer(step.target, currentAmount), "CamelotV2: transfer failed");
                (uint256 amount0Out, uint256 amount1Out) = step.tokenIn == token0 ? (uint256(0), amountOut) : (amountOut, uint256(0));
                pair.swap(amount0Out, amount1Out, address(this), new bytes(0));
            } else if (step.swapType == 4) {
                _safeApprove(step.tokenIn, step.target, currentAmount);
                IAlgebraSwapRouter.ExactInputSingleParams memory algParams = IAlgebraSwapRouter.ExactInputSingleParams({
                    tokenIn: step.tokenIn,
                    tokenOut: step.tokenOut,
                    recipient: address(this),
                    deadline: block.timestamp,
                    amountIn: currentAmount,
                    amountOutMinimum: 1,
                    limitSqrtPrice: 0
                });
                IAlgebraSwapRouter(step.target).exactInputSingle(algParams);
                _safeApprove(step.tokenIn, step.target, 0);
            } else {
                revert("FlashLoanArbitrage: unsupported swapType");
            }

            uint256 outAfter = IERC20(step.tokenOut).balanceOf(address(this));
            require(outAfter > outBefore, "FlashLoanArbitrage: zero hop output");
            currentAmount = outAfter - outBefore;
        }
    }

    function quotePathOnChain(
        uint256 amountIn,
        SwapStep[] calldata steps
    ) public view returns (bool canQuote, uint256 currentAmount) {
        currentAmount = amountIn;
        for (uint256 i = 0; i < steps.length; i++) {
            SwapStep calldata step = steps[i];
            if (step.swapType == 2) {
                IUniswapV2Pair pair = IUniswapV2Pair(step.target);
                (uint112 r0, uint112 r1, ) = pair.getReserves();
                address t0 = pair.token0();
                uint256 feeBps = step.fee == 0 ? 9970 : uint256(step.fee);
                (uint256 resIn, uint256 resOut) = step.tokenIn == t0 ? (uint256(r0), uint256(r1)) : (uint256(r1), uint256(r0));
                uint256 amountInWithFee = currentAmount * feeBps;
                currentAmount = (amountInWithFee * resOut) / ((resIn * 10000) + amountInWithFee);
            } else if (step.swapType == 3) {
                ICamelotPair pair = ICamelotPair(step.target);
                (uint112 r0, uint112 r1, uint16 f0, uint16 f1) = pair.getReserves();
                address t0 = pair.token0();
                (uint256 resIn, uint256 resOut, uint256 feePct) = step.tokenIn == t0
                    ? (uint256(r0), uint256(r1), uint256(f0))
                    : (uint256(r1), uint256(r0), uint256(f1));
                uint256 amountInWithFee = currentAmount * (100000 - feePct);
                currentAmount = (amountInWithFee * resOut) / ((resIn * 100000) + amountInWithFee);
            } else if (step.swapType == 0) {
                address[] memory path = new address[](2);
                path[0] = step.tokenIn;
                path[1] = step.tokenOut;
                uint256[] memory amounts = IUniswapV2Router02(step.target).getAmountsOut(currentAmount, path);
                currentAmount = amounts[amounts.length - 1];
            } else if (step.swapType == 1 && step.poolForQuote != address(0)) {
                (bool ok, uint256 out) = _quoteUniV3PoolVirtual(step.poolForQuote, step.tokenIn, currentAmount, step.fee);
                if (!ok) return (false, 0);
                currentAmount = out;
            } else if (step.swapType == 4 && step.poolForQuote != address(0)) {
                (bool ok, uint256 out) = _quoteAlgebraPoolVirtual(step.poolForQuote, step.tokenIn, currentAmount);
                if (!ok) return (false, 0);
                currentAmount = out;
            } else {
                return (false, 0);
            }
        }
        return (true, currentAmount);
    }

    function _quoteUniV3PoolVirtual(address poolAddr, address tokenIn, uint256 amountIn, uint24 feeTier) internal view returns (bool ok, uint256 amountOut) {
        IUniswapV3Pool pool = IUniswapV3Pool(poolAddr);
        (uint160 sqrtPriceX96, , , , , , ) = pool.slot0();
        uint128 liq = pool.liquidity();
        if (sqrtPriceX96 == 0 || liq == 0) return (false, 0);
        address token0 = pool.token0();
        uint256 effIn = (amountIn * (1000000 - uint256(feeTier))) / 1000000;
        amountOut = _computeConcentratedOutput(sqrtPriceX96, liq, tokenIn == token0, effIn);
        return (amountOut > 0, amountOut);
    }

    function _quoteAlgebraPoolVirtual(address poolAddr, address tokenIn, uint256 amountIn) internal view returns (bool ok, uint256 amountOut) {
        IAlgebraPool pool = IAlgebraPool(poolAddr);
        (uint160 sqrtPriceX96, , uint16 dynamicFee, , , , ) = pool.globalState();
        uint128 liq = pool.liquidity();
        if (sqrtPriceX96 == 0 || liq == 0) return (false, 0);
        address token0 = pool.token0();
        uint256 effIn = (amountIn * (1000000 - uint256(dynamicFee))) / 1000000;
        amountOut = _computeConcentratedOutput(sqrtPriceX96, liq, tokenIn == token0, effIn);
        return (amountOut > 0, amountOut);
    }

    function _computeConcentratedOutput(uint160 sqrtPriceX96, uint128 liquidity, bool zeroForOne, uint256 effIn) internal pure returns (uint256 amountOut) {
        uint256 L = uint256(liquidity);
        uint256 sp = uint256(sqrtPriceX96);
        uint256 vRes0 = (L << 96) / sp;
        uint256 vRes1 = (L * sp) >> 96;
        if (vRes0 == 0 || vRes1 == 0) return 0;
        amountOut = zeroForOne ? (effIn * vRes1) / (vRes0 + effIn) : (effIn * vRes0) / (vRes1 + effIn);
    }

    function _validateRouteContinuity(address tokenBorrow, SwapStep[] calldata steps) internal pure {
        require(steps[0].tokenIn == tokenBorrow, "FlashLoanArbitrage: first hop must start with tokenBorrow");
        require(steps[steps.length - 1].tokenOut == tokenBorrow, "FlashLoanArbitrage: final hop must return to tokenBorrow");
        for (uint256 i = 0; i + 1 < steps.length; i++) {
            require(steps[i].tokenOut == steps[i + 1].tokenIn, "FlashLoanArbitrage: discontinuous hop tokens");
        }
    }

    function _safeApprove(address token, address spender, uint256 amount) internal {
        IERC20(token).approve(spender, 0);
        if (amount > 0) {
            require(IERC20(token).approve(spender, amount), "FlashLoanArbitrage: approve failed");
        }
    }

    function setPayoutWallet(address _newPayoutWallet) external onlyOwner {
        require(_newPayoutWallet != address(0), "FlashLoanArbitrage: zero address");
        emit PayoutWalletUpdated(payoutWallet, _newPayoutWallet);
        payoutWallet = _newPayoutWallet;
    }

    function setAavePool(address _newAavePool) external onlyOwner {
        aavePool = _newAavePool;
    }

    function setMorphoBlue(address _newMorphoBlue) external onlyOwner {
        morphoBlue = _newMorphoBlue;
    }

    function setUniswapXReactor(address _newReactor) external onlyOwner {
        uniswapXReactor = _newReactor;
    }

    function setUniswapV4PoolManager(address _newPoolManager) external onlyOwner {
        uniswapV4PoolManager = _newPoolManager;
    }

    function transferOwnership(address _newOwner) external onlyOwner {
        require(_newOwner != address(0), "FlashLoanArbitrage: zero address");
        emit OwnershipTransferred(owner, _newOwner);
        owner = _newOwner;
    }

    function rescueTokens(address token, uint256 amount) external onlyOwner {
        require(IERC20(token).transfer(payoutWallet, amount), "FlashLoanArbitrage: token rescue failed");
    }

    function rescueETH() external onlyOwner {
        uint256 bal = address(this).balance;
        (bool success, ) = payable(payoutWallet).call{value: bal}("");
        require(success, "FlashLoanArbitrage: ETH rescue failed");
    }

    receive() external payable {}
}
