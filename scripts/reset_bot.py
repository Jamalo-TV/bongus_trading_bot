
import sqlite3
import json
import os
from datetime import datetime, timezone

STATE_DB_PATH = "bongus_trading_bot/state.db"
LIVE_CONFIG_PATH = "bongus_trading_bot/live_config.json"

def reset_bot():
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
        "validation_snapshots"
    ]
    
    for table in tables_to_clear:
        print(f"Clearing table {table}...")
        cursor.execute(f"DELETE FROM {table}")

    # 2. Reset portfolio_stats and risk_state
    print("Resetting stats and risk state...")
    
    # Get current exchange equity if available, otherwise use a default or stay as is
    cursor.execute("SELECT value FROM risk_state WHERE key = 'exchange_account_equity'")
    row = cursor.fetchone()
    current_equity = 10000.0
    if row:
        try:
            current_equity = float(row[0])
            print(f"Found exchange equity: {current_equity}")
        except:
            pass
    
    # Reset equity values
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

    conn.commit()
    conn.close()

    # 3. Update live_config.json
    if os.path.exists(LIVE_CONFIG_PATH):
        print(f"Updating {LIVE_CONFIG_PATH}...")
        with open(LIVE_CONFIG_PATH, 'r') as f:
            config = json.load(f)
        
        config["account_equity_usd"] = current_equity
        config["reset_equity_high_watermark"] = True
        
        with open(LIVE_CONFIG_PATH, 'w') as f:
            json.dump(config, f, indent=2)
    
    print("Bot reset complete.")

if __name__ == "__main__":
    reset_bot()
