import os
import time

from bongus.engine.state_store import StateWriter


def main():
    if os.path.exists("test_state.db"):
        os.remove("test_state.db")
    if os.path.exists("test_state.db-shm"):
        os.remove("test_state.db-shm")
    if os.path.exists("test_state.db-wal"):
        os.remove("test_state.db-wal")

    writer = StateWriter("test_state.db")

    # Generate a large snapshot
    snapshot = {f"key_{i}": f"value_{i}" for i in range(1000)}

    start_time = time.time()
    for _ in range(10):  # run a few times
        writer.set_risk_snapshot(snapshot)
    end_time = time.time()

    print(f"Execution time: {end_time - start_time:.4f} seconds")
    writer.close()

if __name__ == "__main__":
    main()
