import os
import subprocess
import sys
import time

import psutil
from dotenv import load_dotenv

load_dotenv()

# Ensure the project root is on PYTHONPATH for all child processes
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Force Python to unbuffer stdout/stderr so logs appear instantly in tmux/dashboard
_ENV = {
    **os.environ, 
    "PYTHONPATH": _PROJECT_ROOT,
    "PYTHONUNBUFFERED": "1"
}

RUST_ENGINE_DIR = "execution_engine"
RUST_COMMAND = ["cargo", "run", "--release"]
PYTHON_COMMAND = [sys.executable, "scripts/live_trader_v2.py"]
SCRAPER_COMMAND = [sys.executable, "bongus/strategies/sentiment_scraper.py"]
DASHBOARD_COMMAND = [
    sys.executable, "-m", "uvicorn",
    "bongus.monitoring.web_dashboard:app",
    "--host", "0.0.0.0", "--port", "8080",
]
TELEGRAM_COMMAND = [sys.executable, "bongus/monitoring/telegram_alerter.py"]

MEMORY_LIMIT_MB = 1024  # Example: 1GB memory threshold before we forcibly restart

def start_process(command, cwd=None):
    print(f"Starting process: {' '.join(command)}")
    return subprocess.Popen(command, cwd=cwd, env=_ENV)

def check_and_restart(proc, command, cwd=None, name="Process"):
    # If process died naturally or crashed
    if proc.poll() is not None:
        print(f"[WATCHDOG] {name} crashed or stopped! Restarting...")
        return start_process(command, cwd=cwd)
    
    # Check memory usage
    try:
        p = psutil.Process(proc.pid)
        mem_info = p.memory_info()
        mem_mb = mem_info.rss / (1024 * 1024)
        
        if mem_mb > MEMORY_LIMIT_MB:
            print(f"[WATCHDOG] {name} memory spike detected ({mem_mb:.2f} MB)! Killing and restarting...")
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                print(f"[WATCHDOG] {name} did not terminate in 5s, sending SIGKILL...")
                proc.kill()
                proc.wait()
            return start_process(command, cwd=cwd)
    except psutil.NoSuchProcess:
        pass
    except Exception as e:
        print(f"[WATCHDOG] Error monitoring {name}: {e}")
        
    return proc

def main():
    print("Starting King Watchdog Supervisor...")
    
    rust_proc = start_process(RUST_COMMAND, cwd=RUST_ENGINE_DIR)
    time.sleep(2) # Give Rust engine a moment to start ZMQ server
    
    python_proc = start_process(PYTHON_COMMAND)
    scraper_proc = start_process(SCRAPER_COMMAND)
    dashboard_proc = start_process(DASHBOARD_COMMAND)
    telegram_proc = start_process(TELEGRAM_COMMAND)

    try:
        while True:
            time.sleep(10)
            rust_proc = check_and_restart(rust_proc, RUST_COMMAND, cwd=RUST_ENGINE_DIR, name="Rust Execution Engine")
            python_proc = check_and_restart(python_proc, PYTHON_COMMAND, name="Python Live Trader")
            scraper_proc = check_and_restart(scraper_proc, SCRAPER_COMMAND, name="Sentiment Scraper")
            dashboard_proc = check_and_restart(dashboard_proc, DASHBOARD_COMMAND, name="Web Dashboard")
            telegram_proc = check_and_restart(telegram_proc, TELEGRAM_COMMAND, name="Telegram Alerter")

    except KeyboardInterrupt:
        print("Watchdog shutting down. Terminating child processes...")
        rust_proc.terminate()
        python_proc.terminate()
        scraper_proc.terminate()
        dashboard_proc.terminate()
        telegram_proc.terminate()

if __name__ == "__main__":
    main()
