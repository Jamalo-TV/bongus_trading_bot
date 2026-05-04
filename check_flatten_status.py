import sqlite3
import os

db_path = "state.db"
if not os.path.exists(db_path):
    print(f"Database {db_path} not found.")
else:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT key, value FROM risk_state WHERE key LIKE 'operator_flatten_all_%'")
    rows = cursor.fetchall()
    for row in rows:
        print(f"{row['key']}: {row['value']}")
    conn.close()
