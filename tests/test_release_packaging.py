from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

import scripts.release_manifest as release_manifest
from scripts.release_manifest import (
    MANIFEST_FILENAME,
    MANIFEST_SIGNATURE_FILENAME,
    REVIEWED_PYTHON_BASELINE,
    WHEELHOUSE_LOCK_FILENAME,
    ReleaseManifestError,
    create_deterministic_archive,
    create_manifest,
    validate_python_compatibility,
    verify_manifest,
    verify_runtime_inventory,
    write_wheelhouse_lock,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _host_meets_reviewed_python_floor() -> bool:
    baseline = (PROJECT_ROOT / ".python-version").read_text(encoding="utf-8").strip()
    try:
        validate_python_compatibility(
            baseline,
            actual=(
                sys.version_info.major,
                sys.version_info.minor,
                sys.version_info.micro,
                sys.version_info.releaselevel,
                sys.version_info.serial,
            ),
        )
    except ReleaseManifestError:
        return False
    return True


def _requirements(path: Path) -> dict[str, Requirement]:
    parsed = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        requirement = Requirement(line)
        parsed[canonicalize_name(requirement.name)] = requirement
    return parsed


def _write_test_pe(path: Path) -> None:
    payload = bytearray(1024)
    payload[:2] = b"MZ"
    struct.pack_into("<I", payload, 0x3C, 0x80)
    payload[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", payload, 0x84, 0x8664)
    struct.pack_into("<H", payload, 0x86, 1)
    struct.pack_into("<H", payload, 0x94, 0xF0)
    struct.pack_into("<H", payload, 0x96, 0x0022)
    optional_header = 0x98
    struct.pack_into("<H", payload, optional_header, 0x20B)
    struct.pack_into("<I", payload, optional_header + 16, 0x1000)
    struct.pack_into("<I", payload, optional_header + 32, 0x1000)
    struct.pack_into("<I", payload, optional_header + 36, 0x200)
    struct.pack_into("<I", payload, optional_header + 56, 0x2000)
    struct.pack_into("<I", payload, optional_header + 60, 0x200)
    section = optional_header + 0xF0
    payload[section : section + 8] = b".text\0\0\0"
    struct.pack_into("<I", payload, section + 8, 0x200)
    struct.pack_into("<I", payload, section + 12, 0x1000)
    struct.pack_into("<I", payload, section + 16, 0x200)
    struct.pack_into("<I", payload, section + 20, 0x200)
    struct.pack_into("<I", payload, section + 36, 0x60000020)
    path.write_bytes(payload)


def _write_test_elf(path: Path) -> None:
    payload = bytearray(512)
    payload[:4] = b"\x7fELF"
    payload[4:7] = bytes((2, 1, 1))
    struct.pack_into("<HHI", payload, 16, 3, 0x3E, 1)
    struct.pack_into("<QQ", payload, 24, 0x400080, 64)
    struct.pack_into("<HHH", payload, 52, 64, 56, 1)
    struct.pack_into("<II", payload, 64, 1, 0x5)
    struct.pack_into("<QQ", payload, 72, 0, 0x400000)
    struct.pack_into("<QQ", payload, 96, len(payload), len(payload))
    path.write_bytes(payload)


def _write_test_wheel(wheelhouse: Path, name: str, version: str) -> Path:
    normalized = name.replace("-", "_")
    wheel = wheelhouse / f"{normalized}-{version}-py3-none-any.whl"
    dist_info = f"{normalized}-{version}.dist-info"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            f"{dist_info}/METADATA",
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n\n",
        )
        archive.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr(f"{dist_info}/RECORD", "")
    return wheel


def _minimal_release(
    root: Path,
    *,
    wheelhouse: bool = True,
    platform: str = "windows",
) -> None:
    process_manifest = root / "bongus" / "runtime" / "process_manifest.json"
    process_manifest.parent.mkdir(parents=True)
    process_manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "canonical_trader": "trader",
                "processes": {
                    "rust": {"kind": "binary", "target": "bin/execution_engine"},
                    "trader": {"kind": "python_module", "target": "scripts.live_trader_v2"},
                    "dashboard": {
                        "kind": "asgi",
                        "target": "bongus.monitoring.web_dashboard:app",
                    },
                    "supervisor": {
                        "kind": "python_module",
                        "target": "bongus.monitoring.supervisor_service",
                    },
                    "telegram": {
                        "kind": "python_script",
                        "target": "bongus/monitoring/telegram_alerter.py",
                    },
                    "scraper": {
                        "kind": "python_script",
                        "target": "bongus/strategies/sentiment_scraper.py",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    binary_name = "execution_engine.exe" if platform == "windows" else "execution_engine"
    binary = root / "bin" / binary_name
    binary.parent.mkdir(parents=True)
    if platform == "windows":
        _write_test_pe(binary)
    else:
        _write_test_elf(binary)
    runtime_stubs = (
        "scripts/__init__.py",
        "scripts/live_trader_v2.py",
        "bongus/monitoring/__init__.py",
        "bongus/monitoring/web_dashboard.py",
        "bongus/monitoring/supervisor_service.py",
        "bongus/monitoring/telegram_alerter.py",
        "bongus/strategies/__init__.py",
        "bongus/strategies/sentiment_scraper.py",
    )
    for relative in runtime_stubs:
        stub = root / relative
        stub.parent.mkdir(parents=True, exist_ok=True)
        stub.write_text("# packaged test stub\n", encoding="utf-8")
    (root / "requirements-runtime.txt").write_text("msgpack==1.1.2\n", encoding="utf-8")
    (root / "app.py").write_text("print('ready')\n", encoding="utf-8")
    if wheelhouse:
        wheels = root / "wheelhouse"
        wheels.mkdir()
        _write_test_wheel(wheels, "msgpack", "1.1.2")
        write_wheelhouse_lock(
            root / "requirements-runtime.txt",
            wheels,
            root / WHEELHOUSE_LOCK_FILENAME,
        )


def test_dependency_tiers_are_exact_and_runtime_excludes_non_runtime_tools() -> None:
    runtime = _requirements(PROJECT_ROOT / "requirements-runtime.txt")
    research = _requirements(PROJECT_ROOT / "requirements-research.txt")
    development = _requirements(PROJECT_ROOT / "requirements-dev.txt")
    compatibility = _requirements(PROJECT_ROOT / "requirements.lock")

    excluded_runtime = {
        "cython",
        "httpx",
        "joblib",
        "pandas",
        "pyarrow",
        "pyright",
        "pytest",
        "pytest-asyncio",
        "pytest-trio",
        "scikit-learn",
        "scipy",
        "trio",
        "rich",
    }
    assert excluded_runtime.isdisjoint(runtime)
    assert {"cython", "joblib", "scikit-learn", "scipy"} <= set(research)
    assert {"httpx", "pyright", "pytest", "pytest-asyncio", "pytest-trio", "trio"} <= set(development)
    assert set(runtime) < set(research) < set(development)

    for tier in (runtime, research, development):
        for name, requirement in tier.items():
            specifications = list(requirement.specifier)
            assert len(specifications) == 1
            assert specifications[0].operator == "=="
            assert str(requirement) == str(compatibility[name])

    runtime_direct = set(_requirements(PROJECT_ROOT / "requirements-runtime.in"))
    research_direct = set(_requirements(PROJECT_ROOT / "requirements-research.in"))
    development_direct = set(_requirements(PROJECT_ROOT / "requirements-dev.in"))
    compatibility_direct = set(_requirements(PROJECT_ROOT / "requirements.txt"))
    assert runtime_direct.isdisjoint(research_direct)
    assert runtime_direct.isdisjoint(development_direct)
    assert research_direct.isdisjoint(development_direct)
    assert compatibility_direct == runtime_direct | research_direct | development_direct


def test_python_patch_floor_accepts_only_same_series_final_upgrades() -> None:
    assert REVIEWED_PYTHON_BASELINE == (PROJECT_ROOT / ".python-version").read_text(encoding="utf-8").strip()
    assert (
        validate_python_compatibility(
            "3.11.15",
            actual=(3, 11, 15, "final", 0),
        )
        == "3.11.15"
    )
    assert (
        validate_python_compatibility(
            "3.11.15",
            actual=(3, 11, 16, "final", 0),
        )
        == "3.11.16"
    )
    with pytest.raises(ReleaseManifestError, match="older"):
        validate_python_compatibility("3.11.15", actual=(3, 11, 14, "final", 0))
    with pytest.raises(ReleaseManifestError, match="major.minor"):
        validate_python_compatibility("3.11.15", actual=(3, 12, 0, "final", 0))
    with pytest.raises(ReleaseManifestError, match="final CPython"):
        validate_python_compatibility("3.11.15", actual=(3, 11, 16, "candidate", 1))
    with pytest.raises(ReleaseManifestError, match="final major.minor.patch"):
        validate_python_compatibility("3.11.15rc1", actual=(3, 11, 16, "final", 0))
    with pytest.raises(ReleaseManifestError, match="reviewed 3.11.15 floor"):
        validate_python_compatibility("3.11.14", actual=(3, 11, 15, "final", 0))


def test_wheelhouse_lock_binds_exact_filename_requirements_and_sha256(tmp_path: Path) -> None:
    root = tmp_path / "release"
    root.mkdir()
    _minimal_release(root)
    lock_path = root / WHEELHOUSE_LOCK_FILENAME
    lock = json.loads(lock_path.read_text(encoding="utf-8"))

    created = create_manifest(
        root,
        source_revision="7" * 40,
        python_version="3.11.15",
        rust_toolchain="1.94.1",
    )
    assert created["wheelhouse_lock"]["status"] == "verified"
    assert created["wheelhouse_lock"]["path"] == WHEELHOUSE_LOCK_FILENAME
    assert created["wheelhouse_lock"]["wheel_count"] == 1

    (root / MANIFEST_FILENAME).unlink()
    lock["wheels"][0]["sha256"] = "0" * 64
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    with pytest.raises(ReleaseManifestError, match="wheelhouse lock SHA-256 mismatch"):
        create_manifest(
            root,
            source_revision="7" * 40,
            python_version="3.11.15",
            rust_toolchain="1.94.1",
        )

    lock_path.unlink()
    write_wheelhouse_lock(
        root / "requirements-runtime.txt",
        root / "wheelhouse",
        lock_path,
    )
    requirements_mismatch = json.loads(lock_path.read_text(encoding="utf-8"))
    requirements_mismatch["requirements_sha256"] = "0" * 64
    lock_path.write_text(json.dumps(requirements_mismatch), encoding="utf-8")
    with pytest.raises(ReleaseManifestError, match="requirements SHA-256 mismatch"):
        create_manifest(
            root,
            source_revision="7" * 40,
            python_version="3.11.15",
            rust_toolchain="1.94.1",
        )

    lock_path.unlink()
    write_wheelhouse_lock(
        root / "requirements-runtime.txt",
        root / "wheelhouse",
        lock_path,
    )
    original_wheel = next((root / "wheelhouse").glob("*.whl"))
    original_wheel.rename(root / "wheelhouse" / f"renamed_{original_wheel.name}")
    with pytest.raises(ReleaseManifestError, match="wheelhouse lock filename mismatch"):
        create_manifest(
            root,
            source_revision="7" * 40,
            python_version="3.11.15",
            rust_toolchain="1.94.1",
        )


def test_production_manifest_requires_a_separately_supplied_wheelhouse_lock(tmp_path: Path) -> None:
    root = tmp_path / "release"
    root.mkdir()
    _minimal_release(root)
    (root / WHEELHOUSE_LOCK_FILENAME).unlink()

    development = create_manifest(
        root,
        source_revision="8" * 40,
        python_version="3.11.15",
        rust_toolchain="1.94.1",
    )
    assert development["offline_installable"] is True
    assert development["wheelhouse_lock"]["status"] == "absent"

    with pytest.raises(ReleaseManifestError, match="approved wheelhouse lock"):
        create_manifest(
            root,
            source_revision="8" * 40,
            python_version="3.11.15",
            rust_toolchain="1.94.1",
            production_eligible=True,
        )


def test_manifest_verifier_rejects_wrong_wheel_hash_even_if_inventory_is_rebased(
    tmp_path: Path,
) -> None:
    root = tmp_path / "release"
    root.mkdir()
    _minimal_release(root)
    create_manifest(
        root,
        source_revision="9" * 40,
        python_version="3.11.15",
        rust_toolchain="1.94.1",
    )

    lock_path = root / WHEELHOUSE_LOCK_FILENAME
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["wheels"][0]["sha256"] = "0" * 64
    lock_path.write_text(
        json.dumps(lock, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    manifest_path = root / MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    observed_lock_digest = hashlib.sha256(lock_path.read_bytes()).hexdigest()
    manifest["wheelhouse_lock"]["sha256"] = observed_lock_digest
    lock_record = next(record for record in manifest["files"] if record["path"] == WHEELHOUSE_LOCK_FILENAME)
    lock_record["sha256"] = observed_lock_digest
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(ReleaseManifestError, match="wheelhouse lock SHA-256 mismatch"):
        verify_manifest(root)


def test_release_manifest_is_exact_and_detects_content_or_inventory_changes(tmp_path: Path) -> None:
    root = tmp_path / "release"
    root.mkdir()
    _minimal_release(root)

    created = create_manifest(
        root,
        source_revision="a" * 40,
        python_version="3.11.15",
        rust_toolchain="1.94.1",
    )
    assert created["offline_installable"] is True
    assert created["production_eligible"] is False
    assert created["rust_binary"]["path"] == "bin/execution_engine.exe"
    assert created["rust_binary"]["pe_machine"] == "x86_64"
    assert created["size_contract"]["application_max_bytes"] == 200_000_000
    assert created["size_contract"]["python_runtime_max_bytes"] == 600_000_000
    assert created["size_contract"]["minimum_free_after_install_bytes"] == 20_000_000_000
    assert created["size_contract"]["total_runtime_memory_max_bytes"] == 3_500_000_000
    assert created["size_contract"]["total_runtime_storage_max_bytes"] == 60_000_000_000
    assert verify_manifest(root) == created

    (root / "app.py").write_text("print('tampered')\n", encoding="utf-8")
    with pytest.raises(ReleaseManifestError, match="mismatch"):
        verify_manifest(root)

    (root / "app.py").write_text("print('ready')\n", encoding="utf-8")
    (root / "unexpected.txt").write_text("not inventoried", encoding="utf-8")
    with pytest.raises(ReleaseManifestError, match="unexpected"):
        verify_manifest(root)


def test_runtime_inventory_allows_installed_outputs_but_rejects_injected_source(
    tmp_path: Path,
) -> None:
    root = tmp_path / "release"
    root.mkdir()
    _minimal_release(root)
    created = create_manifest(
        root,
        source_revision="a" * 40,
        python_version="3.11.15",
        rust_toolchain="1.94.1",
    )

    installed = root / ".venv" / "Lib" / "site-packages"
    installed.mkdir(parents=True)
    (installed / "runtime_dependency.py").write_text("value = 1\n", encoding="utf-8")
    log_path = root / "scripts" / "logs" / "live_trader.log"
    log_path.parent.mkdir(parents=True)
    log_path.write_text("bounded runtime log\n", encoding="utf-8")
    pycache = root / "bongus" / "monitoring" / "__pycache__"
    pycache.mkdir()
    (pycache / "web_dashboard.cpython-311.pyc").write_bytes(b"runtime bytecode")

    assert verify_runtime_inventory(root) == created

    injected = root / "bongus" / "monitoring" / "injected.py"
    injected.write_text("raise SystemExit('injected')\n", encoding="utf-8")
    with pytest.raises(ReleaseManifestError, match="unexpected executable/source"):
        verify_runtime_inventory(root)


def test_mutable_data_root_config_does_not_invalidate_signed_release(tmp_path: Path) -> None:
    root = tmp_path / "release"
    root.mkdir()
    _minimal_release(root)
    packaged_config = root / "live_config.json"
    packaged_config.write_text(
        json.dumps({"autonomous_startup_recovery": False}) + "\n",
        encoding="utf-8",
    )
    created = create_manifest(
        root,
        source_revision="d" * 40,
        python_version="3.11.15",
        rust_toolchain="1.94.1",
    )

    data_root = tmp_path / "data"
    data_root.mkdir()
    runtime_config = data_root / "live_config.json"
    shutil.copy2(packaged_config, runtime_config)
    runtime_config.write_text(
        json.dumps({"autonomous_startup_recovery": True}) + "\n",
        encoding="utf-8",
    )

    assert verify_runtime_inventory(root) == created
    packaged_config.write_text(runtime_config.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(ReleaseManifestError, match="mismatch"):
        verify_runtime_inventory(root)


def test_manifest_rejects_path_traversal(tmp_path: Path) -> None:
    root = tmp_path / "release"
    root.mkdir()
    _minimal_release(root)
    create_manifest(
        root,
        source_revision="b" * 40,
        python_version="3.11.15",
        rust_toolchain="1.94.1",
    )
    manifest_path = root / MANIFEST_FILENAME
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["files"][0]["path"] = "../outside"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ReleaseManifestError, match="unsafe manifest path"):
        verify_manifest(root)


def test_manifest_rejects_non_native_rust_bytes(tmp_path: Path) -> None:
    root = tmp_path / "release"
    root.mkdir()
    _minimal_release(root)
    (root / "bin" / "execution_engine.exe").write_bytes(b"not an executable")

    with pytest.raises(ReleaseManifestError, match="neither a supported PE nor ELF"):
        create_manifest(
            root,
            source_revision="d" * 40,
            python_version="3.11.15",
            rust_toolchain="1.94.1",
        )


def test_linux_manifest_resolves_and_validates_native_elf(tmp_path: Path) -> None:
    root = tmp_path / "release"
    root.mkdir()
    _minimal_release(root, platform="linux")

    created = create_manifest(
        root,
        source_revision="f" * 40,
        python_version="3.11.15",
        rust_toolchain="1.94.1",
    )

    assert created["rust_binary"]["path"] == "bin/execution_engine"
    assert created["rust_binary"]["platform"] == "linux"
    assert created["rust_binary"]["executable_format"] == "elf"
    assert created["rust_binary"]["machine"] == "x86_64"
    assert verify_manifest(root) == created


@pytest.mark.skipif(shutil.which("openssl") is None, reason="OpenSSL unavailable")
def test_linux_production_signature_is_verified_and_operator_pinned(tmp_path: Path) -> None:
    root = tmp_path / "release"
    root.mkdir()
    _minimal_release(root, platform="linux")
    signature_dir = root / "signatures"
    signature_dir.mkdir()
    private_key = tmp_path / "private.pem"
    public_key = signature_dir / "linux-release-public.pem"
    signature = signature_dir / "execution_engine.sig"
    subprocess.run(
        [
            "openssl",
            "genpkey",
            "-algorithm",
            "RSA",
            "-pkeyopt",
            "rsa_keygen_bits:2048",
            "-out",
            str(private_key),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["openssl", "pkey", "-in", str(private_key), "-pubout", "-out", str(public_key)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "openssl",
            "dgst",
            "-sha256",
            "-sign",
            str(private_key),
            "-out",
            str(signature),
            str(root / "bin" / "execution_engine"),
        ],
        check=True,
        capture_output=True,
    )
    public_der = subprocess.run(
        ["openssl", "pkey", "-pubin", "-in", str(public_key), "-outform", "DER"],
        check=True,
        capture_output=True,
    ).stdout
    fingerprint = hashlib.sha256(public_der).hexdigest()

    created = create_manifest(
        root,
        source_revision="9" * 40,
        python_version="3.11.15",
        rust_toolchain="1.94.1",
        production_eligible=True,
        rust_signature_status="Valid",
        rust_signature_scheme="openssl-sha256",
        rust_signer_fingerprint=fingerprint,
        rust_signer_subject="release-operator@example.test",
        rust_signature_path="signatures/execution_engine.sig",
        rust_public_key_path="signatures/linux-release-public.pem",
    )
    manifest_signature = root.joinpath(*MANIFEST_SIGNATURE_FILENAME.split("/"))
    subprocess.run(
        [
            "openssl",
            "dgst",
            "-sha256",
            "-sign",
            str(private_key),
            "-out",
            str(manifest_signature),
            str(root / MANIFEST_FILENAME),
        ],
        check=True,
        capture_output=True,
    )

    assert (
        verify_manifest(
            root,
            require_production=True,
            expected_linux_signing_key_sha256=fingerprint,
        )
        == created
    )
    assert (
        verify_runtime_inventory(
            root,
            require_production=True,
            expected_linux_signing_key_sha256=fingerprint,
        )
        == created
    )
    with pytest.raises(ReleaseManifestError, match="operator trust pin"):
        verify_manifest(
            root,
            require_production=True,
            expected_linux_signing_key_sha256="0" * 64,
        )
    with pytest.raises(ReleaseManifestError, match="out-of-band Linux signing-key pin"):
        verify_manifest(root, require_production=True)
    with pytest.raises(ReleaseManifestError, match="operator trust pin"):
        verify_runtime_inventory(
            root,
            require_production=True,
            expected_linux_signing_key_sha256="0" * 64,
        )
    with pytest.raises(ReleaseManifestError, match="out-of-band Linux signing-key pin"):
        verify_runtime_inventory(root, require_production=True)

    original_manifest = (root / MANIFEST_FILENAME).read_bytes()
    (root / "app.py").write_text("print('attacker replacement')\n", encoding="utf-8")
    create_manifest(
        root,
        source_revision="9" * 40,
        python_version="3.11.15",
        rust_toolchain="1.94.1",
        production_eligible=True,
        rust_signature_status="Valid",
        rust_signature_scheme="openssl-sha256",
        rust_signer_fingerprint=fingerprint,
        rust_signer_subject="release-operator@example.test",
        rust_signature_path="signatures/execution_engine.sig",
        rust_public_key_path="signatures/linux-release-public.pem",
    )
    with pytest.raises(ReleaseManifestError, match="detached signature is invalid"):
        verify_manifest(
            root,
            require_production=True,
            expected_linux_signing_key_sha256=fingerprint,
        )

    (root / MANIFEST_FILENAME).write_bytes(original_manifest)
    manifest_signature.unlink()
    with pytest.raises(ReleaseManifestError, match="manifest signature is missing"):
        verify_manifest(
            root,
            require_production=True,
            expected_linux_signing_key_sha256=fingerprint,
        )


@pytest.mark.skipif(
    any(shutil.which(tool) is None for tool in ("bash", "openssl", "sha256sum")),
    reason="Bash/OpenSSL/coreutils unavailable",
)
def test_archive_bootstrap_wrong_pin_or_symlink_cannot_reach_extraction_step(
    tmp_path: Path,
) -> None:
    private_key = tmp_path / "private.pem"
    public_key = tmp_path / "release.public.pem"
    archive = tmp_path / "release.zip"
    signature = tmp_path / "release.zip.sig"
    marker = tmp_path / "extraction-reached"
    archive.write_bytes(b"authenticated archive bytes")
    subprocess.run(
        [
            "openssl",
            "genpkey",
            "-algorithm",
            "RSA",
            "-pkeyopt",
            "rsa_keygen_bits:2048",
            "-out",
            str(private_key),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["openssl", "pkey", "-in", str(private_key), "-pubout", "-out", str(public_key)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "openssl",
            "dgst",
            "-sha256",
            "-sign",
            str(private_key),
            "-out",
            str(signature),
            str(archive),
        ],
        check=True,
        capture_output=True,
    )
    public_der = subprocess.run(
        ["openssl", "pkey", "-pubin", "-in", str(public_key), "-outform", "DER"],
        check=True,
        capture_output=True,
    ).stdout
    fingerprint = hashlib.sha256(public_der).hexdigest()
    verifier = PROJECT_ROOT / "deployment" / "Verify-BongusArchive.sh"
    command = 'bash "$1" "$2" "$3" "$4" "$5" && printf reached > "$6"'

    rejected = subprocess.run(
        [
            shutil.which("bash") or "bash",
            "-c",
            command,
            "bootstrap-test",
            str(verifier),
            str(archive),
            str(signature),
            str(public_key),
            "0" * 64,
            str(marker),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rejected.returncode != 0
    assert "operator trust pin" in rejected.stderr
    assert not marker.exists()

    if os.name != "nt":
        linked_archive = tmp_path / "linked-release.zip"
        linked_archive.symlink_to(archive)
        linked_marker = tmp_path / "linked-extraction-reached"
        linked_rejected = subprocess.run(
            [
                shutil.which("bash") or "bash",
                "-c",
                command,
                "bootstrap-test",
                str(verifier),
                str(linked_archive),
                str(signature),
                str(public_key),
                fingerprint,
                str(linked_marker),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert linked_rejected.returncode != 0
        assert "linked" in linked_rejected.stderr
        assert not linked_marker.exists()

    accepted = subprocess.run(
        [
            shutil.which("bash") or "bash",
            "-c",
            command,
            "bootstrap-test",
            str(verifier),
            str(archive),
            str(signature),
            str(public_key),
            fingerprint,
            str(marker),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert accepted.returncode == 0, accepted.stderr
    assert marker.read_text(encoding="utf-8") == "reached"


@pytest.mark.skipif(
    sys.platform != "linux"
    or not _host_meets_reviewed_python_floor()
    or any(shutil.which(tool) is None for tool in ("bash", "git", "openssl", "sha256sum")),
    reason="native Linux release tooling unavailable",
)
def test_linux_builder_archive_sign_failure_is_unpublished_and_retryable(tmp_path: Path) -> None:
    configured_python = (PROJECT_ROOT / ".python-version").read_text(encoding="utf-8").strip()
    actual_python = f"{sys.version_info.major}.{sys.version_info.minor}"
    if not configured_python.startswith(f"{actual_python}."):
        pytest.skip(f"release builder requires Python {configured_python}")

    private_key = tmp_path / "private.pem"
    subprocess.run(
        [
            shutil.which("openssl") or "openssl",
            "genpkey",
            "-algorithm",
            "RSA",
            "-pkeyopt",
            "rsa_keygen_bits:2048",
            "-out",
            str(private_key),
        ],
        check=True,
        capture_output=True,
    )
    rust_binary = tmp_path / "execution_engine"
    _write_test_elf(rust_binary)
    rust_binary.chmod(0o755)

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_openssl = fake_bin / "openssl"
    fake_openssl.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
last_argument=""
for argument in "$@"; do last_argument="$argument"; done
if [[ "${1:-}" == "dgst" && "$last_argument" == *.zip ]]; then
    echo "injected archive signing failure" >&2
    exit 97
fi
exec "$REAL_OPENSSL" "$@"
""",
        encoding="utf-8",
    )
    fake_openssl.chmod(0o755)

    output = tmp_path / "release-output"
    archive = Path(f"{output}.zip")
    command = [
        shutil.which("bash") or "bash",
        str(PROJECT_ROOT / "scripts" / "build_release.sh"),
        "--output",
        str(output),
        "--python",
        sys.executable,
        "--rust-binary",
        str(rust_binary),
        "--skip-rust-build",
        "--without-wheelhouse",
        "--allow-dirty-source",
        "--signing-key",
        str(private_key),
        "--signer-subject",
        "release-test@example.invalid",
    ]
    injected_environment = os.environ.copy()
    injected_environment["REAL_OPENSSL"] = shutil.which("openssl") or "openssl"
    injected_environment["PATH"] = f"{fake_bin}{os.pathsep}{injected_environment['PATH']}"

    failed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        env=injected_environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert failed.returncode != 0
    assert "injected archive signing failure" in failed.stderr
    for artifact in (
        output,
        archive,
        Path(f"{archive}.sha256"),
        Path(f"{archive}.sig"),
        Path(f"{archive}.public.pem"),
    ):
        assert not artifact.exists()
    assert not tuple(tmp_path.glob(".bongus-release.*"))
    assert not tuple(tmp_path.glob(".bongus-release-artifacts.*"))

    retried = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert retried.returncode == 0, retried.stderr
    assert output.is_dir()
    assert archive.is_file()
    assert Path(f"{archive}.sha256").is_file()
    assert Path(f"{archive}.sig").is_file()
    assert Path(f"{archive}.public.pem").is_file()


def test_manifest_rejects_a_missing_declared_process_target(tmp_path: Path) -> None:
    root = tmp_path / "release"
    root.mkdir()
    _minimal_release(root)
    (root / "bongus" / "monitoring" / "telegram_alerter.py").unlink()

    with pytest.raises(ReleaseManifestError, match="Python script is missing"):
        create_manifest(
            root,
            source_revision="d" * 40,
            python_version="3.11.15",
            rust_toolchain="1.94.1",
        )


def test_windows_package_cannot_claim_production_without_whole_manifest_signature(
    tmp_path: Path,
) -> None:
    root = tmp_path / "release"
    root.mkdir()
    _minimal_release(root)

    with pytest.raises(ReleaseManifestError, match="Authenticode"):
        create_manifest(
            root,
            source_revision="e" * 40,
            python_version="3.11.15",
            rust_toolchain="1.94.1",
            production_eligible=True,
        )

    with pytest.raises(ReleaseManifestError, match="whole-manifest"):
        create_manifest(
            root,
            source_revision="e" * 40,
            python_version="3.11.15",
            rust_toolchain="1.94.1",
            production_eligible=True,
            rust_signature_status="Valid",
            rust_signer_thumbprint="A" * 40,
            rust_signer_subject="CN=Release Test",
        )


def test_offline_manifest_requires_every_pinned_wheel(tmp_path: Path) -> None:
    root = tmp_path / "release"
    root.mkdir()
    _minimal_release(root)
    (root / "requirements-runtime.txt").write_text(
        "msgpack==1.1.2\nrequests==2.32.5\n",
        encoding="utf-8",
    )

    with pytest.raises(ReleaseManifestError, match="requests==2.32.5"):
        create_manifest(
            root,
            source_revision="f" * 40,
            python_version="3.11.15",
            rust_toolchain="1.94.1",
        )


def test_offline_manifest_rejects_non_runtime_wheels(tmp_path: Path) -> None:
    root = tmp_path / "release"
    root.mkdir()
    _minimal_release(root)
    _write_test_wheel(root / "wheelhouse", "pytest", "9.0.0")

    with pytest.raises(ReleaseManifestError, match="non-runtime or unpinned"):
        create_manifest(
            root,
            source_revision="f" * 40,
            python_version="3.11.15",
            rust_toolchain="1.94.1",
        )


def test_application_budget_is_a_hard_manifest_gate(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "release"
    root.mkdir()
    _minimal_release(root)
    monkeypatch.setattr(release_manifest, "APPLICATION_MAX_BYTES", 1)

    with pytest.raises(ReleaseManifestError, match="hard budget"):
        create_manifest(
            root,
            source_revision="1" * 40,
            python_version="3.11.15",
            rust_toolchain="1.94.1",
        )


def test_archive_bytes_are_reproducible_and_have_digest_sidecars(tmp_path: Path) -> None:
    root = tmp_path / "release"
    root.mkdir()
    _minimal_release(root)
    create_manifest(
        root,
        source_revision="c" * 40,
        python_version="3.11.15",
        rust_toolchain="1.94.1",
    )

    first, first_digest = create_deterministic_archive(root, tmp_path / "first.zip")
    second, second_digest = create_deterministic_archive(root, tmp_path / "second.zip")

    assert first_digest == second_digest
    assert first.read_bytes() == second.read_bytes()
    assert first_digest == hashlib.sha256(first.read_bytes()).hexdigest()
    assert first.with_suffix(".zip.sha256").read_text(encoding="ascii") == (f"{first_digest}  first.zip\n")


def test_release_builder_contract_never_builds_or_downloads_at_runtime() -> None:
    builder = (PROJECT_ROOT / "scripts" / "build_release.ps1").read_text(encoding="utf-8")
    installer = (PROJECT_ROOT / "deployment" / "Install-BongusRelease.ps1").read_text(encoding="utf-8")
    cargo = (PROJECT_ROOT / "execution_engine" / "Cargo.toml").read_text(encoding="utf-8")

    assert "cargo build" in builder
    assert "--locked --release" in builder
    assert '$StagedProcessManifest.processes.rust.target = "bin/execution_engine"' in builder
    assert "-m pip wheel" in builder
    assert "--no-deps" in builder
    assert "--only-binary=:all:" in builder
    assert "ApprovedWheelhouseLock" in builder
    assert "wheelhouse.lock.json" in builder
    assert "check-python" in builder
    assert "check-python" in installer
    assert '"--python-version", $ActualPython' in builder
    assert "--no-index" in installer
    assert "--no-cache-dir" in installer
    assert "cargo" not in installer.casefold()
    assert "AllowUnsignedDevelopmentBinary" in builder
    assert "$GitStatus.Count -eq 0" in builder
    assert "--require-production" in installer
    assert "Get-AuthenticodeSignature" in builder
    assert "Get-AuthenticodeSignature" in installer
    assert "config/binance_endpoints_v1.json" in builder
    assert "scripts\\collect_testnet_account_evidence.py" in builder
    assert "scripts\\collect_soak_evidence.py" in builder
    assert "scripts\\collect_daily_reconciliation.py" in builder
    assert "bongus\\testing\\soak_evidence.py" in builder
    assert "bongus\\testing\\daily_reconciliation_evidence.py" in builder
    assert "bongus\\testing\\measurement_evidence.py" in builder
    assert "python_runtime_max_bytes" in installer
    assert "minimum_free_after_install_bytes" in installer
    assert "[profile.release]" in cargo
    assert "incremental = false" in cargo
    assert 'strip = "symbols"' in cargo


def _systemd_renderer_from_installer() -> tuple[str, list[Path]]:
    installer = (PROJECT_ROOT / "deployment" / "Install-BongusRelease.sh").read_text(encoding="utf-8")
    renderers = [
        block for block in re.findall(r"<<'PY'\n(.*?)\nPY", installer, re.DOTALL)
        if "replacements = {" in block
    ]
    assert len(renderers) == 1
    sources = [PROJECT_ROOT / path for path in re.findall(r'"\$RELEASE_ROOT/(deployment/[^"\n]+\.in)"', installer)]
    assert sources
    return renderers[0], sources


def _systemd_renderer_environment() -> dict[str, str]:
    environment = {
        key: "bongus-test" for key in (
            "SERVICE_USER", "SERVICE_GROUP", "BACKUP_USER", "BACKUP_GROUP",
            "OFFSITE_USER", "OFFSITE_GROUP", "MAINTENANCE_USER", "MAINTENANCE_GROUP",
        )
    }
    environment.update(RELEASE_ROOT="/opt/bongus", DATA_ROOT="/var/lib/bongus", SERVICE_NAME="bongus@paper")
    return environment


def test_installer_renders_actual_systemd_templates_and_preserves_at_syntax(tmp_path: Path) -> None:
    renderer, sources = _systemd_renderer_from_installer()
    destinations = [tmp_path / source.name.removesuffix(".in") for source in sources]
    result = subprocess.run(
        [sys.executable, "-c", renderer, *[str(path) for pair in zip(sources, destinations) for path in pair]],
        env=_systemd_renderer_environment(), capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    rendered = {path.name: path.read_text(encoding="utf-8") for path in destinations}
    assert "SystemCallFilter=@system-service" in rendered["bongus-ops-health.service"]
    assert "After=bongus@paper.service" in rendered["bongus-ops-health.service"]
    assert all(not re.search(r"@[A-Z][A-Z0-9_]*@", text) for text in rendered.values())


@pytest.mark.parametrize("placeholder", ["@UNKNOWN_PATH@", "@UNSET_CONFIG_2@"])
def test_installer_rejects_unknown_systemd_placeholder_before_writing(tmp_path: Path, placeholder: str) -> None:
    renderer, _ = _systemd_renderer_from_installer()
    source = tmp_path / "unknown.service.in"
    destination = tmp_path / "unknown.service"
    source.write_text(f"[Service]\nExecStart={placeholder}/worker\nSystemCallFilter=@system-service\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-c", renderer, str(source), str(destination)],
        env=_systemd_renderer_environment(), capture_output=True, text=True, timeout=10,
    )
    assert result.returncode != 0
    assert "unresolved systemd template marker" in result.stderr
    assert not destination.exists()


def test_linux_release_and_systemd_contracts_are_bounded_and_offline() -> None:
    builder = (PROJECT_ROOT / "scripts" / "build_release.sh").read_text(encoding="utf-8")
    installer = (PROJECT_ROOT / "deployment" / "Install-BongusRelease.sh").read_text(encoding="utf-8")
    service = (PROJECT_ROOT / "deployment" / "bongus.service.in").read_text(encoding="utf-8")
    slice_unit = (PROJECT_ROOT / "deployment" / "bongus.slice.in").read_text(encoding="utf-8")
    health_service = (PROJECT_ROOT / "deployment" / "bongus-ops-health.service.in").read_text(encoding="utf-8")
    health_timer = (PROJECT_ROOT / "deployment" / "bongus-ops-health.timer.in").read_text(encoding="utf-8")
    backup_service = (PROJECT_ROOT / "deployment" / "bongus-backup.service.in").read_text(encoding="utf-8")
    backup_timer = (PROJECT_ROOT / "deployment" / "bongus-backup.timer.in").read_text(encoding="utf-8")
    offsite_service = (PROJECT_ROOT / "deployment" / "bongus-offsite-backup.service.in").read_text(encoding="utf-8")
    maintenance_service = (
        PROJECT_ROOT / "deployment" / "bongus-offsite-maintenance.service.in"
    ).read_text(encoding="utf-8")
    maintenance_timer = (
        PROJECT_ROOT / "deployment" / "bongus-offsite-maintenance.timer.in"
    ).read_text(encoding="utf-8")

    assert "cargo build" in builder
    assert "--locked" in builder
    assert "pip wheel" in builder
    assert "--only-binary=:all:" in builder
    assert "--approved-wheelhouse-lock" in builder
    assert "Production packaging requires --wheelhouse and --approved-wheelhouse-lock" in builder
    assert 'copy_release_file "$APPROVED_WHEELHOUSE_LOCK" "wheelhouse.lock.json"' in builder
    assert "check-python" in builder
    assert "check-python" in installer
    assert '--python-version "$ACTUAL_PYTHON"' in builder
    assert "openssl dgst -sha256 -sign" in builder
    assert "signatures/release-manifest.sig" in builder
    assert 'ARCHIVE_SIGNATURE_PATH="${ARCHIVE_PATH}.sig"' in builder
    assert "--trusted-linux-key-sha256" in builder
    assert "--trusted-linux-key-sha256" in installer
    assert 'payload["processes"]["rust"]["target"] = "bin/execution_engine"' in builder
    assert 'check_operational_health.py" "scripts/check_operational_health.py' in builder
    assert 'upload_verified_offsite_backup.py" "scripts/upload_verified_offsite_backup.py' in builder
    assert 'create_verified_backup_set.py" "scripts/create_verified_backup_set.py' in builder
    assert 'maintain_offsite_repository.py" "scripts/maintain_offsite_repository.py' in builder
    assert 'collect_testnet_account_evidence.py" "scripts/collect_testnet_account_evidence.py' in builder
    assert 'collect_soak_evidence.py" "scripts/collect_soak_evidence.py' in builder
    assert 'collect_daily_reconciliation.py" "scripts/collect_daily_reconciliation.py' in builder
    assert 'soak_evidence.py" "bongus/testing/soak_evidence.py' in builder
    assert 'daily_reconciliation_evidence.py" "bongus/testing/daily_reconciliation_evidence.py' in builder
    assert 'measurement_evidence.py" "bongus/testing/measurement_evidence.py' in builder
    assert "config/binance_endpoints_v1.json" in builder
    assert "bongus-ops-health.service.in" in builder
    assert "bongus.slice.in" in builder
    assert "bongus-ops-health.timer.in" in builder
    assert "bongus-backup.service.in" in builder
    assert "bongus-backup.timer.in" in builder
    assert "bongus-offsite-backup.service.in" in builder
    assert "bongus-offsite-maintenance.service.in" in builder
    assert "bongus-offsite-maintenance.timer.in" in builder
    assert "--no-index" in installer
    assert "--no-cache-dir" in installer
    assert "cargo" not in installer.casefold()
    assert "--trusted-key-sha256" in installer
    assert "--allow-development-package" in installer
    assert 'RUNTIME_CONFIG_PATH="$DATA_ROOT/live_config.json"' in installer
    assert '"$RELEASE_ROOT/live_config.json" "$RUNTIME_CONFIG_PATH"' in installer
    assert "Refusing to replace existing or linked unit" in installer
    assert "chronyc/Chrony is required" in installer
    assert "must have distinct numeric UIDs" in installer
    assert "must have distinct numeric GIDs" in installer
    assert "must not be root" in installer
    assert "os.O_DIRECTORY | os.O_NOFOLLOW" in installer
    assert "MINIMUM_FREE_AFTER_INSTALL_BYTES + BACKUP_OPERATION_MAX_BYTES" in installer
    assert 'chown -R "root:$SERVICE_GROUP" "$RELEASE_ROOT"' in installer
    assert 'chown -R "$SERVICE_USER:$SERVICE_GROUP" "$RELEASE_ROOT"' not in installer
    assert "signed release tree must not overlap" in installer
    assert "systemd-analyze verify" in installer
    assert "After=network-online.target time-sync.target" in service
    assert "Wants=network-online.target time-sync.target" in service
    assert "Environment=BONGUS_DATA_ROOT=@DATA_ROOT@" in service
    assert "EnvironmentFile=-/etc/bongus/trader.env" in service
    assert "Environment=PYTHONDONTWRITEBYTECODE=1" in service
    assert "ReadWritePaths=@DATA_ROOT@" in service
    assert "ReadOnlyPaths=@DATA_ROOT@/backups" in service
    assert "InaccessiblePaths=@DATA_ROOT@/offsite" in service
    assert "ReadWritePaths=@RELEASE_ROOT@" not in service
    assert "MemoryHigh=3000000000" in service
    assert "MemoryMax=3500000000" in service
    assert "MemorySwapMax=0" in service
    assert "Slice=@SERVICE_NAME@.slice" in service
    assert "MemoryHigh=3200000000" in slice_unit
    assert "MemoryMax=3500000000" in slice_unit
    assert "MemorySwapMax=0" in slice_unit
    assert "Slice=@SERVICE_NAME@.slice" in backup_service
    assert "MemoryMax=512000000" in backup_service
    assert "Slice=@SERVICE_NAME@.slice" in offsite_service
    assert "MemoryMax=512000000" in offsite_service
    assert "Slice=@SERVICE_NAME@.slice" in health_service
    assert "MemoryMax=256000000" in health_service
    assert "Restart=always" in service
    assert "-m scripts.check_operational_health" in health_service
    assert "TemporaryFileSystem=@DATA_ROOT@:ro" in health_service
    assert "BindReadOnlyPaths=@DATA_ROOT@/backups @DATA_ROOT@/offsite" in health_service
    assert "ReadWritePaths=" not in health_service
    assert "--clock-warning-offset-ms 100" in health_service
    assert "--clock-critical-offset-ms 250" in health_service
    assert "--offsite-receipt-path @DATA_ROOT@/offsite/upload/latest.json" in health_service
    assert "--offsite-retention-receipt-path @DATA_ROOT@/offsite/maintenance/latest.json" in health_service
    assert "--heartbeat-path @DATA_ROOT@/runtime/runtime_heartbeat.json" in health_service
    assert "--max-offsite-age-seconds 900" in health_service
    assert "OnUnitActiveSec=60s" in health_timer
    assert "Persistent=true" in health_timer
    assert "/usr/bin/flock --exclusive --timeout 840" in backup_service
    assert "-m scripts.create_verified_backup_set create" in backup_service
    assert "--rust-execution-binary @RELEASE_ROOT@/bin/execution_engine" in backup_service
    assert "--rust-recovery-control-socket @DATA_ROOT@/runtime/rust/recovery-control.sock" in backup_service
    assert "--rust-recovery-generations-directory @DATA_ROOT@/runtime/rust/recovery_generations" in backup_service
    assert "--retention-count 1" in backup_service
    assert "--required-headroom-bytes 20000000000" in backup_service
    assert "--backup-tree-budget-bytes 20500000000" in backup_service
    assert "RestrictAddressFamilies=AF_UNIX" in backup_service
    assert "OnCalendar=*-*-* *:00/10:00" in backup_timer
    assert "Persistent=true" in backup_timer
    assert "OnSuccess=@SERVICE_NAME@-offsite-backup.service" in backup_service
    assert "EnvironmentFile=/etc/bongus/offsite-backup.env" in offsite_service
    assert "-m scripts.upload_verified_offsite_backup" in offsite_service
    assert "TemporaryFileSystem=@DATA_ROOT@:ro" in offsite_service
    assert "BindReadOnlyPaths=@DATA_ROOT@/backups" in offsite_service
    assert "BindPaths=@DATA_ROOT@/offsite" in offsite_service
    assert "User=@MAINTENANCE_USER@" in maintenance_service
    assert "Group=@MAINTENANCE_GROUP@" in maintenance_service
    assert "EnvironmentFile=/etc/bongus/offsite-maintenance.env" in maintenance_service
    assert "-m scripts.maintain_offsite_repository" in maintenance_service
    assert "--timeout-seconds 240" in maintenance_service
    assert "TimeoutStartSec=5min" in maintenance_service
    assert "OnCalendar=*-*-* 03:36:00 UTC" in maintenance_timer
    assert "RandomizedDelaySec=0" in maintenance_timer
    assert 'systemctl enable "${SERVICE_NAME}-ops-health.timer"' in installer
    assert 'systemctl enable "${SERVICE_NAME}-backup.timer"' in installer
    assert 'systemctl enable "${SERVICE_NAME}-offsite-maintenance.timer"' in installer


@pytest.mark.parametrize(
    ("unit_name", "module_name"),
    (
        ("bongus-ops-health.service.in", "scripts.check_operational_health"),
        ("bongus-backup.service.in", "scripts.create_verified_backup_set"),
        ("bongus-offsite-backup.service.in", "scripts.upload_verified_offsite_backup"),
        ("bongus-offsite-maintenance.service.in", "scripts.maintain_offsite_repository"),
    ),
)
def test_operational_systemd_python_entrypoints_import_from_release_root(
    unit_name: str,
    module_name: str,
) -> None:
    unit = (PROJECT_ROOT / "deployment" / unit_name).read_text(encoding="utf-8")
    assert "WorkingDirectory=@RELEASE_ROOT@" in unit
    exec_start = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
    python_invocation = "@RELEASE_ROOT@/.venv/bin/python -m "
    assert python_invocation + module_name in exec_start
    assert f"@RELEASE_ROOT@/{module_name.replace('.', '/')}.py" not in exec_start

    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, "-m", module_name, "--help"],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "ModuleNotFoundError" not in result.stderr


@pytest.mark.skipif(
    shutil.which("powershell") is None or not _host_meets_reviewed_python_floor(),
    reason="Windows PowerShell or reviewed Python floor unavailable",
)
def test_installer_rejects_a_noncanonical_environment_before_mutation(tmp_path: Path) -> None:
    root = tmp_path / "release"
    root.mkdir()
    _minimal_release(root)
    shutil.copy2(PROJECT_ROOT / "scripts" / "release_manifest.py", root / "scripts")
    shutil.copy2(
        PROJECT_ROOT / "deployment" / "Install-BongusRelease.ps1",
        root / "Install-BongusRelease.ps1",
    )
    create_manifest(
        root,
        source_revision="2" * 40,
        python_version=REVIEWED_PYTHON_BASELINE,
        rust_toolchain="1.94.1",
    )
    noncanonical = tmp_path / "different-environment"

    result = subprocess.run(
        [
            shutil.which("powershell") or "powershell",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(root / "Install-BongusRelease.ps1"),
            "-PythonExecutable",
            sys.executable,
            "-EnvironmentPath",
            str(noncanonical),
            "-AllowDevelopmentPackage",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode != 0
    assert "storage-accounted path" in result.stdout + result.stderr
    assert not noncanonical.exists()
    assert not (root / ".venv").exists()


@pytest.mark.skipif(
    shutil.which("powershell") is None or not _host_meets_reviewed_python_floor(),
    reason="Windows PowerShell or reviewed Python floor unavailable",
)
def test_powershell_builder_stages_only_contained_runtime_files(tmp_path: Path) -> None:
    fake_binary = tmp_path / "execution_engine.exe"
    _write_test_pe(fake_binary)
    output = tmp_path / "release"
    result = subprocess.run(
        [
            shutil.which("powershell") or "powershell",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(PROJECT_ROOT / "scripts" / "build_release.ps1"),
            "-OutputPath",
            str(output),
            "-RustBinaryPath",
            str(fake_binary),
            "-SkipRustBuild",
            "-WithoutWheelhouse",
            "-NoArchive",
            "-AllowDirtySource",
            "-AllowUnsignedDevelopmentBinary",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    manifest = verify_manifest(output)
    process_manifest = json.loads((output / "bongus" / "runtime" / "process_manifest.json").read_text(encoding="utf-8"))

    assert manifest["offline_installable"] is False
    assert manifest["production_eligible"] is False
    assert process_manifest["processes"]["rust"]["target"] == "bin/execution_engine"
    assert (output / "bin" / "execution_engine.exe").read_bytes() == fake_binary.read_bytes()
    assert (output / "bongus" / "engine" / "offline_storage_migration.py").is_file()
    probe_env = dict(os.environ)
    probe_env["PYTHONPATH"] = str(output)
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            ("from bongus.monitoring import king_watchdog as w; print(w.RUST_COMMAND[0]); print(w.RUST_ENGINE_DIR)"),
        ],
        cwd=output,
        env=probe_env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert probe.returncode == 0, probe.stdout + probe.stderr
    probe_lines = [line.strip() for line in probe.stdout.splitlines() if line.strip()]
    assert Path(probe_lines[-2]).resolve() == (output / "bin" / "execution_engine.exe").resolve()
    assert Path(probe_lines[-1]).resolve() == (output / "bin").resolve()
    assert Path(probe_lines[-1]).is_dir()
    assert not (output / ".env").exists()
    assert not (output / "tests").exists()
    assert not (output / "bongus" / "research").exists()
    assert not (output / "bongus" / "testing").exists()
    assert not (output / "execution_engine" / "target").exists()

    install = subprocess.run(
        [
            shutil.which("powershell") or "powershell",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(output / "Install-BongusRelease.ps1"),
            "-PythonExecutable",
            sys.executable,
        ],
        cwd=output,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert install.returncode != 0
    assert "production" in (install.stdout + install.stderr).lower()
    assert not (output / ".venv").exists()


@pytest.mark.skipif(
    shutil.which("powershell") is None or not _host_meets_reviewed_python_floor(),
    reason="Windows PowerShell or reviewed Python floor unavailable",
)
def test_powershell_builder_rejects_unsigned_binary_by_default(tmp_path: Path) -> None:
    fake_binary = tmp_path / "execution_engine.exe"
    _write_test_pe(fake_binary)
    output = tmp_path / "release"

    result = subprocess.run(
        [
            shutil.which("powershell") or "powershell",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(PROJECT_ROOT / "scripts" / "build_release.ps1"),
            "-OutputPath",
            str(output),
            "-RustBinaryPath",
            str(fake_binary),
            "-SkipRustBuild",
            "-WithoutWheelhouse",
            "-NoArchive",
            "-AllowDirtySource",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode != 0
    assert "Authenticode" in result.stdout + result.stderr
    assert not output.exists()
