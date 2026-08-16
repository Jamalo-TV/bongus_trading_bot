"""Static dependency gate for standalone cross-venue research entrypoints."""

from __future__ import annotations

import ast
import hashlib
import sys
from pathlib import Path


class ResearchBoundaryViolation(RuntimeError):
    """Research source crossed into credentials, signing, trading, or live IPC."""


_FORBIDDEN_IMPORT_PREFIXES = (
    "bongus.core.config",
    "bongus.core.config_manager",
    "bongus.engine",
    "bongus.ipc",
    "bongus.monitoring",
    "bongus.portfolio",
    "bongus.runtime",
    "bongus.strategies",
    "coincurve",
    "cryptography",
    "dotenv",
    "ecdsa",
    "eth_account",
    "hmac",
    "hyperliquid",
    "importlib",
    "nacl",
    "optuna",
    "sklearn",
    "tensorflow",
    "torch",
    "web3",
    "zmq",
)
_FORBIDDEN_SOURCE_VALUES = (
    "state" + ".db",
    "live_" + "config.json",
    "binance_" + "api_key",
    "binance_" + "api_secret",
    "hyperliquid_" + "private_key",
    "." + "env",
)
_FORBIDDEN_PORTS = (int("555" + "5"), int("900" + "0"))
_ALLOWED_PROJECT_IMPORT_PREFIXES = (
    "bongus.exchanges.hyperliquid_read_only",
    "bongus.research.cross_venue",
)
_PINNED_RUNTIME_REQUIREMENTS = ("pyarrow==23.0.1",)


def _imports(tree: ast.AST) -> tuple[str, ...]:
    values: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            values.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            values.append(node.module)
            values.extend(f"{node.module}.{alias.name}" for alias in node.names)
    return tuple(values)


def assert_research_boundary(paths: tuple[str | Path, ...]) -> str:
    """Validate source files and return a deterministic boundary source hash."""

    digest = hashlib.sha256()
    for raw_path in sorted((Path(path).resolve() for path in paths), key=str):
        if not raw_path.is_file() or raw_path.suffix.casefold() != ".py":
            raise ResearchBoundaryViolation(f"boundary source is not a Python file: {raw_path}")
        source = raw_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(raw_path))
        for module in _imports(tree):
            if any(module == prefix or module.startswith(prefix + ".") for prefix in _FORBIDDEN_IMPORT_PREFIXES):
                raise ResearchBoundaryViolation(f"forbidden research import {module!r} in {raw_path.name}")
            root_module = module.split(".", 1)[0]
            standard_library = root_module in sys.stdlib_module_names or root_module == "__future__"
            project_import = any(
                module == prefix or module.startswith(prefix + ".") for prefix in _ALLOWED_PROJECT_IMPORT_PREFIXES
            )
            optional_parquet = raw_path.name == "artifacts.py" and (
                module == "pyarrow" or module.startswith("pyarrow.")
            )
            if root_module == "bongus" and not project_import:
                raise ResearchBoundaryViolation(f"forbidden research import {module!r} in {raw_path.name}")
            if not standard_library and not project_import and not optional_parquet:
                raise ResearchBoundaryViolation(f"non-standard research import {module!r} in {raw_path.name}")
        if raw_path.name != Path(__file__).name:
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    if isinstance(node.func, ast.Name) and node.func.id == "__import__":
                        raise ResearchBoundaryViolation(f"dynamic imports are forbidden in {raw_path.name}")
                    if isinstance(node.func, ast.Attribute) and node.func.attr in {
                        "getenv",
                        "import_module",
                    }:
                        raise ResearchBoundaryViolation(
                            f"environment/dynamic import access is forbidden in {raw_path.name}"
                        )
                if isinstance(node, ast.Attribute) and node.attr == "environ":
                    raise ResearchBoundaryViolation(f"environment credential access is forbidden in {raw_path.name}")
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    lowered = node.value.casefold()
                    if any(value in lowered for value in _FORBIDDEN_SOURCE_VALUES):
                        raise ResearchBoundaryViolation(f"forbidden live/credential literal in {raw_path.name}")
                    if lowered.rstrip("/").endswith("/" + "exchange"):
                        raise ResearchBoundaryViolation(f"forbidden mutation endpoint in {raw_path.name}")
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, int)
                    and not isinstance(node.value, bool)
                    and node.value in _FORBIDDEN_PORTS
                ):
                    raise ResearchBoundaryViolation(f"forbidden live IPC port in {raw_path.name}")
        encoded_path = raw_path.name.encode("utf-8")
        encoded_source = source.encode("utf-8")
        digest.update(len(encoded_path).to_bytes(4, "big"))
        digest.update(encoded_path)
        digest.update(len(encoded_source).to_bytes(8, "big"))
        digest.update(encoded_source)
    return digest.hexdigest()


def default_research_boundary_paths(entrypoint: str | Path) -> tuple[Path, ...]:
    package_root = Path(__file__).resolve().parent
    repository_root = package_root.parents[2]
    adapter = repository_root / "bongus" / "exchanges" / "hyperliquid_read_only.py"
    return tuple(sorted(package_root.glob("*.py"), key=str)) + (
        adapter,
        Path(entrypoint).resolve(),
    )


def assert_default_research_boundary(entrypoint: str | Path) -> str:
    source_hash = assert_research_boundary(default_research_boundary_paths(entrypoint))
    package_root = Path(__file__).resolve().parent
    requirements = package_root.parents[2] / "requirements-cross-venue.txt"
    if not requirements.is_file():
        raise ResearchBoundaryViolation("cross-venue runtime requirements are missing")
    requirements_text = requirements.read_text(encoding="utf-8")
    dependency_lines = tuple(
        line.strip() for line in requirements_text.splitlines() if line.strip() and not line.lstrip().startswith("#")
    )
    if dependency_lines != _PINNED_RUNTIME_REQUIREMENTS:
        raise ResearchBoundaryViolation(
            "cross-venue runtime requirements must contain only the fixed PyArrow artifact backend"
        )
    digest = hashlib.sha256()
    digest.update(source_hash.encode("ascii"))
    digest.update(("\n".join(requirements_text.splitlines()) + "\n").encode("utf-8"))
    return digest.hexdigest()


__all__ = [
    "ResearchBoundaryViolation",
    "assert_default_research_boundary",
    "assert_research_boundary",
    "default_research_boundary_paths",
]
