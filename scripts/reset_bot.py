import sqlite3
import os
import sys
from pathlib import Path

# Fix: Use paths relative to the project root
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bongus.core.config_manager import ConfigManager

STATE_DB_PATH = str(PROJECT_ROOT / "state.db")
LIVE_CONFIG_PATH = str(PROJECT_ROOT / "live_config.json")

def reset_bot():
    if not os.path.exists(STATE_DB_PATH):
        print(f"Error: {STATE_DB_PATH} not found.")
        return

    print(f"Connecting to {STATE_DB_PATH}...")
    conn = sqlite3.connect(STATE_DB_PATH)
    cursor = conn.cursor()

    # 1. Clear tables
    tables_to_clear = [
        "trade_history",
        "positions",
        "pending_intents",
        "execution_events",
        "candidate_snapshots",
        "opportunity_scores",
        "feature_snapshots",
        "execution_quality",
        "validation_snapshots",
        "model_shadow_decisions",
        "health_samples",
        "market_samples",
        "ai_report_proposals"
    ]
    
    for table in tables_to_clear:
        try:
            print(f"Clearing table {table}...")
            cursor.execute(f"DELETE FROM {table}")
        except sqlite3.OperationalError as e:
            print(f"Warning: Could not clear table {table}: {e}")

    # 2. Reset portfolio_stats and risk_state
    print("Resetting stats and risk state...")
    
    # Get current exchange equity if available
    current_equity = 10000.0
    try:
        cursor.execute("SELECT value FROM risk_state WHERE key = 'exchange_account_equity'")
        row = cursor.fetchone()
        if row:
            current_equity = float(row[0])
            print(f"Found exchange equity: {current_equity}")
    except Exception as e:
        print(f"Warning: Could not fetch exchange equity: {e}")
    
    # Reset equity values
    try:
        cursor.execute("UPDATE portfolio_stats SET value = ? WHERE key = 'account_equity'", (current_equity,))
        cursor.execute("UPDATE risk_state SET value = ? WHERE key = 'account_equity'", (str(current_equity),))
        cursor.execute("UPDATE risk_state SET value = ? WHERE key = 'account_equity_mark_to_market'", (str(current_equity),))
        cursor.execute("UPDATE risk_state SET value = ? WHERE key = 'account_equity_high_watermark'", (str(current_equity),))
        cursor.execute("UPDATE risk_state SET value = '0' WHERE key = 'loss_streak'")
        cursor.execute("UPDATE risk_state SET value = '0' WHERE key = 'win_streak'")
        cursor.execute("UPDATE risk_state SET value = '0.0' WHERE key = 'mark_to_market_open_pnl_usd'")
        cursor.execute("UPDATE risk_state SET value = '[]' WHERE key = 'stale_pending_enter_symbols'")
        cursor.execute("UPDATE risk_state SET value = '[]' WHERE key = 'stale_pending_exit_symbols'")
        cursor.execute("UPDATE risk_state SET value = '{}' WHERE key = 'cooldown_symbols'")
    except sqlite3.OperationalError as e:
        print(f"Warning: Could not update risk state: {e}")

    conn.commit()
    conn.close()

    # 3. Update live_config.json
    if os.path.exists(LIVE_CONFIG_PATH):
        print(f"Updating {LIVE_CONFIG_PATH}...")
        try:
            ConfigManager(config_path=LIVE_CONFIG_PATH).apply_updates(
                {
                    "account_equity_usd": current_equity,
                    "reset_equity_high_watermark": True,
                }
            )
        except Exception as e:
            print(f"Error updating config: {e}")
    
    print("Bot reset complete.")

if __name__ == "__main__":
    reset_bot()
