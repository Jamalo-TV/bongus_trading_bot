import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from bongus.core.config import (
    EMERGENCY_EXIT_MAX_RETRIES,
    EMERGENCY_EXIT_MAX_SLIPPAGE_BPS,
    EMERGENCY_EXIT_READBACK_ATTEMPTS,
    ENTRY_ANN_FUNDING_THRESHOLD,
    LIVE_CONFIG_PATH,
    PROJECT_ROOT,
    RUNTIME_DATA_ROOT,
    STATE_DB_PATH,
    STORAGE_BASE_RUNTIME_RESERVATION_BYTES,
    STORAGE_COMPONENT_BUDGETS_BYTES,
    STORAGE_RESERVE_BYTES,
    STORAGE_UNMANAGED_CONTINGENCY_BYTES,
    STORAGE_VOLUME_BUDGET_BYTES,
    STORAGE_WARNING_FREE_BYTES,
)
from bongus.core.config_manager import ConfigManager, validate_live_config
from scripts.generate_config_reference import render_config_reference


def test_compiled_storage_budget_is_one_exact_sixty_gigabyte_model() -> None:
    assert STORAGE_VOLUME_BUDGET_BYTES == 60_000_000_000
    assert STORAGE_WARNING_FREE_BYTES == 20_000_000_000
    assert STORAGE_WARNING_FREE_BYTES / STORAGE_VOLUME_BUDGET_BYTES > 0.30
    assert STORAGE_COMPONENT_BUDGETS_BYTES["state_db"] == 6_500_000_000
    assert STORAGE_COMPONENT_BUDGETS_BYTES["backup"] == 20_500_000_000
    assert (
        STORAGE_BASE_RUNTIME_RESERVATION_BYTES
        + STORAGE_UNMANAGED_CONTINGENCY_BYTES
        + sum(STORAGE_COMPONENT_BUDGETS_BYTES.values())
        + STORAGE_RESERVE_BYTES
        + STORAGE_WARNING_FREE_BYTES
        == STORAGE_VOLUME_BUDGET_BYTES
    )


def test_checked_in_live_config_uses_the_compiled_storage_model() -> None:
    payload = json.loads((PROJECT_ROOT / "live_config.json").read_text(encoding="utf-8"))

    assert payload["storage_volume_budget_bytes"] == STORAGE_VOLUME_BUDGET_BYTES
    assert payload["storage_component_budgets_bytes"] == STORAGE_COMPONENT_BUDGETS_BYTES
    assert payload["storage_reserve_bytes"] == STORAGE_RESERVE_BYTES
    assert payload["storage_warning_free_bytes"] == STORAGE_WARNING_FREE_BYTES


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
        on_reload=lambda changed, snapshot: reloads.append({"changed": changed, "snapshot": snapshot}),
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


def test_removed_override_restores_default_and_updates_version_hash(tmp_path):
    config_path = tmp_path / "live_config.json"
    config_path.write_text(
        json.dumps({"entry_ann_funding_threshold": 0.25}),
        encoding="utf-8",
    )
    reloads: list[dict] = []
    manager = ConfigManager(
        config_path=config_path,
        on_reload=lambda changed, snapshot: reloads.append(changed),
    )
    first_hash = manager.version_hash

    config_path.write_text("{}\n", encoding="utf-8")
    manager._last_mtime = 0

    assert manager.reload_now() is True
    assert manager.get("entry_ann_funding_threshold") == ENTRY_ANN_FUNDING_THRESHOLD
    assert manager.version_hash != first_hash
    assert reloads[-1]["entry_ann_funding_threshold"] == (
        0.25,
        ENTRY_ANN_FUNDING_THRESHOLD,
    )


def test_partial_override_is_validated_against_effective_defaults():
    try:
        validate_live_config({"soft_drawdown_pct": 0.11})
    except ValueError as exc:
        assert "soft_drawdown_pct" in str(exc)
    else:
        raise AssertionError("soft drawdown above the default kill threshold must be rejected")


@pytest.mark.parametrize(
    ("key", "unsafe_value"),
    [
        ("emergency_exit_max_retries", EMERGENCY_EXIT_MAX_RETRIES + 1),
        (
            "emergency_exit_readback_attempts",
            EMERGENCY_EXIT_READBACK_ATTEMPTS + 1,
        ),
        (
            "emergency_exit_max_slippage_bps",
            EMERGENCY_EXIT_MAX_SLIPPAGE_BPS + 0.1,
        ),
    ],
)
def test_emergency_budgets_can_only_tighten_compiled_safety_ceilings(
    key: str,
    unsafe_value: float,
) -> None:
    with pytest.raises(ValueError, match="compiled"):
        validate_live_config({key: unsafe_value})


def test_atomic_writer_leaves_complete_json_and_consistent_hash(tmp_path):
    config_path = tmp_path / "live_config.json"
    writer = ConfigManager(config_path=config_path)
    reader = ConfigManager(config_path=config_path)

    writer.apply_updates(
        {
            "pause_new_entries": True,
            "entry_ann_funding_threshold": 0.2,
        }
    )
    reader._last_mtime = 0
    assert reader.reload_now() is True

    with config_path.open(encoding="utf-8") as handle:
        stored = json.load(handle)
    assert stored["pause_new_entries"] is True
    assert stored["entry_ann_funding_threshold"] == 0.2
    if os.name == "posix":
        assert config_path.stat().st_mode & 0o777 == 0o640
    assert reader.version_hash == writer.version_hash
    assert not list(tmp_path.glob(".live_config.json.*.tmp"))


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


def test_runtime_paths_are_data_root_absolute():
    runtime_data_root = Path(RUNTIME_DATA_ROOT).resolve()

    assert Path(LIVE_CONFIG_PATH).is_absolute()
    assert Path(STATE_DB_PATH).is_absolute()
    assert Path(LIVE_CONFIG_PATH).resolve().parent == runtime_data_root
    assert Path(STATE_DB_PATH).resolve().parent == runtime_data_root


def test_data_root_relocates_default_live_config_outside_signed_release(tmp_path):
    (tmp_path / "live_config.json").write_text(
        json.dumps({"autonomous_startup_recovery": True}),
        encoding="utf-8",
    )
    environment = {**os.environ, "BONGUS_DATA_ROOT": str(tmp_path.resolve())}
    for name in (
        "BONGUS_STATE_DB_PATH",
        "BONGUS_AUDIT_DB_PATH",
        "BONGUS_RESEARCH_DB_PATH",
    ):
        environment.pop(name, None)
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json; "
                "from bongus.core.config import LIVE_CONFIG_PATH, RUNTIME_DATA_ROOT; "
                "from bongus.core.config_manager import ConfigManager; "
                "manager = ConfigManager(); "
                "print(json.dumps([LIVE_CONFIG_PATH, str(RUNTIME_DATA_ROOT), "
                "manager.get_bool('autonomous_startup_recovery')]))"
            ),
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    live_config_path, data_root, autonomous_recovery = json.loads(completed.stdout)
    assert Path(data_root) == tmp_path.resolve()
    assert Path(live_config_path) == tmp_path.resolve() / "live_config.json"
    assert autonomous_recovery is True


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
    assert validate_live_config({"validation_adjust_notional_scale": 0.5})["validation_adjust_notional_scale"] == 0.5

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


def test_storage_control_handshake_fields_are_wire_visible_but_not_operator_configurable():
    allowed = ConfigManager.allowed_keys()
    assert {
        "storage_control_generation",
        "storage_emergency_latched",
        "storage_recovery_acknowledged",
    } <= allowed

    for key, value in (
        ("storage_control_generation", 99),
        ("storage_emergency_latched", True),
        ("storage_recovery_acknowledged", True),
    ):
        with pytest.raises(ValueError, match="internal"):
            validate_live_config({key: value})


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


def test_config_reference_render_includes_required_keys():
    rendered = render_config_reference()

    assert "This file is generated from `bongus/core/config_manager.py`." in rendered
    assert "| `pause_new_entries` | yes |" in rendered
    assert "| `autonomous_startup_recovery` | yes |" in rendered
