# Rust Execution Engine Rules & Architecture (`execution_engine/`)

This document governs the low-latency execution engine written in Rust, running under a Tokio runtime.

## Core Architecture

- **Runtime**: Tokio asynchronous multi-threaded executor.
- **Channels**: uses `tokio::sync::mpsc` for internal communication (e.g. bridging WS and ZMQ events into `OrderManager`) and `tokio::sync::broadcast` for outbound telemetry.
- **Hot-path allocation**: Minimize allocations. Leverage pre-allocated structures or `std::collections::HashMap` lookup caches where possible.

## Data Ingestion & Connections

- **Public WS Streams**: `WsConnectionManager` (`binance_ws.rs`) reconnects automatically to fetch L2 depth book data and mark price streams.
- **Private WS Streams**: `UserDataWsManager` (`user_data_ws.rs`) tracks order state changes and account balance updates. Skip stream management in `paper` mode.
- **REST Client**: `BinanceRest` (`binance_rest.rs`) executes actions sequentially for placing limit/market orders, fetching exchange metadata, and synchronization.

## Order State Machine (`order_manager.rs`)

`OrderManager` processes inputs via `EngineEvent` variants (`Ws`, `Alpha`, `LeggingTimeout`, `StrategyTick`).

### 1. Dual-Maker Order Entry (`try_place_dual_maker`)
- Employs **passive dual-maker execution** to minimize fees.
- Spitted into spot (limit order) and futures (limit order) legs simultaneously.
- **Price Skewing**: Skews limit prices using **Order Book Imbalance (OBI)**. If $OBI > 0.3$, prices are skewed up by tick/step sizes. If $OBI < -0.3$, prices are skewed down.

### 2. Adaptive Legging Defense
- When the first leg of a dual-maker order fills:
  - Transition state to `ChasePhase::LegFilledWaiting(first_filled_leg)`.
  - Calculate an adaptive legging timeout using `adaptive_legging_timeout_ms` based on short-horizon volatility.
  - Spawn a timer task sleeping for the timeout before pushing `EngineEvent::LeggingTimeout`.
- **Taker Conversion**: If the second leg has not filled within the timeout:
  - Cancel the unfilled resting passive limit order via Binance REST.
  - Submit a **market order** immediately (Legging Defense Taker) to fill the remaining leg and avoid basis risk.

### 3. Single-Leg Unwind Exits
- In exit scenarios requiring single-leg taker unwinds (exits of a naked leg), the order bypasses the maker spread toxicity gates and uses **MARKET** orders directly to ensure immediate closure.

## Risk Management & Circuit Breakers

Circuit breakers are checked in `check_circuit_breakers()` before executing any alpha instructions:

1. **Python Brain Staleness**: If `last_brain_ping.elapsed() > Duration::from_secs(12 * 60)` (12 minutes), halt all new risk.
2. **Gross Exposure Breaker**: If `current_gross_exposure_usd > max_gross_exposure_usd`, block new entries.
3. **Unified Portfolio Margin (PM) Check**:
   - Simulated locally via `UnifiedPortfolioMarginCalculator` (`collateral_engine.rs`).
   - Calculates directional risk ($|Spot Notional - Perp Notional|$), unified account equity (account equity + total unrealized PnL), and the Unified Maintenance Margin Ratio ($uniMMR = \frac{Directional Risk \times 0.004}{Unified Equity}$).
   - If $uniMMR \ge 0.8$ (the danger threshold), trigger a kill-switch and halt.
4. **Spread Toxicity**: Dynamically logs spreads. If `spread_bps` exceeds critical thresholds, the symbol is flagged as toxic, pausing maker operations for that symbol.
