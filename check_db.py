import sqlite3
import os

db_path = 'state.db'
if not os.path.exists(db_path):
    print(f"{db_path} not found")
    exit(1)

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# Get list of tables
cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
tables = cursor.fetchall()

print(f"{'Table':<30} | {'Rows':<10}")
print("-" * 45)

for (table_name,) in tables:
    cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
    count = cursor.fetchone()[0]
    print(f"{table_name:<30} | {count:<10}")

conn.close()
