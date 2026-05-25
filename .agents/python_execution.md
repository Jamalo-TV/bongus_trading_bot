# Python Execution Layer Rules & Architecture (`bongus/`)

This document governs the Python execution layer, strategy ranking, portfolio allocation, and state tracking.

## Core Execution Loop (`live_trader.py`)

- **Execution Environment**: The Bongus system must **always** be run within a `tmux` session/window. If changes are made to the codebase that require a system restart, the restart must be executed inside the `tmux` session.
- **Main Component**: `CanonicalMultiSymbolTrader` (instantiated in `scripts/live_trader.py`).
- **Trading Cycle**: Runs sequentially every 15 seconds.
  1. Pulls funding snapshots and merges with real-time book depth cache from Rust.
  2. Applies hard safety filters (missing spot, low depth, wide spread, toxicity).
  3. Ranks candidates via Winsorized-percentile metrics.
  4. Decides allocation using `PortfolioAllocator`.
  5. Records candidate metrics and dispatches intents.

## Polars DataFrames & Signal Generation
- **Crucial Rule**: **Always use Polars, not Pandas** for strategy DataFrames. All ranking, signal processing, and time-series logic must use Polars APIs.

## Portfolio Allocation (`portfolio_allocator.py`)

- **Dual Mode**:
  - **Canonical Mode**: Configured with a configuration dictionary (`cfg`). Sizes slots dynamically using **fractional Kelly volatility sizing** based on account equity and damped by volatility.
  - **Legacy Mode**: Keeps simple flat slot limits (retained for backward compatibility).
- **Exposure limits**: Clamped by:
  - Max gross exposure (max $22,000 USD limit).
  - Max symbol concentration (max 30% default).
  - Cluster constraints (BTCUSDT/ETHUSDT = `MAJORS`, SUI/SOL/APT = `L1`, PEPE/DOGE = `MEME`).
- **Exit-First Invariant**:
  - Rotations and exits are always prioritized and sent first.
  - **Never** place a replacement entry order in the same cycle as an exit. Entry orders are only allowed if no exits were dispatched in the current cycle.

## Python Risk Engine & State Store (`risk_engine.py`, `state_store.py`)

- **Drawdown Limits**:
  - Soft drawdown ($\ge 4\%$) scales position sizes down linearly.
  - Max drawdown ($\ge 10\%$) triggers the de-risking kill-switch.
- **Latency Debouncing**: High venue latency debounces for 30s. If high latency persists, new risk is blocked, but existing positions are not forced shut to avoid executing during toxic periods.
- **Consecutive Loss Gate**: Halt new risk if consecutive losses exceed 5.
- **Asynchronous Persistence**: Database writes (`StateWriter`) must be queued in an `asyncio.Queue` and processed on a background thread. Never execute blocking SQL queries directly on the hot path.
