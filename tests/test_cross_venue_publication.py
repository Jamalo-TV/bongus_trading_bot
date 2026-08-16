from __future__ import annotations

import base64
import json
import subprocess
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

import bongus.research.cross_venue.artifacts as artifact_module
from bongus.research.cross_venue.artifacts import (
    ParquetArtifactWriter,
    ParquetBackendUnavailable,
    PyArrowZstdBackend,
    parquet_backend_available,
    verify_dataset,
)
from bongus.research.cross_venue.publication import ResearchArtifactPublisher
from bongus.research.cross_venue.schema import CanonicalAsset, ReservedCapital, Venue
from bongus.research.cross_venue.storage import OpportunitySnapshot, RawSnapshotRecord
from scripts import collect_binance_hyperliquid_shadow

ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "cross_venue" / "raw_snapshots.json"
COLLECTOR = ROOT / "scripts" / "collect_binance_hyperliquid_shadow.py"
VERIFIER = ROOT / "scripts" / "verify_cross_venue_dataset.py"


def _require_parquet() -> None:
    if not parquet_backend_available():
        pytest.skip("PyArrow/Zstd is not provisioned; the service startup gate fails closed")


def test_direct_fixture_collection_publishes_and_deep_verifies_parquet(
    tmp_path: Path,
) -> None:
    _require_parquet()
    database = tmp_path / "research.db"
    artifact_root = tmp_path / "artifacts"
    collected = subprocess.run(
        [
            sys.executable,
            str(COLLECTOR),
            "--fixture",
            str(FIXTURE),
            "--database",
            str(database),
            "--artifact-root",
            str(artifact_root),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert collected.returncode == 0, collected.stderr
    payload = json.loads(collected.stdout)
    assert payload["mode"] == "fixture"
    assert payload["processed_events"] == 2

    report = verify_dataset(artifact_root)
    assert report.valid is True
    assert report.row_count == 2
    assert report.manifest_count == 2
    rows = []
    backend = PyArrowZstdBackend()
    for parquet_path in artifact_root.rglob("*.parquet"):
        rows.extend(json.loads(row_json) for _, _, _, row_json in backend.inspect(parquet_path).event_rows)
    assert {row["event_type"] for row in rows} == {"raw_http_snapshot"}
    decoded = {row["event_id"]: base64.b64decode(row["payload"]["payload_base64"]) for row in rows}
    assert all(decoded.values())

    verified = subprocess.run(
        [
            sys.executable,
            str(VERIFIER),
            str(artifact_root),
            "--as-of-time-ns",
            "1000000000000000000",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert verified.returncode == 0, verified.stderr
    assert json.loads(verified.stdout)["valid"] is True


def test_startup_check_replays_sqlite_backlog_idempotently(tmp_path: Path) -> None:
    _require_parquet()
    database = tmp_path / "research.db"
    artifact_root = tmp_path / "artifacts"
    first = subprocess.run(
        [
            sys.executable,
            str(COLLECTOR),
            "--fixture",
            str(FIXTURE),
            "--database",
            str(database),
            "--artifact-root",
            str(artifact_root),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert first.returncode == 0, first.stderr
    manifests_before = tuple(sorted(path.relative_to(artifact_root) for path in artifact_root.rglob("*.manifest.json")))

    startup = subprocess.run(
        [
            sys.executable,
            str(COLLECTOR),
            "--database",
            str(database),
            "--artifact-root",
            str(artifact_root),
            "--startup-check",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert startup.returncode == 0, startup.stderr
    assert json.loads(startup.stdout)["backlog_raw_rows"] == 2
    manifests_after = tuple(sorted(path.relative_to(artifact_root) for path in artifact_root.rglob("*.manifest.json")))
    assert manifests_after == manifests_before
    assert verify_dataset(artifact_root).valid is True


def test_normalized_opportunity_publication_preserves_causal_total_capital_denominator(
    tmp_path: Path,
) -> None:
    _require_parquet()
    base = 1_700_000_000_000_000_000
    raw = RawSnapshotRecord(
        event_id="source-binance",
        dataset="funding_quotes",
        venue=Venue.BINANCE,
        canonical_asset=CanonicalAsset.BTC,
        venue_symbol="BTCUSDT",
        contract_id="BTCUSDT:PERPETUAL",
        endpoint="/fapi/v1/premiumIndex",
        request_method="GET",
        source_time_ns=base,
        capture_time_ns=base + 1,
        receive_time_ns=base + 2,
        available_time_ns=base + 3,
        persistence_time_ns=base + 4,
        http_status=200,
        response_headers={"content-type": "application/json"},
        payload_bytes=b"{}",
        code_sha256="a" * 64,
        configuration_sha256="b" * 64,
    )
    second = replace(
        raw,
        event_id="source-hyperliquid",
        venue=Venue.HYPERLIQUID,
        venue_symbol="BTC",
        contract_id="core:BTC",
        endpoint="/info",
        request_method="POST",
        capture_time_ns=base + 5,
        receive_time_ns=base + 6,
        available_time_ns=base + 7,
        persistence_time_ns=base + 8,
    )
    opportunity = OpportunitySnapshot(
        event_id="opportunity-btc",
        canonical_asset=CanonicalAsset.BTC,
        capture_time_ns=base + 10,
        receive_time_ns=base + 11,
        available_time_ns=base + 12,
        persistence_time_ns=base + 13,
        source_event_ids=(raw.event_id, second.event_id),
        matched_base_quantity=Decimal("0.01"),
        binance_long_entry_price=Decimal("50000"),
        hyperliquid_short_entry_price=Decimal("50001"),
        holding_period_days=Decimal("1"),
        expected_funding_pnl_usd=Decimal("2"),
        expected_executable_price_pnl_usd=Decimal("0"),
        expected_commissions_usd=Decimal("0.5"),
        stablecoin_conversion_cost_usd=Decimal("0.1"),
        collateral_opportunity_cost_usd=Decimal("0.1"),
        repair_failure_cost_usd=Decimal("0.1"),
        reserved_capital=ReservedCapital(
            binance_collateral_usd=Decimal("100"),
            hyperliquid_collateral_usd=Decimal("100"),
            liquidation_buffers_usd=Decimal("50"),
            idle_transfer_buffer_usd=Decimal("25"),
        ),
        code_sha256="a" * 64,
        configuration_sha256="b" * 64,
    )
    result = ResearchArtifactPublisher(ParquetArtifactWriter(tmp_path)).publish_records(
        raw_records=(raw, second),
        opportunity_records=(opportunity,),
    )
    assert result.raw_rows == 2
    assert result.opportunity_rows == 1
    report = verify_dataset(tmp_path)
    assert report.valid is True and report.row_count == 3
    opportunity_path = next(tmp_path.glob("dataset=opportunity_snapshots/**/*.parquet"))
    row_json = PyArrowZstdBackend().inspect(opportunity_path).event_rows[0][3]
    payload = json.loads(row_json)["payload"]
    assert payload["decision_time_ns"] == base + 10
    assert payload["source_available_time_ns"] == base + 7
    assert payload["reserved_capital"]["total_reserved_capital_usd"] == "275"


def test_collection_fails_before_opening_database_when_parquet_backend_is_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(artifact_module, "_PYARROW", None)
    monkeypatch.setattr(artifact_module, "_PARQUET", None)
    database = tmp_path / "research.db"
    with pytest.raises(ParquetBackendUnavailable, match="unavailable"):
        collect_binance_hyperliquid_shadow.main(
            [
                "--fixture",
                str(FIXTURE),
                "--database",
                str(database),
                "--artifact-root",
                str(tmp_path / "artifacts"),
            ]
        )
    assert not database.exists()
