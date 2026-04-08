import json
from pathlib import Path

from bongus.core.config import LIVE_CONFIG_PATH, PROJECT_ROOT, STATE_DB_PATH
from bongus.core.config_manager import ConfigManager


def test_config_manager_rejects_invalid_reload_and_keeps_last_good_values(tmp_path):
    config_path = tmp_path / "live_config.json"
    config_path.write_text(
        json.dumps({"entry_ann_funding_threshold": 0.1}),
        encoding="utf-8",
    )
    errors: list[str] = []
    manager = ConfigManager(
        config_path=config_path,
        on_validation_error=errors.append,
    )
    try:
        assert manager.get("entry_ann_funding_threshold") == 0.1

        config_path.write_text(
            json.dumps({"entry_ann_funding_threshold": 0.2, "unexpected_key": 1}),
            encoding="utf-8",
        )
        manager._last_mtime = 0
        assert manager.reload_now() is False
        assert errors
        assert "unexpected_key" in manager.last_error
        assert manager.get("entry_ann_funding_threshold") == 0.1
    finally:
        manager.stop_watching()


def test_config_manager_emits_reload_callback_for_valid_changes(tmp_path):
    config_path = tmp_path / "live_config.json"
    config_path.write_text(
        json.dumps({"entry_ann_funding_threshold": 0.1}),
        encoding="utf-8",
    )
    reloads: list[dict] = []
    manager = ConfigManager(
        config_path=config_path,
        on_reload=lambda changed, snapshot: reloads.append(
            {"changed": changed, "snapshot": snapshot}
        ),
    )
    try:
        config_path.write_text(
            json.dumps({"entry_ann_funding_threshold": 0.25}),
            encoding="utf-8",
        )
        manager._last_mtime = 0
        assert manager.reload_now() is True
        assert reloads
        assert reloads[-1]["changed"]["entry_ann_funding_threshold"] == (0.1, 0.25)
        assert reloads[-1]["snapshot"]["entry_ann_funding_threshold"] == 0.25
    finally:
        manager.stop_watching()


def test_apply_updates_persists_only_allowed_keys(tmp_path):
    config_path = tmp_path / "live_config.json"
    manager = ConfigManager(config_path=config_path)

    updated = manager.apply_updates(
        {
            "pause_new_entries": True,
            "entry_ann_funding_threshold": 0.2,
            "unknown_setting": 123,
        }
    )

    assert updated["pause_new_entries"] is True
    assert updated["entry_ann_funding_threshold"] == 0.2

    with open(config_path, encoding="utf-8") as handle:
        stored = json.load(handle)

    assert stored["pause_new_entries"] is True
    assert stored["entry_ann_funding_threshold"] == 0.2
    assert "unknown_setting" not in stored

    reloaded = ConfigManager(config_path=config_path)
    assert reloaded.get("pause_new_entries") is True
    assert reloaded.get("entry_ann_funding_threshold") == 0.2
    assert "pause_new_entries" in ConfigManager.allowed_keys()


def test_runtime_paths_are_project_absolute():
    project_root = Path(PROJECT_ROOT).resolve()

    assert Path(LIVE_CONFIG_PATH).is_absolute()
    assert Path(STATE_DB_PATH).is_absolute()
    assert Path(LIVE_CONFIG_PATH).resolve().parent == project_root
    assert Path(STATE_DB_PATH).resolve().parent == project_root
