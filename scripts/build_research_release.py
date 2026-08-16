#!/usr/bin/env python3
"""Build a deterministic, sanitized cross-venue research release archive."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Final, cast

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))
sys.dont_write_bytecode = True

from bongus.research.cross_venue.boundary import (  # noqa: E402
    ResearchBoundaryViolation,
    assert_research_boundary,
)

RELEASE_KIND: Final[str] = "bongus-cross-venue-research"
RELEASE_SCHEMA_VERSION: Final[int] = 1
MANIFEST_NAME: Final[str] = "research-release-manifest.json"
MANIFEST_DIGEST_NAME: Final[str] = "research-release-manifest.sha256"
REQUIREMENTS_PATH: Final[str] = "requirements-cross-venue.txt"
WHEELHOUSE_PATH: Final[str] = "wheelhouse-cross-venue"
PYTHON_VERSION: Final[str] = "3.11.15"
PYTHON_IMPLEMENTATION: Final[str] = "cp"
PYTHON_ABI: Final[str] = "cp311"
PYARROW_VERSION: Final[str] = "23.0.1"
PINNED_REQUIREMENTS: Final[tuple[str, ...]] = (f"pyarrow=={PYARROW_VERSION}",)

ENTRYPOINTS: Final[tuple[str, ...]] = (
    "scripts/screen_binance_hyperliquid_history.py",
    "scripts/collect_binance_hyperliquid_shadow.py",
    "scripts/replay_binance_hyperliquid.py",
    "scripts/backtest_binance_hyperliquid.py",
    "scripts/report_binance_hyperliquid.py",
    "scripts/verify_cross_venue_dataset.py",
    "scripts/evaluate_binance_hyperliquid.py",
    "scripts/probe_cross_venue_region.py",
    "scripts/evaluate_cross_venue_regions.py",
)
_CROSS_VENUE_MODULES: Final[tuple[str, ...]] = (
    "__init__.py",
    "artifacts.py",
    "boundary.py",
    "cadence.py",
    "collector.py",
    "evaluation.py",
    "evidence.py",
    "feeds.py",
    "historical.py",
    "kernel.py",
    "normalization.py",
    "publication.py",
    "region_probe.py",
    "region_probe_network.py",
    "replay.py",
    "schema.py",
    "storage.py",
)
_COPIED_FILES: Final[tuple[str, ...]] = (
    *(f"bongus/research/cross_venue/{name}" for name in _CROSS_VENUE_MODULES),
    "bongus/exchanges/hyperliquid_read_only.py",
    *ENTRYPOINTS,
    "research/experiments/binance_hyperliquid_v1.json",
    "docs/BINANCE_HYPERLIQUID_RESEARCH.md",
    REQUIREMENTS_PATH,
    "deployment/bongus-research.service.in",
    "deployment/Install-BongusResearch.sh",
)
_GENERATED_PACKAGE_FILES: Final[Mapping[str, bytes]] = {
    "bongus/__init__.py": b'"""Sanitized Bongus research release namespace."""\n',
    "bongus/research/__init__.py": b'"""Reproducible isolated research infrastructure."""\n',
    "bongus/exchanges/__init__.py": b'"""Read-only venue adapters included in the research release."""\n',
}
_TARGETS: Final[Mapping[str, Mapping[str, str]]] = {
    "linux-x86_64": {
        "os": "linux",
        "architecture": "x86_64",
        "wheel_platform": "manylinux_2_28_x86_64",
    },
    "linux-aarch64": {
        "os": "linux",
        "architecture": "aarch64",
        "wheel_platform": "manylinux_2_28_aarch64",
    },
}
_CONTROL_FILES: Final[frozenset[str]] = frozenset({MANIFEST_NAME, MANIFEST_DIGEST_NAME})


class ResearchReleaseError(RuntimeError):
    """The research release cannot be proven complete and isolated."""


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_regular_file(path: Path, *, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise ResearchReleaseError(f"{label} is missing, linked, or not a regular file: {path}")


def _dependency_lines(requirements: Path) -> tuple[str, ...]:
    _require_regular_file(requirements, label="research requirements")
    return tuple(
        line.strip()
        for line in requirements.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def _validate_source_inventory(source_root: Path) -> tuple[Path, ...]:
    source_root = source_root.resolve()
    package_root = source_root / "bongus" / "research" / "cross_venue"
    actual_modules = tuple(sorted(path.name for path in package_root.glob("*.py") if path.is_file()))
    if actual_modules != tuple(sorted(_CROSS_VENUE_MODULES)):
        expected_module_set: set[str] = set(_CROSS_VENUE_MODULES)
        actual_module_set: set[str] = set(actual_modules)
        missing = sorted(expected_module_set - actual_module_set)
        unexpected = sorted(actual_module_set - expected_module_set)
        raise ResearchReleaseError(f"research package inventory changed; missing={missing}, unexpected={unexpected}")

    source_paths: list[Path] = []
    for relative in _COPIED_FILES:
        path = source_root / Path(relative)
        _require_regular_file(path, label="release source")
        try:
            path.resolve().relative_to(source_root)
        except ValueError as exc:
            raise ResearchReleaseError(f"release source escapes repository root: {path}") from exc
        source_paths.append(path)

    dependency_lines = _dependency_lines(source_root / REQUIREMENTS_PATH)
    if dependency_lines != PINNED_REQUIREMENTS:
        raise ResearchReleaseError(
            f"research requirements must be exactly {PINNED_REQUIREMENTS!r}; got {dependency_lines!r}"
        )

    boundary_sources = tuple(
        source_root / Path(relative)
        for relative in (
            *(f"bongus/research/cross_venue/{name}" for name in _CROSS_VENUE_MODULES),
            "bongus/exchanges/hyperliquid_read_only.py",
            *ENTRYPOINTS,
        )
    )
    try:
        assert_research_boundary(boundary_sources)
    except ResearchBoundaryViolation as exc:
        raise ResearchReleaseError(str(exc)) from exc
    return boundary_sources


def _metadata_headers(raw: bytes) -> Mapping[str, tuple[str, ...]]:
    values: dict[str, list[str]] = {}
    for line in raw.decode("utf-8").splitlines():
        if not line:
            break
        if line[:1].isspace() or ":" not in line:
            continue
        key, value = line.split(":", 1)
        values.setdefault(key.casefold(), []).append(value.strip())
    return {key: tuple(items) for key, items in values.items()}


def validate_wheelhouse(wheelhouse: Path, target_name: str) -> tuple[Path, ...]:
    if wheelhouse.is_symlink() or not wheelhouse.is_dir():
        raise ResearchReleaseError(f"offline wheelhouse is missing, linked, or not a directory: {wheelhouse}")
    if target_name not in _TARGETS:
        raise ResearchReleaseError(f"unsupported research target: {target_name}")
    expected_tag = f"{PYTHON_ABI}-{PYTHON_ABI}-{_TARGETS[target_name]['wheel_platform']}"
    entries = tuple(sorted(wheelhouse.iterdir(), key=lambda path: path.name.casefold()))
    if len(entries) != 1:
        raise ResearchReleaseError("complete research wheelhouse must contain exactly one PyArrow wheel")
    wheel = entries[0]
    _require_regular_file(wheel, label="research wheel")
    expected_filename = f"pyarrow-{PYARROW_VERSION}-{expected_tag}.whl"
    if wheel.name.casefold() != expected_filename.casefold():
        raise ResearchReleaseError(
            f"wheel does not match the pinned target; expected {expected_filename}, got {wheel.name}"
        )
    try:
        with zipfile.ZipFile(wheel) as archive:
            members = archive.namelist()
            metadata_names = tuple(name for name in members if name.endswith(".dist-info/METADATA"))
            wheel_names = tuple(name for name in members if name.endswith(".dist-info/WHEEL"))
            if len(metadata_names) != 1 or len(wheel_names) != 1:
                raise ResearchReleaseError("PyArrow wheel has an invalid metadata inventory")
            headers = _metadata_headers(archive.read(metadata_names[0]))
            wheel_headers = _metadata_headers(archive.read(wheel_names[0]))
    except (OSError, UnicodeError, zipfile.BadZipFile) as exc:
        raise ResearchReleaseError(f"invalid PyArrow wheel: {wheel}") from exc
    if tuple(value.casefold() for value in headers.get("name", ())) != ("pyarrow",):
        raise ResearchReleaseError("wheel metadata does not declare exactly Name: pyarrow")
    if headers.get("version") != (PYARROW_VERSION,):
        raise ResearchReleaseError("wheel metadata version does not match the pinned PyArrow version")
    if headers.get("requires-dist", ()):
        raise ResearchReleaseError("PyArrow acquired dependencies; freeze and include them before release")
    if expected_tag not in wheel_headers.get("tag", ()):
        raise ResearchReleaseError(f"wheel metadata does not contain required tag {expected_tag}")
    return (wheel,)


def _download_wheelhouse(destination: Path, source_root: Path, target_name: str) -> None:
    target = _TARGETS[target_name]
    command = (
        sys.executable,
        "-I",
        "-m",
        "pip",
        "--isolated",
        "download",
        "--disable-pip-version-check",
        "--only-binary=:all:",
        "--no-deps",
        "--implementation",
        PYTHON_IMPLEMENTATION,
        "--python-version",
        "311",
        "--abi",
        PYTHON_ABI,
        "--platform",
        target["wheel_platform"],
        "--requirement",
        str(source_root / REQUIREMENTS_PATH),
        "--dest",
        str(destination),
    )
    subprocess.run(command, cwd=source_root, check=True)


def _copy_file(source: Path, destination: Path, *, mode: int = 0o644) -> None:
    _require_regular_file(source, label="release source")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    os.chmod(destination, mode)


def _inventory(root: Path) -> tuple[dict[str, object], ...]:
    files: list[dict[str, object]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise ResearchReleaseError(f"release contains a symbolic link: {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ResearchReleaseError(f"release contains a non-regular file: {path}")
        relative = path.relative_to(root).as_posix()
        if relative in _CONTROL_FILES:
            continue
        files.append({"path": relative, "sha256": _sha256_file(path), "size": path.stat().st_size})
    return tuple(files)


def _expected_payload_paths(wheel_name: str) -> frozenset[str]:
    return frozenset(
        {
            *_COPIED_FILES,
            *_GENERATED_PACKAGE_FILES,
            *ENTRYPOINTS,
            f"{WHEELHOUSE_PATH}/{wheel_name}",
        }
    )


def _manifest(
    root: Path,
    *,
    source_root: Path,
    target_name: str,
    boundary_sha256: str,
    wheel_name: str,
) -> Mapping[str, object]:
    inventory = _inventory(root)
    paths = frozenset(cast(str, entry["path"]) for entry in inventory)
    expected = _expected_payload_paths(wheel_name)
    if paths != expected:
        raise ResearchReleaseError(
            f"sanitized payload inventory mismatch; missing={sorted(expected - paths)}, "
            f"unexpected={sorted(paths - expected)}"
        )
    target = _TARGETS[target_name]
    return {
        "boundary_sha256": boundary_sha256,
        "entrypoints": list(ENTRYPOINTS),
        "file_count": len(inventory),
        "files": list(inventory),
        "hash_algorithm": "sha256",
        "release_kind": RELEASE_KIND,
        "requirements": {
            "path": REQUIREMENTS_PATH,
            "pins": list(PINNED_REQUIREMENTS),
            "sha256": _sha256_file(source_root / REQUIREMENTS_PATH),
        },
        "schema_version": RELEASE_SCHEMA_VERSION,
        "target": {
            **target,
            "python_abi": PYTHON_ABI,
            "python_implementation": PYTHON_IMPLEMENTATION,
            "python_version": PYTHON_VERSION,
        },
        "wheelhouse": {
            "complete": True,
            "path": WHEELHOUSE_PATH,
            "wheels": [wheel_name],
        },
    }


def verify_release(root: Path, *, enforce_host: bool = False) -> Mapping[str, object]:
    root = root.resolve()
    manifest_path = root / MANIFEST_NAME
    digest_path = root / MANIFEST_DIGEST_NAME
    _require_regular_file(manifest_path, label="research release manifest")
    _require_regular_file(digest_path, label="research release manifest digest")
    manifest_bytes = manifest_path.read_bytes()
    try:
        decoded = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResearchReleaseError("research release manifest is not valid UTF-8 JSON") from exc
    if not isinstance(decoded, dict) or _canonical_json(decoded) != manifest_bytes:
        raise ResearchReleaseError("research release manifest is not canonical JSON")
    manifest = cast(dict[str, object], decoded)
    manifest_keys = {
        "boundary_sha256",
        "entrypoints",
        "file_count",
        "files",
        "hash_algorithm",
        "release_kind",
        "requirements",
        "schema_version",
        "target",
        "wheelhouse",
    }
    if set(manifest) != manifest_keys:
        raise ResearchReleaseError("research release manifest schema is not exact")
    expected_digest = f"{_sha256_bytes(manifest_bytes)}  {MANIFEST_NAME}\n"
    if digest_path.read_text(encoding="ascii") != expected_digest:
        raise ResearchReleaseError("research release manifest digest mismatch")
    if manifest.get("schema_version") != RELEASE_SCHEMA_VERSION or manifest.get("release_kind") != RELEASE_KIND:
        raise ResearchReleaseError("unsupported research release manifest")
    if manifest.get("hash_algorithm") != "sha256":
        raise ResearchReleaseError("research release must use SHA-256")
    boundary_sha256 = manifest.get("boundary_sha256")
    if (
        not isinstance(boundary_sha256, str)
        or len(boundary_sha256) != 64
        or any(character not in "0123456789abcdef" for character in boundary_sha256)
    ):
        raise ResearchReleaseError("research release boundary digest is invalid")
    target = manifest.get("target")
    if not isinstance(target, dict) or target.get("python_version") != PYTHON_VERSION:
        raise ResearchReleaseError("research release Python target is invalid")
    if target.get("python_abi") != PYTHON_ABI or target.get("python_implementation") != PYTHON_IMPLEMENTATION:
        raise ResearchReleaseError("research release Python ABI target is invalid")
    matching_targets = [name for name, value in _TARGETS.items() if all(target.get(k) == v for k, v in value.items())]
    if len(matching_targets) != 1:
        raise ResearchReleaseError("research release operating-system target is invalid")
    target_name = matching_targets[0]
    expected_target = {
        **_TARGETS[target_name],
        "python_abi": PYTHON_ABI,
        "python_implementation": PYTHON_IMPLEMENTATION,
        "python_version": PYTHON_VERSION,
    }
    if target != expected_target:
        raise ResearchReleaseError("research release target schema is not exact")
    requirements = manifest.get("requirements")
    expected_requirements = {
        "path": REQUIREMENTS_PATH,
        "pins": list(PINNED_REQUIREMENTS),
        "sha256": _sha256_file(root / REQUIREMENTS_PATH),
    }
    if requirements != expected_requirements:
        raise ResearchReleaseError("research release dependency pins are invalid")
    if _dependency_lines(root / REQUIREMENTS_PATH) != PINNED_REQUIREMENTS:
        raise ResearchReleaseError("packaged research dependency pins are invalid")
    wheelhouse = manifest.get("wheelhouse")
    if not isinstance(wheelhouse, dict) or wheelhouse.get("complete") is not True:
        raise ResearchReleaseError("research release wheelhouse is not marked complete")
    wheels = validate_wheelhouse(root / WHEELHOUSE_PATH, target_name)
    if wheelhouse != {"complete": True, "path": WHEELHOUSE_PATH, "wheels": [wheels[0].name]}:
        raise ResearchReleaseError("research release wheel inventory is invalid")
    files = manifest.get("files")
    if not isinstance(files, list) or manifest.get("file_count") != len(files):
        raise ResearchReleaseError("research release file inventory is invalid")
    expected_entries: dict[str, Mapping[str, object]] = {}
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "sha256", "size"}:
            raise ResearchReleaseError("research release file entry is invalid")
        path_value = item.get("path")
        if not isinstance(path_value, str) or PurePosixPath(path_value).as_posix() != path_value:
            raise ResearchReleaseError("research release contains a non-canonical path")
        if path_value.startswith("/") or ".." in PurePosixPath(path_value).parts or path_value in expected_entries:
            raise ResearchReleaseError("research release contains an unsafe or duplicate path")
        expected_entries[path_value] = cast(Mapping[str, object], item)
    actual_inventory = {cast(str, item["path"]): item for item in _inventory(root)}
    if set(actual_inventory) != set(expected_entries):
        raise ResearchReleaseError(
            f"research release inventory mismatch; missing={sorted(set(expected_entries) - set(actual_inventory))}, "
            f"unexpected={sorted(set(actual_inventory) - set(expected_entries))}"
        )
    if set(expected_entries) != _expected_payload_paths(wheels[0].name):
        raise ResearchReleaseError("research release contains content outside the fixed sanitized allowlist")
    for relative, expected in expected_entries.items():
        if actual_inventory[relative] != expected:
            raise ResearchReleaseError(f"research release hash or size mismatch: {relative}")
    if manifest.get("entrypoints") != list(ENTRYPOINTS):
        raise ResearchReleaseError("research release entrypoint inventory is invalid")
    release_boundary_sources = tuple(
        root / Path(relative)
        for relative in (
            *(f"bongus/research/cross_venue/{name}" for name in _CROSS_VENUE_MODULES),
            "bongus/exchanges/hyperliquid_read_only.py",
            *ENTRYPOINTS,
        )
    )
    try:
        verified_boundary_sha256 = assert_research_boundary(release_boundary_sources)
    except ResearchBoundaryViolation as exc:
        raise ResearchReleaseError(str(exc)) from exc
    if boundary_sha256 != verified_boundary_sha256:
        raise ResearchReleaseError("research release boundary digest mismatch")
    if enforce_host:
        machine = platform.machine().casefold()
        normalized_machine = "x86_64" if machine in {"amd64", "x86_64"} else machine
        if platform.system().casefold() != target["os"] or normalized_machine != target["architecture"]:
            raise ResearchReleaseError("research release does not match this installation host")
        if platform.python_version() != PYTHON_VERSION:
            raise ResearchReleaseError(f"installer requires exact Python {PYTHON_VERSION}")
    return manifest


def _write_archive(root: Path, archive_path: Path) -> None:
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    files = tuple(item for item in root.rglob("*") if item.is_file())
    directories = {
        parent.as_posix() + "/" for path in files for parent in path.relative_to(root).parents if parent != Path(".")
    }
    members = sorted({*(path.relative_to(root).as_posix() for path in files), *directories})
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for relative in members:
            is_directory = relative.endswith("/")
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            mode = (
                stat.S_IFDIR | 0o755
                if is_directory
                else stat.S_IFREG | (0o755 if relative == "deployment/Install-BongusResearch.sh" else 0o644)
            )
            info.external_attr = (mode << 16) | (0x10 if is_directory else 0)
            info.flag_bits |= 0x800
            content = b"" if is_directory else (root / Path(relative)).read_bytes()
            archive.writestr(info, content, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def build_release(
    *,
    source_root: Path,
    output: Path,
    archive_path: Path,
    wheelhouse_source: Path | None,
    target_name: str = "linux-x86_64",
) -> Mapping[str, object]:
    source_root = source_root.resolve()
    output = output.resolve()
    archive_path = archive_path.resolve()
    archive_digest_path = archive_path.with_name(archive_path.name + ".sha256")
    if target_name not in _TARGETS:
        raise ResearchReleaseError(f"unsupported research target: {target_name}")
    for destination in (output, archive_path, archive_digest_path):
        if destination.exists() or destination.is_symlink():
            raise ResearchReleaseError(f"refusing to replace existing release output: {destination}")
    if output == source_root or output in source_root.parents:
        raise ResearchReleaseError("release output cannot contain or replace the source repository")
    if archive_path == output or output in archive_path.parents:
        raise ResearchReleaseError("release archive cannot be nested inside the release directory")
    boundary_sources = _validate_source_inventory(source_root)
    boundary_sha256 = assert_research_boundary(boundary_sources)
    output.parent.mkdir(parents=True, exist_ok=True)
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".bongus-research-release-", dir=output.parent))
    temporary_wheelhouse: Path | None = None
    try:
        for relative in _COPIED_FILES:
            mode = 0o755 if relative == "deployment/Install-BongusResearch.sh" else 0o644
            _copy_file(source_root / Path(relative), staging / Path(relative), mode=mode)
        for relative, content in _GENERATED_PACKAGE_FILES.items():
            destination = staging / Path(relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
            os.chmod(destination, 0o644)

        if wheelhouse_source is None:
            temporary_wheelhouse = Path(tempfile.mkdtemp(prefix="bongus-research-wheelhouse-"))
            _download_wheelhouse(temporary_wheelhouse, source_root, target_name)
            selected_wheelhouse = temporary_wheelhouse
        else:
            selected_wheelhouse = wheelhouse_source.resolve()
        wheels = validate_wheelhouse(selected_wheelhouse, target_name)
        destination_wheelhouse = staging / WHEELHOUSE_PATH
        destination_wheelhouse.mkdir(parents=True)
        _copy_file(wheels[0], destination_wheelhouse / wheels[0].name)

        manifest = _manifest(
            staging,
            source_root=source_root,
            target_name=target_name,
            boundary_sha256=boundary_sha256,
            wheel_name=wheels[0].name,
        )
        manifest_bytes = _canonical_json(manifest)
        (staging / MANIFEST_NAME).write_bytes(manifest_bytes)
        (staging / MANIFEST_DIGEST_NAME).write_text(
            f"{_sha256_bytes(manifest_bytes)}  {MANIFEST_NAME}\n", encoding="ascii", newline="\n"
        )
        verify_release(staging)
        os.replace(staging, output)
        _write_archive(output, archive_path)
        archive_digest_path.write_text(
            f"{_sha256_file(archive_path)}  {archive_path.name}\n", encoding="ascii", newline="\n"
        )
        return verify_release(output)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
        if temporary_wheelhouse is not None:
            shutil.rmtree(temporary_wheelhouse)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=_REPOSITORY_ROOT)
    parser.add_argument("--output", type=Path, default=Path("dist/bongus-research-linux-x86_64"))
    parser.add_argument("--archive", type=Path)
    parser.add_argument(
        "--wheelhouse-source",
        type=Path,
        help="already-downloaded exact target wheelhouse; omission downloads on the build host",
    )
    parser.add_argument("--target", choices=tuple(_TARGETS), default="linux-x86_64")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    archive = args.archive or args.output.with_name(args.output.name + ".zip")
    manifest = build_release(
        source_root=args.source_root,
        output=args.output,
        archive_path=archive,
        wheelhouse_source=args.wheelhouse_source,
        target_name=args.target,
    )
    print(
        json.dumps(
            {
                "archive": str(archive.resolve()),
                "archive_sha256": _sha256_file(archive.resolve()),
                "file_count": manifest["file_count"],
                "output": str(args.output.resolve()),
                "release_kind": manifest["release_kind"],
                "target": args.target,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
