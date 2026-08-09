"""Authenticated, short-lived human approval artifacts for live startup.

The approval key is supplied out of band through the environment.  The
artifact binds the exact effective configuration, complete release inventory,
packaged Rust executable, account, and approved decision artifact. Runtime code
can verify approvals but cannot manufacture one without the operator-held key.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path
from typing import Any, Mapping


class LiveApprovalError(RuntimeError):
    """Raised when live authorization is absent, stale, or not authentic."""


@dataclass(frozen=True, slots=True)
class VerifiedLiveApproval:
    path: Path
    approved_by: str
    approved_at: str
    expires_at: str
    config_sha256: str
    release_manifest_sha256: str
    rust_binary_sha256: str
    decision_artifact_path: Path
    decision_artifact_sha256: str
    account_id: str
    artifact_sha256: str


def sha256_file(path: str | Path) -> str:
    resolved = Path(path).resolve()
    digest = hashlib.sha256()
    try:
        with resolved.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise LiveApprovalError(f"cannot hash approved file {resolved}: {exc}") from exc
    return digest.hexdigest()


def canonical_approval_bytes(payload: Mapping[str, Any]) -> bytes:
    unsigned = {
        str(key): value
        for key, value in payload.items()
        if key != "signature_hmac_sha256"
    }
    try:
        rendered = json.dumps(
            unsigned,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise LiveApprovalError(f"approval artifact is not canonical JSON: {exc}") from exc
    return rendered.encode("utf-8")


def sign_live_approval(payload: Mapping[str, Any], key: bytes) -> str:
    """Return the HMAC for an artifact; used by an offline operator workflow."""

    if len(key) < 32:
        raise LiveApprovalError("live approval key must contain at least 32 bytes")
    return hmac.new(key, canonical_approval_bytes(payload), hashlib.sha256).hexdigest()


def _required_text(payload: Mapping[str, Any], key: str) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise LiveApprovalError(f"approval artifact missing {key}")
    return value


def _sha256_text(payload: Mapping[str, Any], key: str) -> str:
    value = _required_text(payload, key).lower()
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise LiveApprovalError(f"approval artifact {key} is not a SHA-256 digest")
    return value


def _utc_timestamp(payload: Mapping[str, Any], key: str) -> datetime:
    raw = _required_text(payload, key)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LiveApprovalError(f"approval artifact {key} is not ISO-8601") from exc
    if parsed.tzinfo is None:
        raise LiveApprovalError(f"approval artifact {key} must include a timezone")
    return parsed.astimezone(timezone.utc)


def verify_live_approval(
    artifact_path: str | Path,
    *,
    key: bytes,
    expected_config_sha256: str,
    release_manifest_path: str | Path,
    rust_binary_path: str | Path,
    expected_account_id: str | None,
    now: datetime | None = None,
) -> VerifiedLiveApproval:
    path = Path(artifact_path).resolve()
    if not path.is_file():
        raise LiveApprovalError(f"live approval artifact does not exist: {path}")
    try:
        raw_bytes = path.read_bytes()
        payload = json.loads(raw_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise LiveApprovalError(f"cannot read live approval artifact: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise LiveApprovalError("live approval artifact root must be an object")
    if int(payload.get("schema_version") or 0) != 3:
        raise LiveApprovalError("unsupported live approval artifact schema")
    if payload.get("approved") is not True:
        raise LiveApprovalError("live approval artifact is not approved")
    if str(payload.get("trading_mode") or "").strip().lower() != "live":
        raise LiveApprovalError("approval artifact does not authorize live mode")
    if len(key) < 32:
        raise LiveApprovalError("live approval key must contain at least 32 bytes")

    supplied_signature = _sha256_text(payload, "signature_hmac_sha256")
    expected_signature = sign_live_approval(payload, key)
    if not hmac.compare_digest(supplied_signature, expected_signature):
        raise LiveApprovalError("live approval signature mismatch")

    approved_at = _utc_timestamp(payload, "approved_at")
    expires_at = _utc_timestamp(payload, "expires_at")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if approved_at > current:
        raise LiveApprovalError("live approval is future-dated")
    if expires_at <= approved_at or current >= expires_at:
        raise LiveApprovalError("live approval has expired")
    # Deliberately short-lived: a stale human review must not survive material
    # exchange/account drift indefinitely.
    if (expires_at - approved_at).total_seconds() > 24 * 60 * 60:
        raise LiveApprovalError("live approval validity may not exceed 24 hours")

    config_sha256 = _sha256_text(payload, "config_sha256")
    if not hmac.compare_digest(config_sha256, expected_config_sha256.lower()):
        raise LiveApprovalError("live approval config hash mismatch")
    release_manifest_sha256 = _sha256_text(
        payload,
        "release_manifest_sha256",
    )
    actual_release_manifest_sha256 = sha256_file(release_manifest_path)
    if not hmac.compare_digest(
        release_manifest_sha256,
        actual_release_manifest_sha256,
    ):
        raise LiveApprovalError("live approval release manifest hash mismatch")
    rust_binary_sha256 = _sha256_text(payload, "rust_binary_sha256")
    actual_rust_sha256 = sha256_file(rust_binary_path)
    if not hmac.compare_digest(rust_binary_sha256, actual_rust_sha256):
        raise LiveApprovalError("live approval Rust binary hash mismatch")
    account_id = _required_text(payload, "account_id")
    if expected_account_id is not None and not hmac.compare_digest(
        account_id,
        str(expected_account_id).strip(),
    ):
        raise LiveApprovalError("live approval account mismatch")

    decision_artifact_sha256 = _sha256_text(
        payload,
        "decision_artifact_sha256",
    )
    decision_artifact_text = _required_text(payload, "decision_artifact_path")
    decision_artifact_path = Path(decision_artifact_text)
    if not decision_artifact_path.is_absolute():
        decision_artifact_path = path.parent / decision_artifact_path
    decision_artifact_path = decision_artifact_path.resolve()
    actual_decision_artifact_sha256 = sha256_file(decision_artifact_path)
    if not hmac.compare_digest(
        decision_artifact_sha256,
        actual_decision_artifact_sha256,
    ):
        raise LiveApprovalError("live approval decision artifact hash mismatch")
    approved_by = _required_text(payload, "approved_by")
    _required_text(payload, "nonce")
    return VerifiedLiveApproval(
        path=path,
        approved_by=approved_by,
        approved_at=approved_at.isoformat(),
        expires_at=expires_at.isoformat(),
        config_sha256=config_sha256,
        release_manifest_sha256=release_manifest_sha256,
        rust_binary_sha256=rust_binary_sha256,
        decision_artifact_path=decision_artifact_path,
        decision_artifact_sha256=decision_artifact_sha256,
        account_id=account_id,
        artifact_sha256=hashlib.sha256(raw_bytes).hexdigest(),
    )
