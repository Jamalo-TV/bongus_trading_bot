import sqlite3
import time

conn = sqlite3.connect('state.db')
cursor = conn.cursor()
try:
    cursor.execute("SELECT value FROM risk_state WHERE key='exchange_account_equity'")
    row = cursor.fetchone()
    if row:
        actual_equity = float(row[0])
        print(f"Found actual Binance balance: {actual_equity}")
        
        now = str(time.time())
        # Update HWM to match exactly the actual balance to clear the drawdown
        cursor.execute("UPDATE risk_state SET value=?, updated_at=? WHERE key LIKE '%hwm%' OR key LIKE '%watermark%'", (str(actual_equity), now))
        cursor.execute("UPDATE risk_state SET value=?, updated_at=? WHERE key='account_equity'", (str(actual_equity), now))
        cursor.execute("UPDATE portfolio_stats SET value=?, updated_at=? WHERE key LIKE '%hwm%' OR key LIKE '%watermark%' OR key='account_equity'", (actual_equity, now))
        conn.commit()
        print("Successfully synced High Watermark to match the actual Binance balance!")
    else:
        print("Could not find actual Binance balance in database.")
except Exception as e:
    print("Error:", e)
finally:
    conn.close()
