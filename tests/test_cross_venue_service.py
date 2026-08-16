from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from bongus.research.cross_venue.artifacts import parquet_backend_available

UNIT = Path(__file__).parents[1] / "deployment" / "bongus-research.service.in"
ROOT = Path(__file__).parents[1]
INSTALLER = ROOT / "deployment" / "Install-BongusResearch.sh"


def test_research_service_is_hard_isolated_and_not_credentialed() -> None:
    text = UNIT.read_text(encoding="utf-8")
    assert "User=bongus-research" in text
    assert "Group=bongus-research" in text
    assert "--allow-network --continuous" in text
    assert "--artifact-root @BONGUS_RESEARCH_DATA@/artifacts" in text
    assert "--artifact-flush-seconds 30" in text
    assert "ExecStartPre=" in text and "--startup-check" in text
    assert "ExecStartPre=@BONGUS_RESEARCH_PYTHON@ -I " in text
    assert "ExecStart=@BONGUS_RESEARCH_PYTHON@ -I " in text
    assert "ReadWritePaths=@BONGUS_RESEARCH_DATA@" in text
    assert "InaccessiblePaths=-@BONGUS_LIVE_DATA@" in text
    assert "NoNewPrivileges=yes" in text
    assert "ProtectSystem=strict" in text
    assert "ProtectHome=yes" in text
    assert "PrivateIPC=yes" in text
    assert "RestrictAddressFamilies=AF_INET AF_INET6" in text
    assert "IPAddressDeny=localhost" in text
    assert "IPAddressAllow=127.0.0.53/32" in text
    assert "SocketBindDeny=any" in text
    assert "CapabilityBoundingSet=\n" in text
    assert "UnsetEnvironment=BINANCE_API_KEY BINANCE_API_SECRET" in text
    assert "EnvironmentFile=" not in text
    assert "LoadCredential=" not in text
    assert "5555" not in text and "9000" not in text
    assert "king_watchdog" not in text


def _expanded_command(line: str, tmp_path: Path) -> list[str]:
    replacements = {
        "@BONGUS_RESEARCH_PYTHON@": sys.executable,
        "@BONGUS_RESEARCH_ROOT@": str(ROOT),
        "@BONGUS_RESEARCH_DATA@": str(tmp_path),
    }
    command = line.split("=", 1)[1]
    for placeholder, value in replacements.items():
        command = command.replace(placeholder, value)
    return command.split()


def test_exact_service_preflight_and_execstart_shape_are_directly_executable(
    tmp_path: Path,
) -> None:
    if not parquet_backend_available():
        pytest.skip("service intentionally fails closed without its Parquet backend")
    lines = UNIT.read_text(encoding="utf-8").splitlines()
    preflight = _expanded_command(next(line for line in lines if line.startswith("ExecStartPre=")), tmp_path)
    checked = subprocess.run(
        preflight,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert checked.returncode == 0, checked.stderr
    assert json.loads(checked.stdout)["mode"] == "startup_check"

    service = _expanded_command(next(line for line in lines if line.startswith("ExecStart=")), tmp_path)
    imported = subprocess.run(
        [*service, "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert imported.returncode == 0, imported.stderr
    assert "--continuous" in imported.stdout


def test_clean_research_installer_consumes_exact_pinned_offline_backend() -> None:
    requirements = [
        line.strip()
        for line in (ROOT / "requirements-cross-venue.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    installer = INSTALLER.read_text(encoding="utf-8")
    assert requirements == ["pyarrow==23.0.1"]
    assert 'dependency_lines != ["pyarrow==23.0.1"]' in installer
    assert "--no-index --no-cache-dir" in installer
    assert "--only-binary=:all:" in installer
    assert "wheelhouse-cross-venue" in installer
    assert "research-release-manifest.json" in installer
    assert "--no-deps" in installer
    assert '"$PYTHON_REAL" -I -' in installer
    assert '"$VENV_PYTHON" -I -m pip --isolated install' in installer
    assert "--startup-check" in installer
    assert 'pyarrow.Codec.is_available("zstd")' in installer
    assert "systemd-analyze verify" in installer
    assert "systemctl enable" in installer
    assert "systemctl start" not in installer


@pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("systemd-analyze") is None,
    reason="systemd unit verification is a Linux packaging gate",
)
def test_rendered_research_unit_passes_systemd_analyze(tmp_path: Path) -> None:
    text = UNIT.read_text(encoding="utf-8")
    user = subprocess.run(["id", "-un"], check=True, capture_output=True, text=True).stdout.strip()
    group = subprocess.run(["id", "-gn"], check=True, capture_output=True, text=True).stdout.strip()
    replacements = {
        "@BONGUS_RESEARCH_ROOT@": str(ROOT),
        "@BONGUS_RESEARCH_PYTHON@": sys.executable,
        "@BONGUS_RESEARCH_DATA@": str(tmp_path / "research-data"),
        "@BONGUS_LIVE_DATA@": str(tmp_path / "live-data"),
        "User=bongus-research": f"User={user}",
        "Group=bongus-research": f"Group={group}",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    assert "@BONGUS_" not in text
    rendered = tmp_path / "bongus-research.service"
    rendered.write_text(text, encoding="utf-8")
    checked = subprocess.run(
        ["systemd-analyze", "verify", str(rendered)],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr
