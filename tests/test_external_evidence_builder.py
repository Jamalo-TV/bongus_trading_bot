from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.build_masterplan_external_evidence import (
    EVIDENCE_KINDS,
    build_manifest,
)


def _write_artifact(path: Path, evidence_kind: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "evidence_kind": evidence_kind,
                "metrics": {"measured": True},
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return path


def test_manifest_binds_only_portable_hash_verified_artifacts(tmp_path: Path) -> None:
    output = tmp_path / "master_execution_plan_external_evidence.json"
    artifact_paths = {
        evidence_kind: _write_artifact(
            tmp_path / "evidence" / f"{evidence_kind}.json",
            evidence_kind,
        )
        for evidence_kind in EVIDENCE_KINDS
    }

    manifest = build_manifest(output_path=output, artifact_paths=artifact_paths)

    assert manifest["schema_version"] == 2
    assert manifest["policy"] == {
        "live_entries_resumed": False,
        "local_tests_are_promotion_evidence": False,
    }
    assert set(manifest["artifacts"]) == set(EVIDENCE_KINDS)
    for evidence_kind, artifact_path in artifact_paths.items():
        reference = manifest["artifacts"][evidence_kind]
        assert reference["path"] == f"evidence/{evidence_kind}.json"
        assert reference["sha256"] == hashlib.sha256(artifact_path.read_bytes()).hexdigest()


def test_manifest_rejects_artifacts_outside_its_portable_root(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    artifact = _write_artifact(tmp_path / "outside.json", "signed_testnet")

    with pytest.raises(ValueError, match="must be beneath"):
        build_manifest(
            output_path=bundle / "manifest.json",
            artifact_paths={"signed_testnet": artifact},
        )


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"schema_version": 2, "evidence_kind": "signed_testnet", "metrics": {}}, "schema_version"),
        ({"schema_version": 1, "evidence_kind": "operations", "metrics": {}}, "wrong evidence_kind"),
        ({"schema_version": 1, "evidence_kind": "signed_testnet", "metrics": []}, "metrics"),
    ],
)
def test_manifest_rejects_mislabeled_or_generic_json(
    tmp_path: Path,
    payload: dict,
    message: str,
) -> None:
    artifact = tmp_path / "artifact.json"
    artifact.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        build_manifest(
            output_path=tmp_path / "manifest.json",
            artifact_paths={"signed_testnet": artifact},
        )


@pytest.mark.parametrize(
    ("encoded", "message"),
    [
        (
            '{"schema_version":1,"evidence_kind":"signed_testnet",'
            '"metrics":{"value":NaN}}',
            "non-finite JSON",
        ),
        (
            '{"schema_version":1,"evidence_kind":"signed_testnet",'
            '"metrics":{},"metrics":{}}',
            "duplicate JSON key",
        ),
    ],
)
def test_manifest_rejects_ambiguous_json(
    tmp_path: Path,
    encoded: str,
    message: str,
) -> None:
    artifact = tmp_path / "artifact.json"
    artifact.write_text(encoded, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        build_manifest(
            output_path=tmp_path / "manifest.json",
            artifact_paths={"signed_testnet": artifact},
        )
