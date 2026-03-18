# Bot Training and Server Deployment Guide

This guide covers how to train (backtest) the trading bot locally on your PC, followed by recommendations and instructions for deploying the live execution engine to a cloud server.

---

## Part 1: Training and Backtesting on Your PC

Before running the bot with real money, you should train and evaluate it on historical data using your local machine.

### 1. Environment Setup
Make sure your Python virtual environment is activated and dependencies are installed. You can do this in your VS Code terminal (PowerShell):
```powershell
# Activate the virtual environment
.\.venv\Scripts\Activate.ps1

# Install requirements
pip install -r requirements.txt
```

### 2. Generate or Download Data
To run a backtest, the bot needs data. You can either use synthetic sample data or real Binance historical data.

**Using Synthetic Data (Fastest):**
Run the sample data generator to create 90 days of synthetic Spot, Perp, and Funding data inside the `data/` folder:
```powershell
python generate_sample_data.py
```

**Using Real Binance Data:**
1. Go to [Binance Public Data](https://data.binance.vision/).
2. Download 1m Klines for Spot and UM Futures (Perpetual), along with Monthly Funding Rates.
3. Convert the downloaded CSV files into `.parquet` files aligning to a 1-minute timeline and place them in the `data/` directory (name them appropriately as expected by `data_loader.py`).

### 3. Run the Backtest (Training)
The repository contains a `walk_forward.py` module to test your parameters over different time windows, ensuring the strategy doesn't overfit.
```powershell
python walk_forward.py
```
You can also run the main entry point to look at analytics or generate overall results:
```powershell
python main.py
```

*Tip: Adjust the threshold settings in `config.py` (like `ENTRY_PREMIUM_THRESHOLD` and `NOTIONAL_PER_TRADE`) to optimize your bot's behavior during backtesting.*

---

## Part 2: Deploying to a Server

Since your objective is revenue generation via 24/7 runtime, deploying the bot to an external cloud server is required so it operates regardless of whether your local PC is on.

### The Best Hosting Options
Since the bot relies heavily on Binance and low-latency execution (configured with `MAX_VENUE_LATENCY_MS = 400` in `config.py`), server location is critical. Binance's matching engines are hosted in **Tokyo (AWS ap-northeast-1)**.

1. **AWS EC2 (Highly Recommended)**
   * **Why:** If you host your EC2 instance in the **Tokyo** region (ap-northeast-1), your latency to Binance will be under 10ms.
   * **Size:** `t3.micro` or `t3.small` (Free tier eligible, enough to run the Python analytics + Rust engine).
   
2. **DigitalOcean / Vultr / Linode**
   * **Why:** Fixed, cheap monthly pricing (starting at ~$5-$10/month).
   * **Location:** Choose an Asian region (Tokyo or Singapore) to reduce latency to Binance.

### Deployment Steps (Linux Server)

**1. Clone Your Code**
SSH into your server and download your codebase.
```bash
git clone <your-repo-link>
cd bongus_trading_bot
```

**2. Setup Python and Rust**
Since your workspace has a high-performance Rust execution engine (`execution_engine/`), you must compile it:
```bash
# Install Python & Rust
sudo apt update && sudo apt install -y python3-venv cargo

# Compile the Rust Engine
cd execution_engine
cargo build --release
cd ..

# Setup Python Venv
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**3. Configure API Keys Securely**
NEVER put API keys in `config.py`. Store them in bash environment variables or a `.env` file that is read by your bot.
```bash
export BINANCE_API_KEY="your_api_key"
export BINANCE_API_SECRET="your_api_secret"
```

**4. Keep the Bot Running 24/7**
If you run the bot normally and close your SSH terminal, the bot will die. To keep it running in the background, use one of the two best methods:

**Method A: tmux / screen (Easiest)**
```bash
tmux new -s tradingbot
# Inside tmux:
source .venv/bin/activate
python main.py
# Press Ctrl+B, then D to detach. The bot safely runs in the background.
# To check on it later, type `tmux attach -t tradingbot`.
```

**Method B: systemd (Most Professional - auto-restarts on crash)**
Create a service file: `sudo nano /etc/systemd/system/bongus.service`
```ini
[Unit]
Description=Bongus Trading Bot
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/bongus_trading_bot
ExecStart=/home/ubuntu/bongus_trading_bot/.venv/bin/python main.py
Restart=on-failure
Environment="BINANCE_API_KEY=your_key"
Environment="BINANCE_API_SECRET=your_secret"

[Install]
WantedBy=multi-user.target
```
Then start the service:
```bash
sudo systemctl enable bongus
sudo systemctl start bongus
```