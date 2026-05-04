import sqlite3
import os

db_path = "state.db"
if not os.path.exists(db_path):
    print(f"Database {db_path} not found.")
else:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT symbol, side, qty, net_pnl_usd FROM positions WHERE status != 'CLOSED'")
    rows = cursor.fetchall()
    for row in rows:
        print(f"Symbol: {row['symbol']}, Side: {row['side']}, Qty: {row['qty']}, PnL: {row['net_pnl_usd']}")
    conn.close()
