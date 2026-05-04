import sqlite3
import os

db_path = "state.db"
if not os.path.exists(db_path):
    print(f"Database {db_path} not found.")
else:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # 1. Close PHBUSDT
    cursor.execute("UPDATE positions SET status='CLOSED', recovery_state='cleaned_dust' WHERE symbol='PHBUSDT'")
    print(f"Rows affected for PHBUSDT: {cursor.rowcount}")
    
    # 2. Reset the failed flatten status so a new one can be triggered if needed
    cursor.execute("UPDATE risk_state SET value='completed' WHERE key='operator_flatten_all_status'")
    print(f"Flatten status reset.")
    
    conn.commit()
    conn.close()
    print("Cleanup complete.")
