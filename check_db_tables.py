"""List SQLite tables without creating or mutating a database."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", nargs="?", type=Path, default=Path("state.db"))
    args = parser.parse_args()
    database = args.database.resolve(strict=True)
    if not database.is_file() or database.is_symlink():
        parser.error("database must be a regular non-link file")
    with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as connection:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
    print(json.dumps([str(row[0]) for row in rows]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
