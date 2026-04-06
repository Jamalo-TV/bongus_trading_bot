from __future__ import annotations

import os
import shutil
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


@pytest.fixture
def tmp_path() -> Generator[Path, None, None]:
    path = _ensure_repo_temp_root() / f"tmp_{uuid4().hex}"
    path.mkdir(parents=True, exist_ok=False)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
