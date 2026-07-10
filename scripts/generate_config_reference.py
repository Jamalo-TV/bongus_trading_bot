"""Generate CONFIG.md from the live ConfigManager defaults."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bongus.core.config_manager import ConfigManager

OUTPUT_PATH = PROJECT_ROOT / "CONFIG.md"


def _format_default(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return f"`{json.dumps(value, sort_keys=True)}`"
    return f"`{value}`"


def render_config_reference() -> str:
    manager = ConfigManager(config_path=PROJECT_ROOT / ".missing-live-config-for-docs.json")
    try:
        defaults = manager.snapshot()
    finally:
        manager.stop_watching()

    required = ConfigManager.required_live_keys()
    lines = [
        "# Bongus Runtime Config Reference",
        "",
        "This file is generated from `bongus/core/config_manager.py`.",
        "Run `python3 scripts/generate_config_reference.py` after adding or changing live config keys.",
        "",
        "| Key | Required in live_config.json | Default |",
        "| --- | --- | --- |",
    ]
    for key in sorted(defaults):
        lines.append(
            f"| `{key}` | {'yes' if key in required else 'no'} | {_format_default(defaults[key])} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    OUTPUT_PATH.write_text(render_config_reference(), encoding="utf-8")


if __name__ == "__main__":
    main()
