from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from scripts.build_research_release import (
    ENTRYPOINTS,
    MANIFEST_DIGEST_NAME,
    MANIFEST_NAME,
    ResearchReleaseError,
    build_release,
    verify_release,
)

ROOT = Path(__file__).parents[1]
WHEEL_NAME = "pyarrow-23.0.1-cp311-cp311-manylinux_2_28_x86_64.whl"


def _write_fixture_wheel(wheelhouse: Path) -> Path:
    wheelhouse.mkdir()
    wheel = wheelhouse / WHEEL_NAME
    with zipfile.ZipFile(wheel, "w") as archive:
        members = {
            "pyarrow-23.0.1.dist-info/METADATA": (
                "Metadata-Version: 2.1\nName: pyarrow\nVersion: 23.0.1\n\n"
            ),
            "pyarrow-23.0.1.dist-info/RECORD": "",
            "pyarrow-23.0.1.dist-info/WHEEL": (
                "Wheel-Version: 1.0\nTag: cp311-cp311-manylinux_2_28_x86_64\n\n"
            ),
        }
        for name, content in members.items():
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            archive.writestr(info, content)
    return wheel


def _build(tmp_path: Path, name: str) -> tuple[Path, Path]:
    wheelhouse = tmp_path / f"{name}-wheelhouse"
    _write_fixture_wheel(wheelhouse)
    output = tmp_path / name
    archive = tmp_path / f"{name}.zip"
    build_release(
        source_root=ROOT,
        output=output,
        archive_path=archive,
        wheelhouse_source=wheelhouse,
    )
    return output, archive


def test_research_release_is_deterministic_exact_and_directly_executable(tmp_path: Path) -> None:
    first, first_archive = _build(tmp_path, "first")
    second, second_archive = _build(tmp_path, "second")

    assert first_archive.read_bytes() == second_archive.read_bytes()
    assert (first / MANIFEST_NAME).read_bytes() == (second / MANIFEST_NAME).read_bytes()
    assert verify_release(first)["release_kind"] == "bongus-cross-venue-research"

    manifest = json.loads((first / MANIFEST_NAME).read_text(encoding="utf-8"))
    packaged = {entry["path"] for entry in manifest["files"]}
    assert manifest["entrypoints"] == list(ENTRYPOINTS)
    assert {f"wheelhouse-cross-venue/{WHEEL_NAME}"} <= packaged
    assert {MANIFEST_NAME, MANIFEST_DIGEST_NAME}.isdisjoint(packaged)
    assert not any(
        path == ".env"
        or path == "live_config.json"
        or path.startswith("bongus/engine/")
        or path.startswith("bongus/ipc/")
        or path.startswith("bongus/core/")
        or path.endswith("state.db")
        for path in packaged
    )
    assert "from bongus.exchanges.base" not in (
        first / "bongus" / "exchanges" / "__init__.py"
    ).read_text(encoding="utf-8")

    with zipfile.ZipFile(first_archive) as archive:
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist())
        assert archive.namelist() == sorted(archive.namelist())

    for relative in ENTRYPOINTS:
        checked = subprocess.run(
            [sys.executable, "-I", str(first / relative), "--help"],
            cwd=first,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"},
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert checked.returncode == 0, f"{relative}: {checked.stderr}"
        assert "usage:" in checked.stdout.casefold()


def test_research_release_rejects_tamper_extra_content_and_wrong_wheel(tmp_path: Path) -> None:
    release, _ = _build(tmp_path, "release")
    target = release / "bongus" / "research" / "cross_venue" / "schema.py"
    target.write_text(target.read_text(encoding="utf-8") + "\n# tamper\n", encoding="utf-8")
    with pytest.raises(ResearchReleaseError, match="hash or size mismatch"):
        verify_release(release)

    release, _ = _build(tmp_path, "extra")
    (release / ".env").write_text("BINANCE_API_KEY=forbidden\n", encoding="utf-8")
    with pytest.raises(ResearchReleaseError, match="inventory mismatch"):
        verify_release(release)

    wrong_wheelhouse = tmp_path / "wrong-wheelhouse"
    wrong_wheelhouse.mkdir()
    (wrong_wheelhouse / "pyarrow-23.0.1-py3-none-any.whl").write_bytes(b"not a wheel")
    with pytest.raises(ResearchReleaseError, match="pinned target"):
        build_release(
            source_root=ROOT,
            output=tmp_path / "wrong",
            archive_path=tmp_path / "wrong.zip",
            wheelhouse_source=wrong_wheelhouse,
        )


def test_research_installer_and_ci_are_fail_closed_and_enable_only() -> None:
    installer = (ROOT / "deployment" / "Install-BongusResearch.sh").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert installer.index("research-release-manifest.json") < installer.index("groupadd --system")
    assert installer.index("research-release-manifest.json") < installer.index("-m venv --copies")
    assert "release contains content outside the research-only allowlist" in installer
    assert "st_uid != 0" in installer and "st_mode & 0o022" in installer
    assert "useradd --system" in installer and "--shell \"$NOLOGIN_SHELL\"" in installer
    assert '"$account_uid" -gt 0' in installer and '"$account_uid" -lt 1000' in installer
    assert '"$all_groups" == "$SERVICE_GROUP"' in installer
    assert "--no-index --no-cache-dir" in installer
    assert "--only-binary=:all: --no-deps" in installer
    assert '"$PYTHON_REAL" -I -' in installer
    assert "-m pip --isolated" in installer
    assert 'pyarrow.Codec.is_available("zstd")' in installer
    assert all(Path(entrypoint).name in installer for entrypoint in ENTRYPOINTS)
    assert "systemd-analyze verify" in installer
    assert 'systemctl enable "${SERVICE_NAME}.service"' in installer
    assert "systemctl start" not in installer
    assert "--allow-network" not in installer

    assert "requirements-cross-venue.txt" in workflow
    assert "-I -m pip --isolated" in workflow
    assert 'pyarrow.__version__ == "23.0.1"' in workflow
    assert 'pyarrow.Codec.is_available("zstd")' in workflow
    assert "scripts/build_research_release.py" in workflow
