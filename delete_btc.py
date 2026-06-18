import sqlite3
import os

db_path = "/mnt/data/bongus_trading_bot/state.db"
if not os.path.exists(db_path):
    print(f"Database {db_path} not found.")
else:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM positions WHERE symbol = 'BTCUSDT'")
    conn.commit()
    print("Deleted BTCUSDT from positions")
    conn.close()
