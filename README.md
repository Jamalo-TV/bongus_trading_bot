# Bongus Trading Bot

The Bongus Trading Bot is an automated cryptocurrency trading system designed to operate 24/7, primarily focused on BTCUSDT. It leverages a hybrid Python/Rust modular architecture for high-performance data ingestion, feature engineering, strategy execution, risk management, and order execution. 

The bot is optimized for demo accounts (e.g., $10k balance), supports up to 5x leverage, and features built-in hard-stop controls for safety.

## Core Architecture

- **Python Brain**: Handles data fetching, feature engineering, risk management, and strategy signal generation.
- **Rust Execution Engine**: A low-latency order execution engine (`/execution_engine`) that interacts directly with Binance matching engines via WebSockets and REST.

## Prerequisites

Before you begin, ensure you have the following installed on your machine:
- **Python 3.10+**
- **Rust & Cargo** (for compiling the execution engine)
- **Git**

## Installation & Setup

1. **Clone the repository**
   ```bash
   git clone <your-repo-url>
   cd bongus_trading_bot
   ```

2. **Set up the Python Virtual Environment**
   ```bash
   # Create the virtual environment
   python -m venv .venv
   
   # Activate it (Windows)
   .\.venv\Scripts\Activate.ps1
   # Activate it (Linux/Mac)
   source .venv/bin/activate
   
   # Install dependencies
   pip install -r requirements.txt
   ```

3. **Compile the Rust Execution Engine**
   ```bash
   cd execution_engine
   cargo build --release
   cd ..
   ```

4. **Configure Environment Variables**
   Never put your API keys in the source code. Export them to your environment or use a `.env` file:
   ```bash
   export BINANCE_API_KEY="your_api_key_here"
   export BINANCE_API_SECRET="your_api_secret_here"
   ```

## How to Run

### 1. Training & Backtesting (Local)
Before running the bot with real funds, evaluate it on historical data.

- **Generate Sample Data**: If you don't have Binance historical data downloaded, you can generate 90 days of synthetic data to test the pipeline.
  ```bash
  python generate_sample_data.py
  ```
- **Run the Walk-Forward Optimizer**: Test your parameters over different time windows to ensure the strategy avoids overfitting.
  ```bash
  python walk_forward.py
  ```
- **Run a Standard Backtest / Analytics**:
  ```bash
  python main.py
  ```

### 2. Live Deployment (Server)
To run the bot 24/7 for live trading, it is highly recommended to deploy it to an AWS EC2 instance in **Tokyo (ap-northeast-1)** to maintain `<10ms` latency to Binance's matching engine.

1. SSH into your server and repeat the **Installation & Setup** steps.
2. Set your live `BINANCE_API_KEY` and `BINANCE_API_SECRET` securely.
3. Run the bot using a terminal multiplexer like `tmux` or `screen` to keep it alive when you close your SSH session:
   ```bash
   tmux new -s tradingbot
   source .venv/bin/activate
   python main.py
   ```
   *Note: For production resilience, it is recommended to set up a `systemd` service to auto-restart the bot on failures. See `TRAINING_AND_DEPLOYMENT.md` for detailed instructions.*

## Safety & Disclaimer

- **Hard-Stop Controls**: Built-in mechanisms attempt to prevent catastrophic drawdowns.
- **Toxicity & Circuit Breakers**: The Rust execution engine automatically pauses operations during severe spread toxicity or if it loses communication with the Python brain.
- **Disclaimer**: This bot is intended for demo or controlled environments. Use in production with real funds entirely at your own risk.

---
For a deeper dive into the system logic, refer to `HOW_IT_WORKS.md`.