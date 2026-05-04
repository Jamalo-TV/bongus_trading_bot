import sqlite3
import os

db_path = "state.db"
if not os.path.exists(db_path):
    print(f"Database {db_path} not found.")
else:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT symbol, hedge_ratio, qty FROM positions WHERE symbol='PHBUSDT'")
    row = cursor.fetchone()
    if row:
        print(f"Symbol: {row['symbol']}, Hedge Ratio: {row['hedge_ratio']}, Qty: {row['qty']}")
    else:
        print("PHBUSDT not found in positions table.")
    conn.close()
