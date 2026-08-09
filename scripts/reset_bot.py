import sqlite3
import os
import sys
from pathlib import Path

# Fix: Use paths relative to the project root
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bongus.core.config_manager import ConfigManager

STATE_DB_PATH = str(PROJECT_ROOT / "state.db")
LIVE_CONFIG_PATH = str(PROJECT_ROOT / "live_config.json")

def reset_bot():
    raise RuntimeError(
        "reset_bot is disabled: bulk deletion destroys Tier-A execution, "
        "economic, lifecycle, and recovery evidence. Use the authenticated "
        "operator flatten/reconciliation workflow and retain the audit ledger."
    )

if __name__ == "__main__":
    reset_bot()
