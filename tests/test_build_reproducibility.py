from __future__ import annotations

import json
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _requirement_lines(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_python_lock_is_exact_and_covers_every_direct_requirement():
    direct = [Requirement(line) for line in _requirement_lines(PROJECT_ROOT / "requirements.txt")]
    locked_requirements = [Requirement(line) for line in _requirement_lines(PROJECT_ROOT / "requirements.lock")]
    locked: dict[str, Version] = {}

    for requirement in locked_requirements:
        specs = list(requirement.specifier)
        assert requirement.url is None
        assert len(specs) == 1
        assert specs[0].operator == "=="
        locked[canonicalize_name(requirement.name)] = Version(specs[0].version)

    for requirement in direct:
        name = canonicalize_name(requirement.name)
        assert name in locked
        assert not requirement.specifier or locked[name] in requirement.specifier


def test_pyright_scope_is_the_active_runtime_only():
    config = json.loads((PROJECT_ROOT / "pyrightconfig.json").read_text(encoding="utf-8"))

    assert config["include"] == ["bongus", "scripts/live_trader_v2.py"]
    assert config["pythonVersion"] == "3.11"
    assert ".claude" in config["exclude"]
    assert ".worktrees" in config["exclude"]


def test_pinned_toolchain_and_cargo_lock_are_present():
    python_baseline = (PROJECT_ROOT / ".python-version").read_text(encoding="utf-8").strip()
    assert python_baseline == "3.11.15"
    ci = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    research_builder = (PROJECT_ROOT / "scripts" / "build_research_release.py").read_text(encoding="utf-8")
    research_installer = (PROJECT_ROOT / "deployment" / "Install-BongusResearch.sh").read_text(encoding="utf-8")
    assert f'python-version: "{python_baseline}"' in ci
    assert f'PYTHON_VERSION: Final[str] = "{python_baseline}"' in research_builder
    assert f"Exact Python {python_baseline} executable" in research_installer
    rust_toolchain = (PROJECT_ROOT / "rust-toolchain.toml").read_text(encoding="utf-8")

    assert 'channel = "1.94.1"' in rust_toolchain
    assert (PROJECT_ROOT / "execution_engine" / "Cargo.lock").is_file()
