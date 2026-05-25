from bongus.engine.state_store import StateWriter
import logging

logging.basicConfig(level=logging.INFO)
print("Starting prune...")
writer = StateWriter(migrate=False)
results = writer.archive_old_data(snapshot_retention_days=1, feature_retention_days=1)
print(f"Archived: {results}")
print("Vacuuming state.db...")
writer.maintenance(run_vacuum=True)
print("Done.")
