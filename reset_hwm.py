import sqlite3
import time

conn = sqlite3.connect('state.db')
cursor = conn.cursor()
try:
    now = str(time.time())
    cursor.execute("UPDATE risk_state SET value='10000.0', updated_at=? WHERE key LIKE '%equity%' OR key LIKE '%hwm%' OR key LIKE '%watermark%'", (now,))
    cursor.execute("UPDATE portfolio_stats SET value=10000.0, updated_at=? WHERE key LIKE '%equity%' OR key LIKE '%hwm%' OR key LIKE '%watermark%'", (now,))
    conn.commit()
    print("Updated all equity fields to 10000.0")
except Exception as e:
    print("Error:", e)
finally:
    conn.close()
