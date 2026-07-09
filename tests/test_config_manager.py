import json
from pathlib import Path

from bongus.core.config import LIVE_CONFIG_PATH, PROJECT_ROOT, STATE_DB_PATH
from bongus.core.config_manager import ConfigManager, validate_live_config


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
            "startup_recovery_acknowledge_symbols": ["BTCUSDT"],
            "startup_recovery_auto_exit_manual_review": True,
            "unknown_setting": 123,
        }
    )

    assert updated["pause_new_entries"] is True
    assert updated["entry_ann_funding_threshold"] == 0.2
    assert updated["startup_recovery_acknowledge_symbols"] == ["BTCUSDT"]
    assert updated["startup_recovery_auto_exit_manual_review"] is True

    with open(config_path, encoding="utf-8") as handle:
        stored = json.load(handle)

    assert stored["pause_new_entries"] is True
    assert stored["entry_ann_funding_threshold"] == 0.2
    assert stored["startup_recovery_acknowledge_symbols"] == ["BTCUSDT"]
    assert stored["startup_recovery_auto_exit_manual_review"] is True
    assert "unknown_setting" not in stored

    reloaded = ConfigManager(config_path=config_path)
    assert reloaded.get("pause_new_entries") is True
    assert reloaded.get("entry_ann_funding_threshold") == 0.2
    assert reloaded.get("startup_recovery_acknowledge_symbols") == ["BTCUSDT"]
    assert reloaded.get("startup_recovery_auto_exit_manual_review") is True
    assert "pause_new_entries" in ConfigManager.allowed_keys()
    assert "startup_recovery_acknowledge_symbols" in ConfigManager.allowed_keys()
    assert "hwm_auto_decay_after_hours" in ConfigManager.allowed_keys()
    assert "hwm_auto_decay_fraction" in ConfigManager.allowed_keys()


def test_runtime_paths_are_project_absolute():
    project_root = Path(PROJECT_ROOT).resolve()

    assert Path(LIVE_CONFIG_PATH).is_absolute()
    assert Path(STATE_DB_PATH).is_absolute()
    assert Path(LIVE_CONFIG_PATH).resolve().parent == project_root
    assert Path(STATE_DB_PATH).resolve().parent == project_root


def test_live_config_validation_rejects_dangerous_drawdown_and_premium():
    try:
        validate_live_config({"max_drawdown_pct": 0.99})
    except ValueError as exc:
        assert "max_drawdown_pct" in str(exc)
    else:
        raise AssertionError("unsafe drawdown should be rejected")

    try:
        validate_live_config({"entry_premium_threshold": 0.005})
    except ValueError as exc:
        assert "entry_premium_threshold" in str(exc)
    else:
        raise AssertionError("toxic premium threshold should be rejected")


def test_live_config_validation_bounds_validation_adjust_scale():
    assert validate_live_config({"validation_adjust_notional_scale": 0.5})[
        "validation_adjust_notional_scale"
    ] == 0.5

    for unsafe in (0.0, 1.5):
        try:
            validate_live_config({"validation_adjust_notional_scale": unsafe})
        except ValueError as exc:
            assert "validation_adjust_notional_scale" in str(exc)
        else:
            raise AssertionError("unsafe validation ADJUST scale should be rejected")


def test_live_config_validation_uses_strict_boolean_coercion():
    assert validate_live_config({"pause_new_entries": "false"})["pause_new_entries"] is False
    assert validate_live_config({"pause_new_entries": "true"})["pause_new_entries"] is True

    try:
        validate_live_config({"pause_new_entries": "definitely"})
    except ValueError as exc:
        assert "pause_new_entries" in str(exc)
    else:
        raise AssertionError("invalid boolean string should be rejected")


def test_config_manager_reports_missing_required_live_keys(tmp_path):
    config_path = tmp_path / "live_config.json"
    config_path.write_text(
        json.dumps({"pause_new_entries": True, "max_drawdown_pct": 0.1}),
        encoding="utf-8",
    )
    manager = ConfigManager(config_path=config_path)
    try:
        missing = manager.missing_required_live_keys()
        assert "account_equity_usd" in missing
        assert "pause_new_entries" not in missing
        assert "max_drawdown_pct" not in missing
        assert "max_drawdown_pct" in ConfigManager.required_live_keys()
    finally:
        manager.stop_watching()
