"""Emergency fail-closed configuration helper.

This historical script used to set the maximum drawdown to 99%, bypassing the
versioned configuration manager and effectively disabling a critical guard.
It now performs only the universally safe emergency action: pause new entries.
Risk-limit changes must use the governed proposal and promotion workflow.
"""

from pathlib import Path

from bongus.core.config_manager import ConfigManager


def main() -> None:
    config_path = Path(__file__).resolve().with_name("live_config.json")
    ConfigManager(config_path=config_path).apply_updates({"pause_new_entries": True})
    print("New entries paused. No risk limit or equity watermark was changed.")


if __name__ == "__main__":
    main()
