from __future__ import annotations

import hashlib
import json
import os
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
    ReleaseManifestError,
    create_deterministic_archive,
    create_manifest,
    verify_manifest,
    verify_runtime_inventory,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


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
    payload[section:section + 8] = b".text\0\0\0"
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
    assert {"httpx", "pyright", "pytest", "pytest-asyncio", "pytest-trio", "trio"} <= set(
        development
    )
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


def test_release_manifest_is_exact_and_detects_content_or_inventory_changes(tmp_path: Path) -> None:
    root = tmp_path / "release"
    root.mkdir()
    _minimal_release(root)

    created = create_manifest(
        root,
        source_revision="a" * 40,
        python_version="3.11.4",
        rust_toolchain="1.94.1",
    )
    assert created["offline_installable"] is True
    assert created["production_eligible"] is False
    assert created["rust_binary"]["path"] == "bin/execution_engine.exe"
    assert created["rust_binary"]["pe_machine"] == "x86_64"
    assert created["size_contract"]["application_max_bytes"] == 200_000_000
    assert created["size_contract"]["python_runtime_max_bytes"] == 600_000_000
    assert created["size_contract"]["minimum_free_after_install_bytes"] == 4_000_000_000
    assert created["size_contract"]["total_runtime_memory_max_bytes"] == 20_000_000_000
    assert created["size_contract"]["total_runtime_storage_max_bytes"] == 20_000_000_000
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
        python_version="3.11.4",
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


def test_manifest_rejects_path_traversal(tmp_path: Path) -> None:
    root = tmp_path / "release"
    root.mkdir()
    _minimal_release(root)
    create_manifest(
        root,
        source_revision="b" * 40,
        python_version="3.11.4",
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
            python_version="3.11.4",
            rust_toolchain="1.94.1",
        )


def test_linux_manifest_resolves_and_validates_native_elf(tmp_path: Path) -> None:
    root = tmp_path / "release"
    root.mkdir()
    _minimal_release(root, platform="linux")

    created = create_manifest(
        root,
        source_revision="f" * 40,
        python_version="3.11.4",
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
        python_version="3.11.4",
        rust_toolchain="1.94.1",
        production_eligible=True,
        rust_signature_status="Valid",
        rust_signature_scheme="openssl-sha256",
        rust_signer_fingerprint=fingerprint,
        rust_signer_subject="release-operator@example.test",
        rust_signature_path="signatures/execution_engine.sig",
        rust_public_key_path="signatures/linux-release-public.pem",
    )

    assert verify_manifest(root, require_production=True) == created
    assert verify_runtime_inventory(
        root,
        require_production=True,
        expected_linux_signing_key_sha256=fingerprint,
    ) == created
    with pytest.raises(ReleaseManifestError, match="operator trust pin"):
        verify_runtime_inventory(
            root,
            require_production=True,
            expected_linux_signing_key_sha256="0" * 64,
        )
    with pytest.raises(ReleaseManifestError, match="requires BONGUS_RELEASE_SIGNING_KEY_SHA256"):
        verify_runtime_inventory(root, require_production=True)


def test_manifest_rejects_a_missing_declared_process_target(tmp_path: Path) -> None:
    root = tmp_path / "release"
    root.mkdir()
    _minimal_release(root)
    (root / "bongus" / "monitoring" / "telegram_alerter.py").unlink()

    with pytest.raises(ReleaseManifestError, match="Python script is missing"):
        create_manifest(
            root,
            source_revision="d" * 40,
            python_version="3.11.4",
            rust_toolchain="1.94.1",
        )


def test_production_manifest_requires_signature_and_complete_processes(tmp_path: Path) -> None:
    root = tmp_path / "release"
    root.mkdir()
    _minimal_release(root)

    with pytest.raises(ReleaseManifestError, match="Authenticode"):
        create_manifest(
            root,
            source_revision="e" * 40,
            python_version="3.11.4",
            rust_toolchain="1.94.1",
            production_eligible=True,
        )

    created = create_manifest(
        root,
        source_revision="e" * 40,
        python_version="3.11.4",
        rust_toolchain="1.94.1",
        production_eligible=True,
        rust_signature_status="Valid",
        rust_signer_thumbprint="A" * 40,
        rust_signer_subject="CN=Release Test",
    )

    assert created["production_eligible"] is True
    assert verify_manifest(root, require_offline=True, require_production=True) == created


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
            python_version="3.11.4",
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
            python_version="3.11.4",
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
            python_version="3.11.4",
            rust_toolchain="1.94.1",
        )


def test_archive_bytes_are_reproducible_and_have_digest_sidecars(tmp_path: Path) -> None:
    root = tmp_path / "release"
    root.mkdir()
    _minimal_release(root)
    create_manifest(
        root,
        source_revision="c" * 40,
        python_version="3.11.4",
        rust_toolchain="1.94.1",
    )

    first, first_digest = create_deterministic_archive(root, tmp_path / "first.zip")
    second, second_digest = create_deterministic_archive(root, tmp_path / "second.zip")

    assert first_digest == second_digest
    assert first.read_bytes() == second.read_bytes()
    assert first_digest == hashlib.sha256(first.read_bytes()).hexdigest()
    assert first.with_suffix(".zip.sha256").read_text(encoding="ascii") == (
        f"{first_digest}  first.zip\n"
    )


def test_release_builder_contract_never_builds_or_downloads_at_runtime() -> None:
    builder = (PROJECT_ROOT / "scripts" / "build_release.ps1").read_text(encoding="utf-8")
    installer = (PROJECT_ROOT / "deployment" / "Install-BongusRelease.ps1").read_text(
        encoding="utf-8"
    )
    cargo = (PROJECT_ROOT / "execution_engine" / "Cargo.toml").read_text(encoding="utf-8")

    assert "cargo build" in builder
    assert "--locked --release" in builder
    assert '$StagedProcessManifest.processes.rust.target = "bin/execution_engine"' in builder
    assert "-m pip wheel" in builder
    assert "--no-deps" in builder
    assert "--no-index" in installer
    assert "--no-cache-dir" in installer
    assert "cargo" not in installer.casefold()
    assert "AllowUnsignedDevelopmentBinary" in builder
    assert "$GitStatus.Count -eq 0" in builder
    assert "--require-production" in installer
    assert "Get-AuthenticodeSignature" in builder
    assert "Get-AuthenticodeSignature" in installer
    assert "python_runtime_max_bytes" in installer
    assert "minimum_free_after_install_bytes" in installer
    assert "[profile.release]" in cargo
    assert 'incremental = false' in cargo
    assert 'strip = "symbols"' in cargo


def test_linux_release_and_systemd_contracts_are_bounded_and_offline() -> None:
    builder = (PROJECT_ROOT / "scripts" / "build_release.sh").read_text(encoding="utf-8")
    installer = (PROJECT_ROOT / "deployment" / "Install-BongusRelease.sh").read_text(
        encoding="utf-8"
    )
    service = (PROJECT_ROOT / "deployment" / "bongus.service.in").read_text(
        encoding="utf-8"
    )

    assert "cargo build" in builder
    assert "--locked" in builder
    assert "pip wheel" in builder
    assert "openssl dgst -sha256 -sign" in builder
    assert 'payload["processes"]["rust"]["target"] = "bin/execution_engine"' in builder
    assert "--no-index" in installer
    assert "--no-cache-dir" in installer
    assert "cargo" not in installer.casefold()
    assert "--trusted-key-sha256" in installer
    assert "--allow-development-package" in installer
    assert "MemoryHigh=16000000000" in service
    assert "MemoryMax=@MEMORY_MAX_BYTES@" in service
    assert "MemorySwapMax=0" in service
    assert "Restart=always" in service


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell unavailable")
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
        python_version=(
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        ),
        rust_toolchain="1.94.1",
        production_eligible=True,
        rust_signature_status="Valid",
        rust_signer_thumbprint="A" * 40,
        rust_signer_subject="CN=Release Test",
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


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell unavailable")
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
    process_manifest = json.loads(
        (output / "bongus" / "runtime" / "process_manifest.json").read_text(encoding="utf-8")
    )

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
            (
                "from bongus.monitoring import king_watchdog as w; "
                "print(w.RUST_COMMAND[0]); print(w.RUST_ENGINE_DIR)"
            ),
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


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell unavailable")
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
