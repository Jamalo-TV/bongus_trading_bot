"""The historical package runtime must remain a delegate, never a fork."""

import ast
from pathlib import Path

import bongus.runtime.live_trader as compatibility_runtime
import scripts.live_trader as compatibility_script
import scripts.live_trader_v2 as canonical_runtime


ROOT = Path(__file__).resolve().parents[1]


def test_historical_runtime_imports_resolve_to_the_canonical_implementation():
    assert compatibility_runtime.LiveTraderV2 is canonical_runtime.LiveTraderV2
    assert compatibility_runtime.CanonicalMultiSymbolTrader is canonical_runtime.LiveTraderV2
    assert compatibility_runtime.main is canonical_runtime.main
    assert compatibility_runtime.run_cli is canonical_runtime.run_cli
    assert compatibility_script.LiveTraderV2 is canonical_runtime.LiveTraderV2
    assert compatibility_script.CanonicalMultiSymbolTrader is canonical_runtime.LiveTraderV2


def test_historical_runtime_files_contain_no_executable_trader_class():
    for relative_path in ("bongus/runtime/live_trader.py", "scripts/live_trader.py"):
        tree = ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))
        assert not any(
            isinstance(node, ast.ClassDef)
            and node.name in {"LiveTraderV2", "CanonicalMultiSymbolTrader"}
            for node in ast.walk(tree)
        )
