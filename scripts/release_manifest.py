"""Create and verify content-addressed offline release packages.

The release manifest deliberately contains no wall-clock timestamp or absolute
path.  Given identical staged bytes and the same source/toolchain identifiers,
it is byte-for-byte stable.  The verifier rejects extra files, missing files,
links/reparse points, path traversal, and changed file content.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Iterator
from email.parser import Parser
from pathlib import Path, PurePosixPath
from typing import Any


MANIFEST_FILENAME = "release-manifest.json"
SCHEMA_VERSION = 2
APPLICATION_MAX_BYTES = 200_000_000
PYTHON_RUNTIME_MAX_BYTES = 600_000_000
MINIMUM_FREE_AFTER_INSTALL_BYTES = 4_000_000_000
TOTAL_RUNTIME_MEMORY_MAX_BYTES = 20_000_000_000
TOTAL_RUNTIME_STORAGE_MAX_BYTES = 20_000_000_000
_REPARSE_POINT_ATTRIBUTE = 0x0400
_HASH_CHUNK_BYTES = 1024 * 1024
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_EXACT_REQUIREMENT = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[^\s;]+)$"
)
_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_AUTHENTICODE_THUMBPRINT = re.compile(r"^[0-9A-F]{40}$")
_SIGNING_KEY_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PE_MACHINES = {0x8664: "x86_64", 0xAA64: "arm64"}
_ELF_MACHINES = {0x3E: "x86_64", 0xB7: "arm64"}
_PRODUCTION_PROCESS_NAMES = frozenset(
    {"rust", "trader", "dashboard", "supervisor", "telegram", "scraper"}
)


class ReleaseManifestError(ValueError):
    """Raised when a release directory violates the package contract."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _is_link_or_reparse(path: Path, *, entry: os.DirEntry[str] | None = None) -> bool:
    if entry is not None and entry.is_symlink():
        return True
    if path.is_symlink():
        return True
    metadata = entry.stat(follow_symlinks=False) if entry is not None else path.lstat()
    return bool(getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT_ATTRIBUTE)


def _walk_safe_files(directory: Path) -> Iterator[Path]:
    """Yield regular files without ever following links or reparse points."""

    with os.scandir(directory) as entries:
        for entry in sorted(entries, key=lambda value: value.name.casefold()):
            path = Path(entry.path)
            if _is_link_or_reparse(path, entry=entry):
                raise ReleaseManifestError(f"links/reparse points are forbidden: {path}")
            if entry.is_dir(follow_symlinks=False):
                yield from _walk_safe_files(path)
            elif entry.is_file(follow_symlinks=False):
                yield path
            else:
                raise ReleaseManifestError(f"non-regular package entry is forbidden: {path}")


def package_files(root: Path, *, include_manifest: bool = False) -> tuple[Path, ...]:
    resolved = root.resolve(strict=True)
    if not resolved.is_dir():
        raise ReleaseManifestError(f"release root is not a directory: {resolved}")
    if _is_link_or_reparse(resolved):
        raise ReleaseManifestError(f"release root cannot be a link/reparse point: {resolved}")
    files = []
    for path in _walk_safe_files(resolved):
        relative = path.relative_to(resolved).as_posix()
        if not include_manifest and relative == MANIFEST_FILENAME:
            continue
        files.append(path)
    return tuple(sorted(files, key=lambda value: value.relative_to(resolved).as_posix()))


def _safe_relative_path(raw_path: object) -> PurePosixPath:
    if not isinstance(raw_path, str) or not raw_path:
        raise ReleaseManifestError("manifest file path must be a non-empty string")
    normalized = PurePosixPath(raw_path)
    if normalized.is_absolute() or ".." in normalized.parts or "\\" in raw_path:
        raise ReleaseManifestError(f"unsafe manifest path: {raw_path!r}")
    if normalized.as_posix() != raw_path or normalized == PurePosixPath(MANIFEST_FILENAME):
        raise ReleaseManifestError(f"non-canonical manifest path: {raw_path!r}")
    return normalized


def _canonical_project_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _runtime_requirements(path: Path) -> dict[str, str]:
    requirements: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _EXACT_REQUIREMENT.fullmatch(line)
        if match is None:
            raise ReleaseManifestError(
                "runtime requirement must be one exact, marker-free pin at "
                f"{path.name}:{line_number}: {line!r}"
            )
        name = _canonical_project_name(match.group("name"))
        version = match.group("version")
        if name in requirements:
            raise ReleaseManifestError(f"duplicate runtime requirement: {name}")
        requirements[name] = version
    if not requirements:
        raise ReleaseManifestError("requirements-runtime.txt contains no pinned packages")
    return requirements


def _wheel_identity(path: Path) -> tuple[str, str]:
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            metadata_names = [
                name
                for name in names
                if name.casefold().endswith(".dist-info/metadata")
            ]
            if len(metadata_names) != 1:
                raise ReleaseManifestError(
                    f"wheel must contain exactly one dist-info/METADATA: {path.name}"
                )
            metadata_name = metadata_names[0]
            dist_info = metadata_name.rsplit("/", 1)[0]
            for required_member in (f"{dist_info}/WHEEL", f"{dist_info}/RECORD"):
                if required_member not in names:
                    raise ReleaseManifestError(
                        f"wheel is missing {required_member}: {path.name}"
                    )
            metadata = Parser().parsestr(
                archive.read(metadata_name).decode("utf-8", errors="strict")
            )
    except (OSError, UnicodeError, zipfile.BadZipFile) as exc:
        raise ReleaseManifestError(f"invalid wheel {path.name}: {exc}") from exc
    raw_name = metadata.get("Name")
    version = metadata.get("Version")
    if not raw_name or not version:
        raise ReleaseManifestError(f"wheel metadata lacks Name/Version: {path.name}")
    return _canonical_project_name(raw_name), version


def _offline_wheelhouse(
    requirements_path: Path,
    wheelhouse: Path,
) -> tuple[tuple[Path, ...], dict[str, str]]:
    requirements = _runtime_requirements(requirements_path)
    if not wheelhouse.exists():
        return (), requirements
    if not wheelhouse.is_dir() or _is_link_or_reparse(wheelhouse):
        raise ReleaseManifestError("wheelhouse must be a regular contained directory")
    entries = package_files(wheelhouse)
    non_wheels = [path.name for path in entries if path.suffix.casefold() != ".whl"]
    if non_wheels:
        raise ReleaseManifestError(f"wheelhouse contains non-wheel files: {non_wheels}")
    available: set[tuple[str, str]] = set()
    for wheel in entries:
        identity = _wheel_identity(wheel)
        if identity in available:
            raise ReleaseManifestError(
                "wheelhouse contains duplicate distributions for "
                f"{identity[0]}=={identity[1]}"
            )
        available.add(identity)
    unexpected = [
        f"{name}=={version}"
        for name, version in sorted(available)
        if requirements.get(name) != version
    ]
    if unexpected:
        raise ReleaseManifestError(
            "wheelhouse contains non-runtime or unpinned distributions: "
            + ", ".join(unexpected)
        )
    missing = [
        f"{name}=={version}"
        for name, version in sorted(requirements.items())
        if (name, version) not in available
    ]
    if entries and missing:
        raise ReleaseManifestError(
            "offline wheelhouse is incomplete for pinned runtime requirements: "
            + ", ".join(missing)
        )
    return tuple(entries), requirements


def inspect_pe_executable(path: Path) -> str:
    """Return the supported PE machine name or reject non-executable bytes."""

    size = path.stat().st_size
    if size < 512:
        raise ReleaseManifestError(f"Rust binary is too small to be a PE executable: {path}")
    with path.open("rb") as stream:
        dos_header = stream.read(64)
        if dos_header[:2] != b"MZ":
            raise ReleaseManifestError(f"Rust binary does not have a DOS/PE header: {path}")
        pe_offset = struct.unpack_from("<I", dos_header, 0x3C)[0]
        if pe_offset < 64 or pe_offset > size - 24:
            raise ReleaseManifestError(f"Rust binary has an invalid PE offset: {path}")
        stream.seek(pe_offset)
        coff = stream.read(24)
        if coff[:4] != b"PE\0\0":
            raise ReleaseManifestError(f"Rust binary does not have a PE signature: {path}")
        machine = struct.unpack_from("<H", coff, 4)[0]
        section_count = struct.unpack_from("<H", coff, 6)[0]
        optional_header_size = struct.unpack_from("<H", coff, 20)[0]
        characteristics = struct.unpack_from("<H", coff, 22)[0]
        if machine not in _PE_MACHINES:
            raise ReleaseManifestError(f"unsupported Rust PE machine 0x{machine:04x}: {path}")
        if section_count < 1 or not characteristics & 0x0002:
            raise ReleaseManifestError(f"Rust PE is not marked executable: {path}")
        if optional_header_size < 112:
            raise ReleaseManifestError(f"Rust PE optional header is incomplete: {path}")
        section_table_offset = pe_offset + 24 + optional_header_size
        if section_table_offset + (section_count * 40) > size:
            raise ReleaseManifestError(f"Rust PE section table is out of bounds: {path}")
        optional_header = stream.read(optional_header_size)
        if struct.unpack_from("<H", optional_header, 0)[0] != 0x20B:
            raise ReleaseManifestError(f"Rust executable must use a PE32+ header: {path}")
        entry_point = struct.unpack_from("<I", optional_header, 16)[0]
        section_alignment = struct.unpack_from("<I", optional_header, 32)[0]
        file_alignment = struct.unpack_from("<I", optional_header, 36)[0]
        size_of_image = struct.unpack_from("<I", optional_header, 56)[0]
        size_of_headers = struct.unpack_from("<I", optional_header, 60)[0]
        if (
            entry_point <= 0
            or entry_point >= size_of_image
            or section_alignment <= 0
            or file_alignment <= 0
            or size_of_headers <= 0
            or size_of_headers > size
            or size_of_image <= size_of_headers
        ):
            raise ReleaseManifestError(f"Rust PE optional header is inconsistent: {path}")
        stream.seek(section_table_offset)
        sections = [stream.read(40) for _ in range(section_count)]
    executable_entry_section = False
    for section in sections:
        virtual_size = struct.unpack_from("<I", section, 8)[0]
        virtual_address = struct.unpack_from("<I", section, 12)[0]
        raw_size = struct.unpack_from("<I", section, 16)[0]
        raw_offset = struct.unpack_from("<I", section, 20)[0]
        section_characteristics = struct.unpack_from("<I", section, 36)[0]
        mapped_size = max(virtual_size, raw_size)
        contains_entry = virtual_address <= entry_point < virtual_address + mapped_size
        raw_bytes_bounded = raw_size > 0 and raw_offset + raw_size <= size
        if section_characteristics & 0x20000000 and contains_entry and raw_bytes_bounded:
            executable_entry_section = True
            break
    if not executable_entry_section:
        raise ReleaseManifestError(
            f"Rust PE entry point is not backed by an executable section: {path}"
        )
    return _PE_MACHINES[machine]


def inspect_elf_executable(path: Path) -> str:
    """Return the supported ELF machine name or reject unsafe/non-native bytes."""

    size = path.stat().st_size
    if size < 120:
        raise ReleaseManifestError(f"Rust binary is too small to be an ELF executable: {path}")
    with path.open("rb") as stream:
        header = stream.read(64)
        if header[:4] != b"\x7fELF":
            raise ReleaseManifestError(f"Rust binary does not have an ELF header: {path}")
        if header[4] != 2 or header[5] != 1 or header[6] != 1:
            raise ReleaseManifestError(
                f"Rust ELF must be 64-bit, little-endian, version 1: {path}"
            )
        executable_type, machine, version = struct.unpack_from("<HHI", header, 16)
        entry_point, program_offset = struct.unpack_from("<QQ", header, 24)
        header_size, program_entry_size, program_count = struct.unpack_from(
            "<HHH", header, 52
        )
        if executable_type not in {2, 3} or machine not in _ELF_MACHINES or version != 1:
            raise ReleaseManifestError(f"unsupported Rust ELF header: {path}")
        if entry_point <= 0 or header_size != 64 or program_entry_size < 56 or program_count < 1:
            raise ReleaseManifestError(f"Rust ELF executable header is inconsistent: {path}")
        if program_offset < header_size or program_offset + program_entry_size * program_count > size:
            raise ReleaseManifestError(f"Rust ELF program table is out of bounds: {path}")
        stream.seek(program_offset)
        program_headers = [stream.read(program_entry_size) for _ in range(program_count)]

    for program in program_headers:
        program_type, flags = struct.unpack_from("<II", program, 0)
        file_offset, virtual_address = struct.unpack_from("<QQ", program, 8)
        file_size, memory_size = struct.unpack_from("<QQ", program, 32)
        contains_entry = virtual_address <= entry_point < virtual_address + memory_size
        file_bytes_bounded = file_size > 0 and file_offset + file_size <= size
        if program_type == 1 and flags & 0x1 and contains_entry and file_bytes_bounded:
            return _ELF_MACHINES[machine]
    raise ReleaseManifestError(
        f"Rust ELF entry point is not backed by an executable load segment: {path}"
    )


def inspect_executable(path: Path) -> tuple[str, str]:
    """Inspect the native Rust executable without trusting its filename suffix."""

    with path.open("rb") as stream:
        magic = stream.read(4)
    if magic[:2] == b"MZ":
        return "pe", inspect_pe_executable(path)
    if magic == b"\x7fELF":
        return "elf", inspect_elf_executable(path)
    raise ReleaseManifestError(f"Rust binary is neither a supported PE nor ELF executable: {path}")


def _linux_public_key_fingerprint(public_key: Path) -> str:
    try:
        result = subprocess.run(
            ["openssl", "pkey", "-pubin", "-in", str(public_key), "-outform", "DER"],
            check=False,
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReleaseManifestError(f"cannot inspect Linux release public key: {exc}") from exc
    if result.returncode != 0 or not result.stdout:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ReleaseManifestError(f"invalid Linux release public key: {detail}")
    return hashlib.sha256(result.stdout).hexdigest()


def _verify_linux_detached_signature(
    binary: Path,
    signature: Path,
    public_key: Path,
) -> None:
    try:
        result = subprocess.run(
            [
                "openssl",
                "dgst",
                "-sha256",
                "-verify",
                str(public_key),
                "-signature",
                str(signature),
                str(binary),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReleaseManifestError(f"cannot verify Linux Rust signature: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stdout + result.stderr).strip()
        raise ReleaseManifestError(f"Linux Rust detached signature is invalid: {detail}")


def _module_target_path(root: Path, raw_target: object, *, asgi: bool) -> Path:
    if not isinstance(raw_target, str) or not raw_target:
        raise ReleaseManifestError("Python module target must be a non-empty string")
    module, separator, attribute = raw_target.partition(":")
    if asgi:
        if separator != ":" or not attribute.isidentifier():
            raise ReleaseManifestError(f"invalid ASGI target: {raw_target!r}")
    elif separator:
        raise ReleaseManifestError(f"Python module target cannot contain ':': {raw_target!r}")
    parts = module.split(".")
    if not parts or any(not part.isidentifier() for part in parts):
        raise ReleaseManifestError(f"invalid Python module target: {raw_target!r}")
    module_path = root.joinpath(*parts).with_suffix(".py")
    package_path = root.joinpath(*parts, "__init__.py")
    for candidate in (module_path, package_path):
        if candidate.is_file() and not _is_link_or_reparse(candidate):
            return candidate
    raise ReleaseManifestError(f"packaged Python module is missing: {module}")


def _load_process_manifest(root: Path) -> tuple[str, str, frozenset[str]]:
    relative = PurePosixPath("bongus/runtime/process_manifest.json")
    path = root.joinpath(*relative.parts)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        processes = payload["processes"]
        rust = processes["rust"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ReleaseManifestError(f"invalid packaged process manifest: {exc}") from exc
    if (
        payload.get("schema_version") != 1
        or not isinstance(processes, dict)
        or not isinstance(rust, dict)
        or rust.get("kind") != "binary"
    ):
        raise ReleaseManifestError("packaged Rust process must be a schema-v1 binary")
    binary_targets: dict[str, PurePosixPath] = {}
    for name, raw_spec in processes.items():
        if not isinstance(name, str) or not isinstance(raw_spec, dict):
            raise ReleaseManifestError("process manifest entries must be named objects")
        kind = raw_spec.get("kind")
        target_value = raw_spec.get("target")
        if kind == "python_module":
            _module_target_path(root, target_value, asgi=False)
        elif kind == "asgi":
            _module_target_path(root, target_value, asgi=True)
        elif kind == "python_script":
            script = _safe_relative_path(target_value)
            script_path = root.joinpath(*script.parts)
            if not script_path.is_file() or _is_link_or_reparse(script_path):
                raise ReleaseManifestError(f"packaged Python script is missing: {script}")
        elif kind == "binary":
            target = _safe_relative_path(target_value)
            candidates = [target]
            if not target.suffix:
                candidates.append(PurePosixPath(f"{target.as_posix()}.exe"))
            existing = [
                candidate
                for candidate in candidates
                if root.joinpath(*candidate.parts).is_file()
                and not _is_link_or_reparse(root.joinpath(*candidate.parts))
            ]
            if len(existing) != 1:
                raise ReleaseManifestError(
                    f"packaged binary must resolve exactly once for {name}: {candidates}"
                )
            executable = existing[0]
            executable_path = root.joinpath(*executable.parts)
            inspect_executable(executable_path)
            binary_targets[name] = executable
        else:
            raise ReleaseManifestError(f"unsupported process kind for {name}: {kind!r}")
    executable = binary_targets["rust"]
    return relative.as_posix(), executable.as_posix(), frozenset(processes)


def _atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_descriptor: int | None = None
        try:
            directory_descriptor = os.open(path.parent, os.O_RDONLY)
            os.fsync(directory_descriptor)
        except OSError:
            # Windows does not generally expose directory fsync. The file
            # itself has already been flushed and atomically replaced.
            pass
        finally:
            if directory_descriptor is not None:
                os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def create_manifest(
    root: Path,
    *,
    source_revision: str,
    python_version: str,
    rust_toolchain: str,
    require_wheelhouse: bool = True,
    production_eligible: bool = False,
    rust_signature_status: str = "NotChecked",
    rust_signer_thumbprint: str = "",
    rust_signer_subject: str = "",
    rust_signature_scheme: str = "",
    rust_signer_fingerprint: str = "",
    rust_signature_path: str = "",
    rust_public_key_path: str = "",
) -> dict[str, Any]:
    root = root.resolve(strict=True)
    process_manifest_path, rust_binary_path, process_names = _load_process_manifest(root)
    requirements_path = root / "requirements-runtime.txt"
    if not requirements_path.is_file() or _is_link_or_reparse(requirements_path):
        raise ReleaseManifestError("requirements-runtime.txt is missing")

    wheelhouse = root / "wheelhouse"
    wheel_files, requirements = _offline_wheelhouse(requirements_path, wheelhouse)
    if require_wheelhouse and not wheel_files:
        raise ReleaseManifestError(
            "offline release requires a complete wheel for every pinned runtime requirement"
        )
    if production_eligible and not wheel_files:
        raise ReleaseManifestError("production release must be offline installable")
    if production_eligible and not _PRODUCTION_PROCESS_NAMES <= process_names:
        missing = sorted(_PRODUCTION_PROCESS_NAMES - process_names)
        raise ReleaseManifestError(
            f"production process manifest is incomplete: missing={missing}"
        )
    rust_executable = root.joinpath(*PurePosixPath(rust_binary_path).parts)
    executable_format, rust_machine = inspect_executable(rust_executable)
    release_platform = "windows" if executable_format == "pe" else "linux"
    normalized_signature_status = str(rust_signature_status).strip()
    normalized_thumbprint = str(rust_signer_thumbprint).strip().upper()
    normalized_subject = str(rust_signer_subject).strip()
    normalized_scheme = str(rust_signature_scheme).strip().lower()
    normalized_fingerprint = str(rust_signer_fingerprint).strip().lower()
    signature_relative = (
        _safe_relative_path(rust_signature_path) if rust_signature_path else None
    )
    public_key_relative = (
        _safe_relative_path(rust_public_key_path) if rust_public_key_path else None
    )
    if executable_format == "pe":
        normalized_scheme = normalized_scheme or "authenticode"
        normalized_fingerprint = normalized_fingerprint or normalized_thumbprint.lower()
        if production_eligible and (
            normalized_signature_status != "Valid"
            or normalized_scheme != "authenticode"
            or _AUTHENTICODE_THUMBPRINT.fullmatch(normalized_thumbprint) is None
            or not normalized_subject
        ):
            raise ReleaseManifestError(
                "production Rust binary requires a valid Authenticode signer record"
            )
    else:
        normalized_scheme = normalized_scheme or "openssl-sha256"
        if production_eligible and (
            normalized_signature_status != "Valid"
            or normalized_scheme != "openssl-sha256"
            or _SIGNING_KEY_SHA256.fullmatch(normalized_fingerprint) is None
            or not normalized_subject
            or signature_relative is None
            or public_key_relative is None
        ):
            raise ReleaseManifestError(
                "production Linux Rust binary requires a valid detached signer record"
            )
        if signature_relative is not None and public_key_relative is not None:
            signature_file = root.joinpath(*signature_relative.parts)
            public_key_file = root.joinpath(*public_key_relative.parts)
            if not signature_file.is_file() or not public_key_file.is_file():
                raise ReleaseManifestError("Linux signature/public-key files are missing")
            observed_fingerprint = _linux_public_key_fingerprint(public_key_file)
            if observed_fingerprint != normalized_fingerprint:
                raise ReleaseManifestError("Linux release public-key fingerprint mismatch")
            _verify_linux_detached_signature(rust_executable, signature_file, public_key_file)

    file_records: list[dict[str, object]] = []
    total_bytes = 0
    application_bytes = 0
    wheelhouse_bytes = 0
    staged_files = package_files(root)
    for path in staged_files:
        relative = path.relative_to(root).as_posix()
        size = path.stat().st_size
        total_bytes += size
        application_bytes += size
        if relative.startswith("wheelhouse/"):
            wheelhouse_bytes += size
    if application_bytes > APPLICATION_MAX_BYTES:
        raise ReleaseManifestError(
            "application release exceeds its 200 MB hard budget: "
            f"observed={application_bytes}, limit={APPLICATION_MAX_BYTES}"
        )
    for path in staged_files:
        relative = path.relative_to(root).as_posix()
        size = path.stat().st_size
        file_records.append(
            {"path": relative, "sha256": sha256_file(path), "size_bytes": size}
        )

    binary_record = next(
        (record for record in file_records if record["path"] == rust_binary_path), None
    )
    if binary_record is None:
        raise ReleaseManifestError("Rust binary was not included in the file inventory")
    rust_binary_record = dict(binary_record)
    rust_binary_record.update(
        {
            "platform": release_platform,
            "executable_format": executable_format,
            "machine": rust_machine,
        }
    )
    rust_binary_record["signature"] = {
        "scheme": normalized_scheme,
        "status": normalized_signature_status,
        "signer_fingerprint": normalized_fingerprint,
        "signer_subject": normalized_subject,
        "signature_path": signature_relative.as_posix() if signature_relative else "",
        "public_key_path": public_key_relative.as_posix() if public_key_relative else "",
    }
    if executable_format == "pe":
        rust_binary_record["pe_machine"] = rust_machine
        rust_binary_record["authenticode"] = {
            "status": normalized_signature_status,
            "signer_thumbprint": normalized_thumbprint,
            "signer_subject": normalized_subject,
        }

    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "hash_algorithm": "sha256",
        "production_eligible": bool(production_eligible),
        "source_revision": source_revision,
        "toolchains": {
            "python": python_version,
            "rust": rust_toolchain,
        },
        "offline_installable": bool(wheel_files),
        "runtime_requirement_count": len(requirements),
        "process_manifest_path": process_manifest_path,
        "runtime_requirements_path": "requirements-runtime.txt",
        "rust_binary": rust_binary_record,
        "size_contract": {
            "application_bytes": application_bytes,
            "application_max_bytes": APPLICATION_MAX_BYTES,
            "wheelhouse_bytes": wheelhouse_bytes,
            "python_runtime_max_bytes": PYTHON_RUNTIME_MAX_BYTES,
            "minimum_free_after_install_bytes": MINIMUM_FREE_AFTER_INSTALL_BYTES,
            "total_runtime_memory_max_bytes": TOTAL_RUNTIME_MEMORY_MAX_BYTES,
            "total_runtime_storage_max_bytes": TOTAL_RUNTIME_STORAGE_MAX_BYTES,
        },
        "file_count": len(file_records),
        "total_bytes": total_bytes,
        "files": file_records,
    }
    _atomic_json_write(root / MANIFEST_FILENAME, manifest)
    return manifest


def verify_manifest(
    root: Path,
    *,
    require_offline: bool = False,
    require_production: bool = False,
) -> dict[str, Any]:
    root = root.resolve(strict=True)
    manifest_path = root / MANIFEST_FILENAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseManifestError(f"cannot read release manifest: {exc}") from exc
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ReleaseManifestError("unsupported release manifest schema")
    if manifest.get("hash_algorithm") != "sha256":
        raise ReleaseManifestError("release manifest must use SHA-256")
    production_eligible = manifest.get("production_eligible")
    if not isinstance(production_eligible, bool):
        raise ReleaseManifestError("release production_eligible flag must be boolean")
    if require_production and not production_eligible:
        raise ReleaseManifestError("development-only release is not production eligible")
    if require_offline and manifest.get("offline_installable") is not True:
        raise ReleaseManifestError("release does not contain an offline wheelhouse")

    raw_records = manifest.get("files")
    if not isinstance(raw_records, list):
        raise ReleaseManifestError("manifest files must be a list")
    expected: dict[str, tuple[int, str]] = {}
    for raw_record in raw_records:
        if not isinstance(raw_record, dict):
            raise ReleaseManifestError("manifest file records must be objects")
        relative = _safe_relative_path(raw_record.get("path")).as_posix()
        if relative in expected:
            raise ReleaseManifestError(f"duplicate manifest file: {relative}")
        size = raw_record.get("size_bytes")
        digest = raw_record.get("sha256")
        if not isinstance(size, int) or size < 0:
            raise ReleaseManifestError(f"invalid size for {relative}")
        if (
            not isinstance(digest, str)
            or _HEX_SHA256.fullmatch(digest) is None
        ):
            raise ReleaseManifestError(f"invalid SHA-256 for {relative}")
        expected[relative] = (size, digest)

    actual_files = package_files(root)
    actual = {path.relative_to(root).as_posix(): path for path in actual_files}
    missing = sorted(set(expected) - set(actual))
    unexpected = sorted(set(actual) - set(expected))
    if missing or unexpected:
        raise ReleaseManifestError(
            f"release inventory mismatch: missing={missing}, unexpected={unexpected}"
        )
    for relative, (expected_size, expected_digest) in expected.items():
        path = actual[relative]
        if path.stat().st_size != expected_size:
            raise ReleaseManifestError(f"size mismatch: {relative}")
        if sha256_file(path) != expected_digest:
            raise ReleaseManifestError(f"SHA-256 mismatch: {relative}")

    if manifest.get("file_count") != len(expected):
        raise ReleaseManifestError("manifest file_count does not match inventory")
    if manifest.get("total_bytes") != sum(size for size, _ in expected.values()):
        raise ReleaseManifestError("manifest total_bytes does not match inventory")
    process_manifest_path, rust_binary_path, process_names = _load_process_manifest(root)
    if manifest.get("process_manifest_path") != process_manifest_path:
        raise ReleaseManifestError("process manifest path does not match package")
    if manifest.get("runtime_requirements_path") != "requirements-runtime.txt":
        raise ReleaseManifestError("runtime requirements path does not match package")
    requirements_path = root / "requirements-runtime.txt"
    wheel_files, requirements = _offline_wheelhouse(requirements_path, root / "wheelhouse")
    offline_installable = bool(wheel_files)
    if manifest.get("offline_installable") is not offline_installable:
        raise ReleaseManifestError("offline_installable does not match wheelhouse coverage")
    if manifest.get("runtime_requirement_count") != len(requirements):
        raise ReleaseManifestError("runtime requirement count does not match pinned set")
    if require_offline and not offline_installable:
        raise ReleaseManifestError("release wheelhouse is incomplete")
    if production_eligible and not _PRODUCTION_PROCESS_NAMES <= process_names:
        missing_processes = sorted(_PRODUCTION_PROCESS_NAMES - process_names)
        raise ReleaseManifestError(
            f"production process manifest is incomplete: missing={missing_processes}"
        )
    rust_record = manifest.get("rust_binary")
    if not isinstance(rust_record, dict) or rust_record.get("path") != rust_binary_path:
        raise ReleaseManifestError("Rust binary record does not match process manifest")
    if expected.get(rust_binary_path) != (
        rust_record.get("size_bytes"),
        rust_record.get("sha256"),
    ):
        raise ReleaseManifestError("Rust binary record does not match file inventory")
    binary_path = root.joinpath(*PurePosixPath(rust_binary_path).parts)
    executable_format, rust_machine = inspect_executable(binary_path)
    release_platform = "windows" if executable_format == "pe" else "linux"
    if (
        rust_record.get("platform") != release_platform
        or rust_record.get("executable_format") != executable_format
        or rust_record.get("machine") != rust_machine
    ):
        raise ReleaseManifestError("Rust executable platform record does not match binary")
    signature = rust_record.get("signature")
    if not isinstance(signature, dict):
        raise ReleaseManifestError("Rust binary signature record is missing")
    signature_status = signature.get("status")
    signature_scheme = signature.get("scheme")
    signer_fingerprint = signature.get("signer_fingerprint")
    signer_subject = signature.get("signer_subject")
    if not all(
        isinstance(value, str)
        for value in (signature_status, signature_scheme, signer_fingerprint, signer_subject)
    ):
        raise ReleaseManifestError("Rust binary signature record is malformed")
    if executable_format == "pe":
        authenticode = rust_record.get("authenticode")
        if not isinstance(authenticode, dict):
            raise ReleaseManifestError("Rust binary Authenticode record is missing")
        signer_thumbprint = authenticode.get("signer_thumbprint")
        if (
            signature_scheme != "authenticode"
            or signer_fingerprint != str(signer_thumbprint or "").lower()
            or signature_status != authenticode.get("status")
            or signer_subject != authenticode.get("signer_subject")
        ):
            raise ReleaseManifestError(
                "generic signature record disagrees with Authenticode record"
            )
        if production_eligible and (
            signature_status != "Valid"
            or _AUTHENTICODE_THUMBPRINT.fullmatch(str(signer_thumbprint)) is None
            or not str(signer_subject).strip()
            or not offline_installable
        ):
            raise ReleaseManifestError(
                "production release lacks a valid Authenticode/offline contract"
            )
    elif production_eligible:
        signature_path = _safe_relative_path(signature.get("signature_path"))
        public_key_path = _safe_relative_path(signature.get("public_key_path"))
        if (
            signature_status != "Valid"
            or signature_scheme != "openssl-sha256"
            or _SIGNING_KEY_SHA256.fullmatch(str(signer_fingerprint)) is None
            or not str(signer_subject).strip()
            or not offline_installable
        ):
            raise ReleaseManifestError(
                "production release lacks a valid Linux detached-signature/offline contract"
            )
        public_key_file = root.joinpath(*public_key_path.parts)
        if _linux_public_key_fingerprint(public_key_file) != signer_fingerprint:
            raise ReleaseManifestError("Linux release public-key fingerprint mismatch")
        _verify_linux_detached_signature(
            binary_path,
            root.joinpath(*signature_path.parts),
            public_key_file,
        )
    size_contract = manifest.get("size_contract")
    application_bytes = sum(size for size, _ in expected.values())
    wheelhouse_bytes = sum(
        size for relative, (size, _) in expected.items() if relative.startswith("wheelhouse/")
    )
    expected_size_contract = {
        "application_bytes": application_bytes,
        "application_max_bytes": APPLICATION_MAX_BYTES,
        "wheelhouse_bytes": wheelhouse_bytes,
        "python_runtime_max_bytes": PYTHON_RUNTIME_MAX_BYTES,
        "minimum_free_after_install_bytes": MINIMUM_FREE_AFTER_INSTALL_BYTES,
        "total_runtime_memory_max_bytes": TOTAL_RUNTIME_MEMORY_MAX_BYTES,
        "total_runtime_storage_max_bytes": TOTAL_RUNTIME_STORAGE_MAX_BYTES,
    }
    if size_contract != expected_size_contract:
        raise ReleaseManifestError("release size contract does not match inventory/policy")
    if application_bytes > APPLICATION_MAX_BYTES:
        raise ReleaseManifestError("application release exceeds its 200 MB hard budget")
    return manifest


def verify_runtime_inventory(
    root: Path,
    *,
    require_production: bool = False,
    expected_linux_signing_key_sha256: str = "",
) -> dict[str, Any]:
    """Verify immutable packaged bytes after installation/runtime mutation.

    The production installer deliberately adds ``.venv`` and the running
    system creates logs, bytecode and bounded runtime files. Those artifacts
    are outside the release manifest. Every manifest-owned file is still
    rehashed, and unexpected files inside executable/source roots are rejected
    except for Python bytecode and the dedicated log directory.
    """

    root = root.resolve(strict=True)
    manifest_path = root / MANIFEST_FILENAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseManifestError(f"cannot read release manifest: {exc}") from exc
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ReleaseManifestError("unsupported release manifest schema")
    if manifest.get("hash_algorithm") != "sha256":
        raise ReleaseManifestError("release manifest must use SHA-256")
    if require_production and manifest.get("production_eligible") is not True:
        raise ReleaseManifestError("development-only release is not production eligible")
    if require_production and manifest.get("offline_installable") is not True:
        raise ReleaseManifestError("production release is not offline installable")

    raw_records = manifest.get("files")
    if not isinstance(raw_records, list):
        raise ReleaseManifestError("manifest files must be a list")
    expected: dict[str, tuple[int, str]] = {}
    for raw_record in raw_records:
        if not isinstance(raw_record, dict):
            raise ReleaseManifestError("manifest file records must be objects")
        relative = _safe_relative_path(raw_record.get("path")).as_posix()
        size = raw_record.get("size_bytes")
        digest = raw_record.get("sha256")
        if relative in expected:
            raise ReleaseManifestError(f"duplicate manifest file: {relative}")
        if not isinstance(size, int) or size < 0:
            raise ReleaseManifestError(f"invalid size for {relative}")
        if not isinstance(digest, str) or _HEX_SHA256.fullmatch(digest) is None:
            raise ReleaseManifestError(f"invalid SHA-256 for {relative}")
        expected[relative] = (size, digest)

    for relative, (expected_size, expected_digest) in expected.items():
        path = root.joinpath(*PurePosixPath(relative).parts)
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise ReleaseManifestError(f"missing manifest file: {relative}") from exc
        if _is_link_or_reparse(path) or not stat.S_ISREG(metadata.st_mode):
            raise ReleaseManifestError(f"manifest file is not regular: {relative}")
        if metadata.st_size != expected_size:
            raise ReleaseManifestError(f"size mismatch: {relative}")
        if sha256_file(path) != expected_digest:
            raise ReleaseManifestError(f"SHA-256 mismatch: {relative}")

    protected_roots = ("bongus", "scripts", "bin")
    for protected_name in protected_roots:
        protected = root / protected_name
        if not protected.exists():
            raise ReleaseManifestError(f"packaged source root is missing: {protected_name}")
        for path in _walk_safe_files(protected):
            relative = path.relative_to(root).as_posix()
            parts = PurePosixPath(relative).parts
            runtime_only = (
                "__pycache__" in parts
                or path.suffix.casefold() in {".pyc", ".pyo"}
                or relative.startswith("scripts/logs/")
            )
            if not runtime_only and relative not in expected:
                raise ReleaseManifestError(
                    f"unexpected executable/source runtime file: {relative}"
                )

    rust_record = manifest.get("rust_binary")
    if not isinstance(rust_record, dict):
        raise ReleaseManifestError("Rust binary record is missing")
    rust_relative = _safe_relative_path(rust_record.get("path")).as_posix()
    if rust_relative not in expected:
        raise ReleaseManifestError("Rust binary is not bound by the file inventory")
    executable_format, machine = inspect_executable(
        root.joinpath(*PurePosixPath(rust_relative).parts)
    )
    if (
        rust_record.get("executable_format") != executable_format
        or rust_record.get("machine") != machine
    ):
        raise ReleaseManifestError("Rust executable record does not match executable")
    signature = rust_record.get("signature")
    if not isinstance(signature, dict):
        raise ReleaseManifestError("Rust binary signature record is missing")
    if executable_format == "elf" and manifest.get("production_eligible") is True:
        raw_signature_path = _safe_relative_path(signature.get("signature_path"))
        raw_public_key_path = _safe_relative_path(signature.get("public_key_path"))
        fingerprint = str(signature.get("signer_fingerprint") or "").strip().lower()
        expected_fingerprint = expected_linux_signing_key_sha256.strip().lower()
        if (
            signature.get("scheme") != "openssl-sha256"
            or signature.get("status") != "Valid"
            or _SIGNING_KEY_SHA256.fullmatch(fingerprint) is None
        ):
            raise ReleaseManifestError("Linux Rust signature record is invalid")
        if expected_fingerprint:
            if _SIGNING_KEY_SHA256.fullmatch(expected_fingerprint) is None:
                raise ReleaseManifestError(
                    "trusted Linux release signing-key fingerprint is malformed"
                )
            if fingerprint != expected_fingerprint:
                raise ReleaseManifestError(
                    "Linux release signing key does not match the operator trust pin"
                )
        elif require_production:
            raise ReleaseManifestError(
                "live Linux runtime requires BONGUS_RELEASE_SIGNING_KEY_SHA256"
            )
        public_key = root.joinpath(*raw_public_key_path.parts)
        if _linux_public_key_fingerprint(public_key) != fingerprint:
            raise ReleaseManifestError("Linux release public-key fingerprint mismatch")
        _verify_linux_detached_signature(
            root.joinpath(*PurePosixPath(rust_relative).parts),
            root.joinpath(*raw_signature_path.parts),
            public_key,
        )
    return manifest


def create_deterministic_archive(root: Path, output: Path) -> tuple[Path, str]:
    root = root.resolve(strict=True)
    verified = verify_manifest(root)
    rust_binary_path = verified["rust_binary"]["path"]
    output = output.resolve(strict=False)
    sidecar = output.with_suffix(f"{output.suffix}.sha256")
    if output.exists() or sidecar.exists():
        raise ReleaseManifestError(f"refusing to replace existing archive: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(
            output,
            mode="x",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
            allowZip64=True,
        ) as archive:
            for path in package_files(root, include_manifest=True):
                relative = path.relative_to(root).as_posix()
                info = zipfile.ZipInfo(relative, date_time=_ZIP_TIMESTAMP)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                executable = relative == rust_binary_path
                mode = 0o755 if executable else 0o644
                info.external_attr = (stat.S_IFREG | mode) << 16
                with path.open("rb") as source, archive.open(info, "w", force_zip64=True) as target:
                    shutil.copyfileobj(source, target, length=_HASH_CHUNK_BYTES)
        digest = sha256_file(output)
        sidecar.write_text(f"{digest}  {output.name}\n", encoding="ascii", newline="\n")
        return output, digest
    except BaseException:
        output.unlink(missing_ok=True)
        sidecar.unlink(missing_ok=True)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    create = subcommands.add_parser("create", help="write a release manifest")
    create.add_argument("root", type=Path)
    create.add_argument("--source-revision", required=True)
    create.add_argument("--python-version", required=True)
    create.add_argument("--rust-toolchain", required=True)
    create.add_argument("--allow-missing-wheelhouse", action="store_true")
    create.add_argument("--production-eligible", action="store_true")
    create.add_argument("--rust-signature-status", default="NotChecked")
    create.add_argument("--rust-signer-thumbprint", default="")
    create.add_argument("--rust-signer-subject", default="")
    create.add_argument("--rust-signature-scheme", default="")
    create.add_argument("--rust-signer-fingerprint", default="")
    create.add_argument("--rust-signature-path", default="")
    create.add_argument("--rust-public-key-path", default="")
    verify = subcommands.add_parser("verify", help="verify an exact release tree")
    verify.add_argument("root", type=Path)
    verify.add_argument("--require-offline", action="store_true")
    verify.add_argument("--require-production", action="store_true")
    archive = subcommands.add_parser("archive", help="create a deterministic verified ZIP")
    archive.add_argument("root", type=Path)
    archive.add_argument("output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "create":
            payload = create_manifest(
                arguments.root,
                source_revision=arguments.source_revision,
                python_version=arguments.python_version,
                rust_toolchain=arguments.rust_toolchain,
                require_wheelhouse=not arguments.allow_missing_wheelhouse,
                production_eligible=arguments.production_eligible,
                rust_signature_status=arguments.rust_signature_status,
                rust_signer_thumbprint=arguments.rust_signer_thumbprint,
                rust_signer_subject=arguments.rust_signer_subject,
                rust_signature_scheme=arguments.rust_signature_scheme,
                rust_signer_fingerprint=arguments.rust_signer_fingerprint,
                rust_signature_path=arguments.rust_signature_path,
                rust_public_key_path=arguments.rust_public_key_path,
            )
            print(json.dumps(payload, sort_keys=True))
        elif arguments.command == "verify":
            payload = verify_manifest(
                arguments.root,
                require_offline=arguments.require_offline,
                require_production=arguments.require_production,
            )
            print(json.dumps(payload, sort_keys=True))
        else:
            output, digest = create_deterministic_archive(arguments.root, arguments.output)
            print(json.dumps({"archive": str(output), "sha256": digest}, sort_keys=True))
        return 0
    except (OSError, ReleaseManifestError) as exc:
        print(f"release package error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
