from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

import bongus.research.cross_venue.artifacts as artifacts
from bongus.research.cross_venue.artifacts import (
    ArtifactIntegrityError,
    ArtifactRow,
    GapRow,
    ParquetArtifactWriter,
    ParquetBackendUnavailable,
    audit_retention,
    load_artifact_manifest,
    parquet_backend_available,
    verify_artifact,
    verify_dataset,
)
from bongus.research.cross_venue.schema import CanonicalAsset, Venue
from scripts import verify_cross_venue_dataset

CODE_HASH = "a" * 64
CONFIG_HASH = "b" * 64
BASE_TIME_NS = 1_700_000_000_000_000_000


def _row(
    *,
    event_id: str = "artifact-1",
    dataset: str = "bbo",
    venue: Venue = Venue.BINANCE,
    venue_symbol: str = "BTCUSDT",
    offset_ns: int = 0,
    payload: dict[str, object] | None = None,
) -> ArtifactRow:
    capture = BASE_TIME_NS + offset_ns
    return ArtifactRow(
        event_id=event_id,
        dataset=dataset,
        venue=venue,
        canonical_asset=CanonicalAsset.BTC,
        venue_symbol=venue_symbol,
        contract_id="binance:BTCUSDT:perpetual",
        event_type="snapshot",
        source_time_ns=capture - 10,
        capture_time_ns=capture,
        receive_time_ns=capture + 1,
        available_time_ns=capture + 2,
        persistence_time_ns=capture + 3,
        code_sha256=CODE_HASH,
        configuration_sha256=CONFIG_HASH,
        payload=payload or {"rate": Decimal("0.000100"), "quantity": "1.25"},
    )


def _require_parquet() -> None:
    if not parquet_backend_available():
        pytest.skip("PyArrow/Zstd is not installed in this development environment")


def test_zstd_parquet_publish_manifest_and_deep_verification(tmp_path: Path) -> None:
    _require_parquet()
    published = ParquetArtifactWriter(tmp_path).write((_row(), _row(event_id="artifact-2", offset_ns=100)))

    assert published.data_path.suffix == ".parquet"
    assert "dataset=bbo" in published.data_path.as_posix()
    assert "venue=binance" in published.data_path.as_posix()
    assert not tuple(tmp_path.rglob("*.tmp"))
    assert published.manifest.row_count == 2
    assert published.manifest.retention_class == "raw_14d_min"
    assert published.manifest.retain_until_ns is not None
    assert load_artifact_manifest(published.manifest_path) == published.manifest

    verified = verify_artifact(tmp_path, published.manifest_path)
    assert verified.manifest.file_sha256 == published.manifest.file_sha256
    report = verify_dataset(tmp_path)
    assert report.valid is True
    assert report.manifest_count == 1
    assert report.row_count == 2
    assert len(report.report_sha256) == 64


def test_exact_decimal_is_persisted_as_text_and_future_join_is_reported(tmp_path: Path) -> None:
    _require_parquet()
    row = _row(
        payload={
            "rate": Decimal("0.000100"),
            "decision_time_ns": BASE_TIME_NS,
            "source_available_time_ns": BASE_TIME_NS + 1,
        }
    )
    published = ParquetArtifactWriter(tmp_path).write((row,))
    inspection = artifacts.PyArrowZstdBackend().inspect(published.data_path)
    payload = json.loads(inspection.event_rows[0][3])
    assert payload["payload"]["rate"] == "0.000100"

    report = verify_dataset(tmp_path)
    assert report.valid is False
    assert report.future_data_event_ids == ("artifact-1",)


def test_tampering_and_mixed_partitions_fail_closed(tmp_path: Path) -> None:
    _require_parquet()
    writer = ParquetArtifactWriter(tmp_path)
    with pytest.raises(ArtifactIntegrityError, match="one exact partition"):
        writer.write((_row(), _row(event_id="later", offset_ns=3_600_000_000_000)))

    published = writer.write((_row(),))
    with published.data_path.open("ab") as handle:
        handle.write(b"tamper")
    with pytest.raises(ArtifactIntegrityError, match="SHA-256"):
        verify_artifact(tmp_path, published.manifest_path)


def test_gap_rows_are_permanent_and_retention_audit_never_deletes(tmp_path: Path) -> None:
    _require_parquet()
    gap = GapRow.deterministic(
        dataset="top20_book",
        venue=Venue.HYPERLIQUID,
        canonical_asset=CanonicalAsset.BTC,
        venue_symbol="BTC",
        contract_id="hyperliquid:BTC:core-perpetual",
        scheduled_time_ns=BASE_TIME_NS,
        capture_time_ns=BASE_TIME_NS,
        receive_time_ns=BASE_TIME_NS + 1,
        available_time_ns=BASE_TIME_NS + 2,
        persistence_time_ns=BASE_TIME_NS + 3,
        reason="transport_timeout",
        dropped_snapshots=2,
        code_sha256=CODE_HASH,
        configuration_sha256=CONFIG_HASH,
    )
    published = ParquetArtifactWriter(tmp_path).write((gap.as_artifact_row(),))
    assert published.manifest.retention_class == "permanent"
    assert published.manifest.retain_until_ns is None

    raw = ParquetArtifactWriter(tmp_path).write((_row(),))
    audit = audit_retention(
        (published.manifest, raw.manifest),
        as_of_time_ns=BASE_TIME_NS + 15 * artifacts.NANOSECONDS_PER_DAY,
    )
    assert audit.permanent_artifacts == (published.manifest.relative_data_path,)
    assert audit.eligible_raw_book_artifacts == (raw.manifest.relative_data_path,)
    assert published.data_path.exists() and raw.data_path.exists()


def test_missing_optional_backend_refuses_to_write_parquet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(artifacts, "_PYARROW", None)
    monkeypatch.setattr(artifacts, "_PARQUET", None)
    with pytest.raises(ParquetBackendUnavailable, match="unavailable"):
        ParquetArtifactWriter(tmp_path)


def test_conflicting_duplicate_event_ids_across_partitions_are_invalid(tmp_path: Path) -> None:
    _require_parquet()
    writer = ParquetArtifactWriter(tmp_path)
    writer.write((_row(),))
    writer.write(
        (
            replace(
                _row(payload={"rate": "different"}),
                capture_time_ns=BASE_TIME_NS + 3_600_000_000_000,
                receive_time_ns=BASE_TIME_NS + 3_600_000_000_001,
                available_time_ns=BASE_TIME_NS + 3_600_000_000_002,
                persistence_time_ns=BASE_TIME_NS + 3_600_000_000_003,
            ),
        )
    )
    report = verify_dataset(tmp_path)
    assert report.valid is False
    assert report.conflicting_event_ids == ("artifact-1",)


def test_verify_dataset_cli_reports_wall_clock_gates_as_evidence_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _require_parquet()
    ParquetArtifactWriter(tmp_path).write((_row(),))
    assert (
        verify_cross_venue_dataset.main(
            [str(tmp_path), "--as-of-time-ns", str(BASE_TIME_NS + 15 * artifacts.NANOSECONDS_PER_DAY)]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is True
    assert payload["parquet_backend"] == "pyarrow-zstd"
    assert payload["wall_clock_gates"]["forward_oos_90_to_180_days"] == "evidence_required"
