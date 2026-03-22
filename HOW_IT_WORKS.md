## Bongus Trading Bot: How It Works

### Overview
The Bongus Trading Bot is an automated cryptocurrency trading system designed to operate 24/7, primarily focused on BTCUSDT. It leverages a modular architecture for data ingestion, feature engineering, strategy execution, risk management, and order execution. The bot is optimized for demo accounts with a $10k balance, supports up to 5x leverage, and is built to tolerate significant drawdowns with hard-stop controls for safety.

### Core Components

- **Data Layer**: Downloads, loads, and processes market and sentiment data. Modules include:
  - `data/downloader.py`: Fetches raw data from exchanges/APIs.
  - `data/loader.py`: Loads and preprocesses data for analysis.
  - `data/feature_engineering.py`: Generates features for strategies.

- **Strategy Engine**: Implements trading logic and signal generation.
  - `strategies/strategy.py`: Base class for strategies.
  - `strategies/auto_tweaker.py`: Automated parameter optimization.
  - `strategies/sentiment_scraper.py`: Integrates sentiment data.

- **Execution Engine**: Handles order placement and management.
  - Rust-based engine in `execution_engine/` for low-latency execution.
  - Python IPC modules (`ipc/execution.py`) communicate with the Rust engine.

- **Risk & Cost Management**:
  - `engine/risk_engine.py`: Position sizing, leverage, and stop-loss logic.
  - `engine/cost_model.py`: Models trading fees and slippage.

- **Monitoring & Alerts**:
  - `monitoring/cli_dashboard.py` and `monitoring/web_dashboard.py`: Real-time monitoring.
  - `monitoring/telegram_alerter.py`: Sends alerts to Telegram.
  - `monitoring/king_watchdog.py`: Ensures bot health and uptime.

### Workflow
1. **Data Acquisition**: Market and sentiment data are downloaded and processed.
2. **Feature Engineering**: Data is transformed into features for strategy input.
3. **Signal Generation**: Strategies analyze features and generate trade signals.
4. **Risk Assessment**: Signals are filtered through risk and cost models.
5. **Order Execution**: Validated orders are sent to the Rust execution engine.
6. **Monitoring**: All activity is logged and monitored, with alerts for anomalies.

### Customization
- Strategies, risk parameters, and data sources can be configured in the `config/` directory.
- The bot is designed for extensibility—new strategies or data sources can be added with minimal changes.

### Safety
- Hard-stop controls are implemented to prevent catastrophic losses.
- The bot is intended for demo or controlled environments; use in production at your own risk.

---
For more details, see the codebase and configuration files.