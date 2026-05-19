import sqlite3
import os

db_path = 'state.db'
if not os.path.exists(db_path):
    print(f"{db_path} not found")
    exit(1)

conn = sqlite3.connect(db_path)
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA busy_timeout=10000")

# Use datetime() for proper comparison of ISO8601 strings
print("Deleting old candidate_snapshots (> 1 day)...")
cursor = conn.execute("DELETE FROM candidate_snapshots WHERE datetime(snapshot_time) < datetime('now', '-1 day')")
print(f"Deleted {cursor.rowcount} rows.")
conn.commit()

print("Deleting old feature_snapshots (> 1 day)...")
cursor = conn.execute("DELETE FROM feature_snapshots WHERE datetime(snapshot_time) < datetime('now', '-1 day')")
print(f"Deleted {cursor.rowcount} rows.")
conn.commit()

print("Deleting old market_samples (> 2 days)...")
# Note: market_samples uses sample_minute
cursor = conn.execute("DELETE FROM market_samples WHERE datetime(sample_minute) < datetime('now', '-2 days')")
print(f"Deleted {cursor.rowcount} rows.")
conn.commit()

print("Deleting old health_samples (> 2 days)...")
cursor = conn.execute("DELETE FROM health_samples WHERE datetime(sample_time) < datetime('now', '-2 days')")
print(f"Deleted {cursor.rowcount} rows.")
conn.commit()

print("Vacuuming database to reclaim space... (this may take a while)")
conn.execute("VACUUM")
conn.commit()

print("Final checkpoint...")
conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

conn.close()
print("Done.")
