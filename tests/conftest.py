from __future__ import annotations

import os
import shutil
import socket
import tempfile
from collections.abc import Generator
from pathlib import Path
from uuid import uuid4

import pytest


_REPO_TEMP_ROOT = Path(__file__).resolve().parents[1] / "codex_test_tmp"


def _ensure_repo_temp_root() -> Path:
    _REPO_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    temp_root = str(_REPO_TEMP_ROOT)
    os.environ["TMP"] = temp_root
    os.environ["TEMP"] = temp_root
    tempfile.tempdir = temp_root
    return _REPO_TEMP_ROOT


_ensure_repo_temp_root()


def pytest_sessionstart(session: pytest.Session) -> None:
    """Tests must never inject their synthetic intents into a running bot."""
    del session
    for port in (5555, 9000):
        with socket.socket() as probe:
            probe.settimeout(0.25)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                raise pytest.UsageError(
                    f"Bongus IPC port {port} is occupied. Stop the runtime before "
                    "testing; synthetic test intents must remain isolated."
                )


@pytest.fixture
def tmp_path() -> Generator[Path, None, None]:
    path = _ensure_repo_temp_root() / f"tmp_{uuid4().hex}"
    path.mkdir(parents=True, exist_ok=False)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
