from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

from bongus.research.cross_venue.artifacts import parquet_backend_available
from bongus.research.cross_venue.boundary import (
    ResearchBoundaryViolation,
    assert_default_research_boundary,
    assert_research_boundary,
)
from bongus.research.cross_venue.cadence import cadence_for_dataset
from bongus.research.cross_venue.collector import PublicResearchCollector
from bongus.research.cross_venue.feeds import (
    HttpMethod,
    JsonHttpResponse,
    QueryValue,
)
from bongus.research.cross_venue.schema import CanonicalAsset, Venue
from bongus.research.cross_venue.storage import ResearchStore
from scripts import (
    backtest_binance_hyperliquid,
    collect_binance_hyperliquid_shadow,
    evaluate_binance_hyperliquid,
    replay_binance_hyperliquid,
    report_binance_hyperliquid,
)

ROOT = Path(__file__).parents[1]
RAW_FIXTURE = ROOT / "tests" / "fixtures" / "cross_venue" / "raw_snapshots.json"
EVALUATION_FIXTURE = ROOT / "tests" / "fixtures" / "cross_venue" / "evaluation.json"
ENTRYPOINTS = (
    ROOT / "scripts" / "screen_binance_hyperliquid_history.py",
    ROOT / "scripts" / "collect_binance_hyperliquid_shadow.py",
    ROOT / "scripts" / "replay_binance_hyperliquid.py",
    ROOT / "scripts" / "backtest_binance_hyperliquid.py",
    ROOT / "scripts" / "report_binance_hyperliquid.py",
    ROOT / "scripts" / "verify_cross_venue_dataset.py",
    ROOT / "scripts" / "evaluate_binance_hyperliquid.py",
    ROOT / "scripts" / "probe_cross_venue_region.py",
    ROOT / "scripts" / "evaluate_cross_venue_regions.py",
)


class PublicFixtureTransport:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code

    def request(
        self,
        *,
        method: HttpMethod,
        url: str,
        query: Mapping[str, QueryValue] | None = None,
        body: Mapping[str, object] | None = None,
        timeout_seconds: int = 10,
    ) -> JsonHttpResponse:
        del method, query, body, timeout_seconds
        return JsonHttpResponse(
            status_code=self.status_code,
            payload={"time": 1, "fixture": True},
            url=url,
            headers={"content-type": "application/json", "x-fixture": "yes"},
            raw_body=b'{ "time": 1, "fixture": true }',
        )


class FailedPublicTransport:
    def request(
        self,
        *,
        method: HttpMethod,
        url: str,
        query: Mapping[str, QueryValue] | None = None,
        body: Mapping[str, object] | None = None,
        timeout_seconds: int = 10,
    ) -> JsonHttpResponse:
        del method, url, query, body, timeout_seconds
        raise OSError("fixture transport failure")


def test_default_boundary_accepts_all_entrypoints_and_rejects_forbidden_imports(
    tmp_path: Path,
) -> None:
    for entrypoint in ENTRYPOINTS:
        digest = assert_default_research_boundary(entrypoint)
        assert len(digest) == 64

    forbidden = tmp_path / "forbidden.py"
    forbidden.write_text("from dotenv import load_dotenv\n", encoding="utf-8")
    with pytest.raises(ResearchBoundaryViolation, match="forbidden research import"):
        assert_research_boundary((forbidden,))

    forbidden.write_text("DATABASE = 'state.db'\n", encoding="utf-8")
    with pytest.raises(ResearchBoundaryViolation, match="forbidden live/credential literal"):
        assert_research_boundary((forbidden,))

    forbidden.write_text("from bongus.core import config\n", encoding="utf-8")
    with pytest.raises(ResearchBoundaryViolation, match="forbidden research import"):
        assert_research_boundary((forbidden,))

    forbidden.write_text("import os\nVALUE = os.getenv('TOKEN')\n", encoding="utf-8")
    with pytest.raises(ResearchBoundaryViolation, match="environment"):
        assert_research_boundary((forbidden,))

    forbidden.write_text("import hmac\n", encoding="utf-8")
    with pytest.raises(ResearchBoundaryViolation, match="forbidden research import"):
        assert_research_boundary((forbidden,))

    forbidden.write_text("import requests\n", encoding="utf-8")
    with pytest.raises(ResearchBoundaryViolation, match="non-standard research import"):
        assert_research_boundary((forbidden,))

    forbidden.write_text("import pyarrow\n", encoding="utf-8")
    with pytest.raises(ResearchBoundaryViolation, match="non-standard research import"):
        assert_research_boundary((forbidden,))
    optional_backend = tmp_path / "artifacts.py"
    optional_backend.write_text("import pyarrow\n", encoding="utf-8")
    assert len(assert_research_boundary((optional_backend,))) == 64

    forbidden.write_text("PORT = 5555\n", encoding="utf-8")
    with pytest.raises(ResearchBoundaryViolation, match="live IPC port"):
        assert_research_boundary((forbidden,))


def test_public_collector_persists_http_metadata_without_real_network(tmp_path: Path) -> None:
    ticks = iter(range(1_000, 10_000, 10))
    with ResearchStore(tmp_path / "research.db") as store:
        collector = PublicResearchCollector(
            store,
            transport=PublicFixtureTransport(),
            clock_ns=lambda: next(ticks),
        )
        result = collector.collect_once(include_books=False, include_funding_history=False)
        assert result.inserted_snapshots == 6
        assert result.failed_snapshots == 0
        rows = tuple(store.iter_raw_snapshots())
        assert len(rows) == 6
        assert all(row.http_status == 200 for row in rows)
        assert all(row.response_headers["x-fixture"] == "yes" for row in rows)
        assert all(row.payload_bytes == b'{ "time": 1, "fixture": true }' for row in rows)


def test_public_collector_declares_both_venues_finalized_funding_history(tmp_path: Path) -> None:
    with ResearchStore(tmp_path / "research.db") as store:
        collector = PublicResearchCollector(store, transport=PublicFixtureTransport())
        histories = tuple(
            target for target in collector.targets(include_books=False) if target.dataset == "final_funding_settlements"
        )
    assert len(histories) == 10
    assert {target.canonical_asset for target in histories} == set(CanonicalAsset)
    assert {target.venue for target in histories} == {Venue.BINANCE, Venue.HYPERLIQUID}
    assert all(cadence_for_dataset(target.dataset) for target in collector.targets())


def test_public_collector_records_rejected_http_responses_as_gaps(
    tmp_path: Path,
) -> None:
    ticks = iter(range(20_000, 30_000, 10))
    with ResearchStore(tmp_path / "research.db") as store:
        result = PublicResearchCollector(
            store,
            transport=PublicFixtureTransport(status_code=429),
            clock_ns=lambda: next(ticks),
        ).collect_once(include_books=False, include_funding_history=False)
        assert result.inserted_snapshots == 6
        assert result.failed_snapshots == 6
        rows = tuple(store.iter_raw_snapshots())
        assert all(row.http_status == 429 for row in rows)
        assert all("public_response_rejected" in row.quality_flags for row in rows)


def test_public_collector_records_transport_failures_as_gap_rows(tmp_path: Path) -> None:
    ticks = iter(range(30_000, 40_000, 10))
    with ResearchStore(tmp_path / "research.db") as store:
        result = PublicResearchCollector(
            store,
            transport=FailedPublicTransport(),
            clock_ns=lambda: next(ticks),
        ).collect_once(include_books=False, include_funding_history=False)
        assert result.inserted_snapshots == 6
        assert result.failed_snapshots == 6
        rows = tuple(store.iter_raw_snapshots())
        assert all(row.http_status == 599 for row in rows)
        assert all("transport_failure" in row.quality_flags for row in rows)


def test_offline_clis_replay_backtest_and_report_without_network(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    if not parquet_backend_available():
        pytest.skip("collector service correctly fails closed without PyArrow/Zstd")
    database = tmp_path / "research.db"
    artifacts = tmp_path / "artifacts"
    assert (
        collect_binance_hyperliquid_shadow.main(
            [
                "--fixture",
                str(RAW_FIXTURE),
                "--database",
                str(database),
                "--artifact-root",
                str(artifacts),
            ]
        )
        == 0
    )
    collected = json.loads(capsys.readouterr().out)
    assert collected["mode"] == "fixture"
    assert collected["processed_events"] == 2

    assert replay_binance_hyperliquid.main([str(RAW_FIXTURE)]) == 0
    replayed = json.loads(capsys.readouterr().out)
    assert replayed["input_reordered"] is True

    report_path = tmp_path / "evaluation-report.json"
    assert backtest_binance_hyperliquid.main([str(EVALUATION_FIXTURE), "--output", str(report_path)]) == 0
    backtested = json.loads(capsys.readouterr().out)
    assert backtested["windows"] == 1
    assert report_path.is_file()

    assert report_binance_hyperliquid.main([str(report_path)]) == 0
    reported = json.loads(capsys.readouterr().out)
    assert reported["authority"] == "research_evidence_only"
    assert reported["baseline_outcomes"] == 1

    evidence_path = tmp_path / "evidence-report.json"
    assert evaluate_binance_hyperliquid.main([str(EVALUATION_FIXTURE), "--output", str(evidence_path)]) == 0
    evaluated = json.loads(capsys.readouterr().out)
    assert evaluated["verdict"] == "collector_qa_only"
    assert evaluated["grants_live_authority"] is False


def test_collector_remains_offline_without_explicit_mode() -> None:
    with pytest.raises(SystemExit):
        collect_binance_hyperliquid_shadow.main([])
    with pytest.raises(SystemExit):
        collect_binance_hyperliquid_shadow.main(
            [
                "--artifact-root",
                "artifacts",
                "--fixture",
                str(RAW_FIXTURE),
                "--allow-network",
            ]
        )


def test_every_research_cli_imports_when_invoked_directly_from_release_root() -> None:
    for entrypoint in ENTRYPOINTS:
        result = subprocess.run(
            [sys.executable, str(entrypoint), "--help"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, f"{entrypoint.name}: {result.stderr}"
        assert "usage:" in result.stdout.casefold()


def test_isolated_requirements_pin_only_the_artifact_backend() -> None:
    lines = [
        line.strip()
        for line in (ROOT / "requirements-cross-venue.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert lines == ["pyarrow==23.0.1"]
