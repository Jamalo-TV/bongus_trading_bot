from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import live_trader_v2 as compatibility_trader
import scripts.live_trader_v2 as canonical_trader
from bongus.monitoring import king_watchdog


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_watchdog_launches_only_the_canonical_trader_module():
    expected = [sys.executable, "-m", "scripts.live_trader_v2"]

    assert king_watchdog.CANONICAL_TRADER_MODULE == "scripts.live_trader_v2"
    assert king_watchdog.PYTHON_COMMAND == expected

    process_defs, skipped = king_watchdog._build_process_defs(
        rust_build_ok=True,
        sentiment_enabled=False,
    )
    commands = {name: command for name, command, _ in process_defs}

    assert skipped == ()
    assert commands["trader"] == expected


def test_process_manifest_is_the_supervisor_source_of_truth():
    manifest_path = PROJECT_ROOT / "bongus" / "runtime" / "process_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["schema_version"] == 1
    assert manifest["canonical_trader"] == "trader"
    assert manifest["processes"]["trader"] == {
        "kind": "python_module",
        "target": "scripts.live_trader_v2",
        "required_for_trading": True,
    }
    assert manifest["processes"]["telegram"] == {
        "kind": "python_script",
        "target": "bongus/monitoring/telegram_alerter.py",
        "required_for_trading": False,
    }
    assert king_watchdog.PROCESS_MANIFEST == manifest
    assert manifest["deprecated_implementations"] == []
    assert set(manifest["compatibility_entrypoints"]) == {
        "live_trader_v2.py",
        "scripts/live_trader.py",
        "bongus.runtime.live_trader",
    }
    process_defs, _ = king_watchdog._build_process_defs(
        rust_build_ok=True,
        sentiment_enabled=True,
    )
    supervised = {name for name, _, _ in process_defs}
    manifest_names = set(manifest["processes"])
    assert supervised <= manifest_names


def test_root_trader_is_a_delegate_without_a_second_implementation():
    wrapper_path = PROJECT_ROOT / "live_trader_v2.py"
    wrapper_tree = ast.parse(wrapper_path.read_text(encoding="utf-8"))

    assert compatibility_trader.LiveTraderV2 is canonical_trader.LiveTraderV2
    assert compatibility_trader.main is canonical_trader.main
    assert compatibility_trader.run_cli is canonical_trader.run_cli
    assert not any(
        isinstance(node, ast.ClassDef) and node.name == "LiveTraderV2"
        for node in ast.walk(wrapper_tree)
    )


def test_stale_trader_process_forms_remain_detectable_for_cleanup():
    matchers = king_watchdog._PYTHON_PROCESS_MATCHERS["trader"]

    assert king_watchdog.CANONICAL_TRADER_MODULE in matchers
    assert "scripts/live_trader_v2.py" in matchers
    assert "live_trader_v2.py" in matchers
