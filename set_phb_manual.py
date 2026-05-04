import sqlite3
import os

db_path = "state.db"
if not os.path.exists(db_path):
    print(f"Database {db_path} not found.")
else:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Set to manual_review to stop rotation attempts
    cursor.execute("UPDATE positions SET recovery_state='manual_review' WHERE symbol='PHBUSDT'")
    print(f"PHBUSDT set to manual_review: {cursor.rowcount} rows")
    
    conn.commit()
    conn.close()
