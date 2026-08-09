from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from bongus.core.live_approval import (
    LiveApprovalError,
    sha256_file,
    sign_live_approval,
    verify_live_approval,
)


def _artifact(tmp_path: Path, *, now: datetime) -> tuple[Path, Path, bytes, dict]:
    binary = tmp_path / "execution_engine.exe"
    binary.write_bytes(b"packaged-rust-binary")
    release_manifest = tmp_path / "release-manifest.json"
    release_manifest.write_text('{"production_eligible":true}', encoding="utf-8")
    decision_artifact = tmp_path / "gate-d-decision.json"
    decision_artifact.write_text('{"decision":"GO"}', encoding="utf-8")
    key = b"operator-held-live-approval-key-32-bytes-minimum"
    payload = {
        "schema_version": 3,
        "approved": True,
        "trading_mode": "live",
        "approved_by": "risk-owner@example.test",
        "approved_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=4)).isoformat(),
        "config_sha256": "a" * 64,
        "release_manifest_sha256": sha256_file(release_manifest),
        "rust_binary_sha256": sha256_file(binary),
        "decision_artifact_path": decision_artifact.name,
        "decision_artifact_sha256": sha256_file(decision_artifact),
        "account_id": "account-1",
        "nonce": "review-2026-08-09-001",
    }
    payload["signature_hmac_sha256"] = sign_live_approval(payload, key)
    path = tmp_path / "approval.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path, binary, key, payload


def test_live_approval_binds_config_binary_account_and_expiry(tmp_path: Path) -> None:
    now = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    path, binary, key, _ = _artifact(tmp_path, now=now)

    verified = verify_live_approval(
        path,
        key=key,
        expected_config_sha256="a" * 64,
        release_manifest_path=tmp_path / "release-manifest.json",
        rust_binary_path=binary,
        expected_account_id="account-1",
        now=now + timedelta(minutes=1),
    )

    assert verified.approved_by == "risk-owner@example.test"
    assert verified.rust_binary_sha256 == sha256_file(binary)
    assert verified.decision_artifact_path.name == "gate-d-decision.json"
    assert len(verified.artifact_sha256) == 64


@pytest.mark.parametrize(
    "mutation,match",
    [
        (lambda payload: payload.update(config_sha256="c" * 64), "signature mismatch"),
        (lambda payload: payload.update(account_id="other"), "signature mismatch"),
        (lambda payload: payload.update(approved=False), "not approved"),
    ],
)
def test_tampered_live_approval_fails_closed(
    tmp_path: Path,
    mutation,
    match: str,
) -> None:
    now = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    path, binary, key, payload = _artifact(tmp_path, now=now)
    mutation(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(LiveApprovalError, match=match):
        verify_live_approval(
            path,
            key=key,
            expected_config_sha256="a" * 64,
            release_manifest_path=tmp_path / "release-manifest.json",
            rust_binary_path=binary,
            expected_account_id="account-1",
            now=now + timedelta(minutes=1),
        )


def test_expired_or_overlong_live_approval_fails_closed(tmp_path: Path) -> None:
    now = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    path, binary, key, payload = _artifact(tmp_path, now=now)

    with pytest.raises(LiveApprovalError, match="expired"):
        verify_live_approval(
            path,
            key=key,
            expected_config_sha256="a" * 64,
            release_manifest_path=tmp_path / "release-manifest.json",
            rust_binary_path=binary,
            expected_account_id="account-1",
            now=now + timedelta(hours=5),
        )

    payload["expires_at"] = (now + timedelta(hours=25)).isoformat()
    payload["signature_hmac_sha256"] = sign_live_approval(payload, key)
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(LiveApprovalError, match="may not exceed"):
        verify_live_approval(
            path,
            key=key,
            expected_config_sha256="a" * 64,
            release_manifest_path=tmp_path / "release-manifest.json",
            rust_binary_path=binary,
            expected_account_id="account-1",
            now=now + timedelta(minutes=1),
        )


def test_decision_artifact_content_is_hash_bound(tmp_path: Path) -> None:
    now = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    path, binary, key, payload = _artifact(tmp_path, now=now)
    (tmp_path / payload["decision_artifact_path"]).write_text(
        '{"decision":"NO_GO"}', encoding="utf-8"
    )

    with pytest.raises(LiveApprovalError, match="decision artifact hash mismatch"):
        verify_live_approval(
            path,
            key=key,
            expected_config_sha256="a" * 64,
            release_manifest_path=tmp_path / "release-manifest.json",
            rust_binary_path=binary,
            expected_account_id="account-1",
            now=now + timedelta(minutes=1),
        )


def test_release_manifest_content_is_hash_bound(tmp_path: Path) -> None:
    now = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    path, binary, key, _payload = _artifact(tmp_path, now=now)
    (tmp_path / "release-manifest.json").write_text(
        '{"production_eligible":false}',
        encoding="utf-8",
    )

    with pytest.raises(LiveApprovalError, match="release manifest hash mismatch"):
        verify_live_approval(
            path,
            key=key,
            expected_config_sha256="a" * 64,
            release_manifest_path=tmp_path / "release-manifest.json",
            rust_binary_path=binary,
            expected_account_id="account-1",
            now=now + timedelta(minutes=1),
        )
