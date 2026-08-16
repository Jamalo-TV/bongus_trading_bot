"""Assemble the canonical, content-addressed external-evidence manifest.

This command never turns local tests into promotion evidence.  It records only
validated JSON artifacts already present beneath the output manifest's
directory, so the resulting evidence bundle is portable and independently
re-verifiable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_KINDS: Final = (
    "signed_testnet",
    "safety_window",
    "operations",
    "region_probe",
    "research_forward",
)


def _reject_nonfinite_json(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key is forbidden: {key}")
        result[key] = value
    return result


def _load(path: Path) -> dict[str, Any]:
    payload = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_reject_duplicate_json_keys,
        parse_constant=_reject_nonfinite_json,
    )
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _portable_ref(*, evidence_kind: str, path: Path, evidence_root: Path) -> dict[str, str]:
    root = evidence_root.resolve(strict=True)
    candidate = path.resolve(strict=True)
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{evidence_kind} artifact must be beneath {root}") from exc
    cursor = root
    for part in relative.parts:
        cursor /= part
        if cursor.is_symlink():
            raise ValueError(f"{evidence_kind} artifact path cannot contain symlinks")
    if not candidate.is_file() or candidate.suffix.casefold() != ".json":
        raise ValueError(f"{evidence_kind} artifact must be a JSON file")
    payload = _load(candidate)
    if payload.get("schema_version") != 1:
        raise ValueError(f"{evidence_kind} artifact schema_version must be 1")
    if payload.get("evidence_kind") != evidence_kind:
        raise ValueError(f"{evidence_kind} artifact declares the wrong evidence_kind")
    if not isinstance(payload.get("metrics"), dict):
        raise ValueError(f"{evidence_kind} artifact metrics must be a JSON object")
    return {"path": relative.as_posix(), "sha256": _sha256(candidate)}


def build_manifest(
    *,
    output_path: Path,
    artifact_paths: Mapping[str, Path | None],
) -> dict[str, Any]:
    unknown = set(artifact_paths) - set(EVIDENCE_KINDS)
    if unknown:
        raise ValueError(f"unsupported evidence kinds: {sorted(unknown)}")
    output_parent = output_path.parent.resolve(strict=True)
    artifacts = {
        evidence_kind: _portable_ref(
            evidence_kind=evidence_kind,
            path=path,
            evidence_root=output_parent,
        )
        for evidence_kind, path in artifact_paths.items()
        if path is not None
    }
    return {
        "schema_version": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "policy": {
            "live_entries_resumed": False,
            "local_tests_are_promotion_evidence": False,
        },
        "artifacts": artifacts,
    }


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    for evidence_kind in EVIDENCE_KINDS:
        parser.add_argument(
            f"--{evidence_kind.replace('_', '-')}",
            type=Path,
            help=f"schema-v1 {evidence_kind} JSON beneath the output directory",
        )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT
        / "verification_artifacts"
        / "master_execution_plan_external_evidence.json",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    artifact_paths = {
        evidence_kind: getattr(args, evidence_kind)
        for evidence_kind in EVIDENCE_KINDS
    }
    manifest = build_manifest(output_path=output, artifact_paths=artifact_paths)
    _write_atomic(output, manifest)
    print(
        json.dumps(
            {
                "status": "assembled",
                "output": str(output),
                "sha256": _sha256(output),
                "bound_artifacts": sorted(manifest["artifacts"]),
                "live_entries_resumed": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
