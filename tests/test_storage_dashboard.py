import asyncio
import base64
import io
import json
import os
import sqlite3
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest
from fastapi import HTTPException, WebSocketDisconnect
from starlette.responses import StreamingResponse

from bongus.monitoring.storage_observability import (
    collect_database_metrics,
    collect_storage_observability,
    read_storage_snapshot,
    storage_snapshot_path,
)
from bongus.monitoring.web_dashboard import (
    _DEGRADED_SUPPORT_BUNDLE_MAX_BYTES,
    api_storage,
    api_admin_acknowledge_storage_recovery,
    app,
    download_logs,
    websocket_storage,
)
from bongus.monitoring.log_artifacts import DEFAULT_SUPPORT_BUNDLE_MAX_BYTES


def _snapshot_payload(*, state: str = "healthy") -> dict[str, Any]:
    return {
        "generation": 7,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "state": state,
        "instantaneous_state": state,
        "reasons": [],
        "volumes": [],
        "components": [],
        "durability_probes": [],
        "worst_time_to_full_hours": None,
        "risk_increase_blocked": state in {"degraded", "emergency", "critical"},
        "emergency_latched": state in {"emergency", "critical"},
        "healthy_recovery_samples": 0,
        "recovery_samples_required": 3,
        "recovery_ready_for_operator": False,
        "integrity_ok": True,
        "exchange_reconciled": True,
        "active_faults": [],
        "reserve": None,
    }


def _write_snapshot(path: Path, *, state: str = "healthy") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_snapshot_payload(state=state)) + "\n", encoding="utf-8")


def test_default_snapshot_path_is_project_runtime_and_does_not_create_it(tmp_path: Path) -> None:
    with patch.dict(os.environ, {}, clear=True):
        path = storage_snapshot_path(tmp_path)

    assert path == (tmp_path / "runtime" / "storage_health.json").absolute()
    assert not path.exists()
    assert not path.parent.exists()


def test_missing_snapshot_and_database_are_explicitly_unavailable_without_creation(tmp_path: Path) -> None:
    snapshot_path = tmp_path / "runtime" / "storage_health.json"
    database_path = tmp_path / "state.db"

    payload = cast(
        dict[str, Any],
        collect_storage_observability(
            tmp_path,
            snapshot_path=snapshot_path,
            database_path=database_path,
        ),
    )

    assert payload["available"] is False
    assert payload["status"] == "unavailable"
    assert payload["snapshot"] is None
    assert payload["database"]["available"] is False
    assert payload["database"]["status"] == "unavailable"
    assert payload["risk_increase_blocked"] is True
    assert not snapshot_path.exists()
    assert not snapshot_path.parent.exists()
    assert not database_path.exists()
    assert not Path(f"{database_path}-wal").exists()
    assert not Path(f"{database_path}-shm").exists()


@pytest.mark.parametrize(
    ("contents", "expected_status"),
    [
        (b'{"state":', "malformed"),
        (b"[]", "malformed"),
        (json.dumps({"state": "healthy"}).encode("utf-8"), "malformed"),
    ],
)
def test_malformed_storage_snapshot_is_reported_without_raising(
    tmp_path: Path,
    contents: bytes,
    expected_status: str,
) -> None:
    target = tmp_path / "storage_health.json"
    target.write_bytes(contents)

    result = read_storage_snapshot(target)

    assert result["available"] is False
    assert result["status"] == expected_status
    assert result["snapshot"] is None
    assert "malformed" in str(result["error"])


def test_oversized_storage_snapshot_is_rejected_before_json_decode(tmp_path: Path) -> None:
    target = tmp_path / "storage_health.json"
    target.write_bytes(b"{" + b"x" * 128)

    result = read_storage_snapshot(target, max_bytes=64)

    assert result["available"] is False
    assert result["status"] == "oversized"
    assert result["size_bytes"] == 129


def test_database_metrics_include_db_wal_shm_pages_freelist_and_recent_rates_without_writes(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state.db"
    observed_at = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE candidate_snapshots (
                id INTEGER PRIMARY KEY,
                snapshot_time TEXT NOT NULL,
                payload BLOB
            );
            CREATE INDEX idx_candidate_time ON candidate_snapshots(snapshot_time);
            CREATE TABLE health_samples (
                id INTEGER PRIMARY KEY,
                sample_time TEXT NOT NULL
            );
            CREATE INDEX idx_health_time ON health_samples(sample_time);
            """
        )
        connection.executemany(
            "INSERT INTO candidate_snapshots(snapshot_time, payload) VALUES (?, ?)",
            [
                ((observed_at - timedelta(minutes=10)).isoformat(), b"recent"),
                ((observed_at - timedelta(hours=2)).isoformat(), b"old"),
                ((observed_at - timedelta(minutes=5)).isoformat(), b"x" * 20_000),
            ],
        )
        connection.execute(
            "INSERT INTO health_samples(sample_time) VALUES (?)",
            ((observed_at - timedelta(minutes=15)).isoformat(),),
        )
        # Make at least one free page available without vacuuming it away.
        connection.execute("DELETE FROM candidate_snapshots WHERE length(payload) > 1000")
        connection.commit()

    Path(f"{database_path}-wal").write_bytes(b"w" * 123)
    Path(f"{database_path}-shm").write_bytes(b"s" * 45)
    before = {
        child.name: (child.stat().st_size, child.stat().st_mtime_ns)
        for child in tmp_path.iterdir()
    }

    metrics = cast(
        dict[str, Any],
        collect_database_metrics(database_path, observed_at=observed_at),
    )

    after = {
        child.name: (child.stat().st_size, child.stat().st_mtime_ns)
        for child in tmp_path.iterdir()
    }
    assert before == after
    assert metrics["available"] is True
    assert metrics["read_mode"] == "filesystem_header_and_sqlite_immutable"
    assert metrics["files"]["database"]["size_bytes"] == database_path.stat().st_size
    assert metrics["files"]["wal"]["size_bytes"] == 123
    assert metrics["files"]["shm"]["size_bytes"] == 45
    assert metrics["pages"]["page_size_bytes"] >= 512
    assert metrics["pages"]["page_count"] >= 1
    assert metrics["pages"]["freelist_pages"] >= 0
    rates = metrics["table_insert_rates"]
    assert rates["source"] == "sqlite_immutable_main_database"
    assert rates["includes_uncheckpointed_wal"] is False
    assert rates["tables"]["candidate_snapshots"]["rows_in_window"] == 1
    assert rates["tables"]["candidate_snapshots"]["estimated_inserts_per_hour"] == 1.0
    assert rates["tables"]["health_samples"]["rows_in_window"] == 1
    assert rates["tables"]["opportunity_scores"]["available"] is False


def test_storage_observability_routes_activity_probes_to_split_databases(
    tmp_path: Path,
) -> None:
    observed_at = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
    snapshot_path = tmp_path / "storage_health.json"
    state_path = tmp_path / "state.db"
    audit_path = tmp_path / "audit.db"
    research_path = tmp_path / "research.db"
    _write_snapshot(snapshot_path)

    with sqlite3.connect(state_path) as connection:
        connection.execute("CREATE TABLE risk_state (key TEXT PRIMARY KEY, value TEXT)")
    with sqlite3.connect(audit_path) as connection:
        connection.execute(
            "CREATE TABLE health_samples (id INTEGER PRIMARY KEY, sample_time TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO health_samples(sample_time) VALUES (?)",
            ((observed_at - timedelta(minutes=5)).isoformat(),),
        )
    with sqlite3.connect(research_path) as connection:
        connection.execute(
            "CREATE TABLE candidate_snapshots "
            "(id INTEGER PRIMARY KEY, snapshot_time TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO candidate_snapshots(snapshot_time) VALUES (?)",
            ((observed_at - timedelta(minutes=10)).isoformat(),),
        )

    before = {
        child.name: (child.stat().st_size, child.stat().st_mtime_ns)
        for child in tmp_path.iterdir()
    }
    payload = cast(
        dict[str, Any],
        collect_storage_observability(
            tmp_path,
            snapshot_path=snapshot_path,
            database_path=state_path,
            audit_path=audit_path,
            research_path=research_path,
            observed_at=observed_at,
        ),
    )
    after = {
        child.name: (child.stat().st_size, child.stat().st_mtime_ns)
        for child in tmp_path.iterdir()
    }

    assert before == after
    databases = cast(dict[str, Any], payload["databases"])
    assert payload["database"] is databases["state"]
    assert databases["state"]["table_insert_rates"]["tables"] == {}
    assert (
        databases["audit"]["table_insert_rates"]["tables"]["health_samples"][
            "rows_in_window"
        ]
        == 1
    )
    research_rates = databases["research"]["table_insert_rates"]["tables"]
    assert research_rates["candidate_snapshots"]["rows_in_window"] == 1
    assert "candidate_snapshots" not in databases["audit"]["table_insert_rates"]["tables"]


def test_api_and_websocket_storage_routes_are_exposed() -> None:
    paths = {getattr(route, "path", None) for route in app.routes}
    assert "/api/storage" in paths
    assert "/ws/storage" in paths
    assert "/api/admin/storage/acknowledge-recovery" in paths


def test_storage_recovery_ack_requires_atomic_recovery_proof() -> None:
    unavailable = {"available": False, "snapshot": None}
    with patch(
        "bongus.monitoring.web_dashboard._current_storage_snapshot",
        return_value=unavailable,
    ), pytest.raises(HTTPException) as unavailable_error:
        asyncio.run(api_admin_acknowledge_storage_recovery("operator"))
    assert unavailable_error.value.status_code == 503

    not_ready = _snapshot_payload(state="emergency")
    with patch(
        "bongus.monitoring.web_dashboard._current_storage_snapshot",
        return_value={"available": True, "snapshot": not_ready},
    ), pytest.raises(HTTPException) as not_ready_error:
        asyncio.run(api_admin_acknowledge_storage_recovery("operator"))
    assert not_ready_error.value.status_code == 409


def test_storage_recovery_ack_writes_operator_request_only_after_proof() -> None:
    ready = _snapshot_payload(state="healthy")
    ready.update(
        {
            # A degraded incident also latches risk and requires explicit
            # operator recovery, even when it never reached emergency.
            "emergency_latched": False,
            "risk_increase_blocked": True,
            "recovery_ready_for_operator": True,
            "healthy_recovery_samples": 3,
        }
    )
    with patch(
        "bongus.monitoring.web_dashboard._current_storage_snapshot",
        return_value={"available": True, "snapshot": ready},
    ), patch(
        "bongus.monitoring.web_dashboard.config_manager.apply_updates"
    ) as apply_updates:
        response = asyncio.run(
            api_admin_acknowledge_storage_recovery("risk-owner")
        )

    assert response["status"] == "requested"
    assert response["requested_by"] == "risk-owner"
    update = apply_updates.call_args.args[0]
    assert update["storage_recovery_request_id"] == response["request_id"]
    assert update["storage_recovery_requested_by"] == "risk-owner"


def test_storage_recovery_ack_rejects_stale_or_incomplete_atomic_proof() -> None:
    stale = _snapshot_payload(state="healthy")
    stale.update(
        {
            "observed_at": (
                datetime.now(timezone.utc) - timedelta(minutes=10)
            ).isoformat(),
            "risk_increase_blocked": True,
            "recovery_ready_for_operator": True,
            "healthy_recovery_samples": 3,
        }
    )
    with patch(
        "bongus.monitoring.web_dashboard._current_storage_snapshot",
        return_value={"available": True, "snapshot": stale},
    ), pytest.raises(HTTPException) as stale_error:
        asyncio.run(api_admin_acknowledge_storage_recovery("operator"))
    assert stale_error.value.status_code == 503

    incomplete = _snapshot_payload(state="healthy")
    incomplete.update(
        {
            "risk_increase_blocked": True,
            "recovery_ready_for_operator": True,
            "healthy_recovery_samples": 3,
            "exchange_reconciled": False,
        }
    )
    with patch(
        "bongus.monitoring.web_dashboard._current_storage_snapshot",
        return_value={"available": True, "snapshot": incomplete},
    ), pytest.raises(HTTPException) as incomplete_error:
        asyncio.run(api_admin_acknowledge_storage_recovery("operator"))
    assert incomplete_error.value.status_code == 409


def test_api_storage_uses_path_overrides_and_never_creates_a_missing_database(tmp_path: Path) -> None:
    snapshot_path = tmp_path / "operator" / "storage.json"
    database_path = tmp_path / "absent.db"
    _write_snapshot(snapshot_path, state="warning")

    with patch.dict(
        os.environ,
        {
            "BONGUS_STORAGE_HEALTH_SNAPSHOT_PATH": str(snapshot_path),
            "BONGUS_STATE_DB_PATH": str(database_path),
        },
        clear=False,
    ), patch("bongus.monitoring.web_dashboard.reader", object()):
        payload = cast(dict[str, Any], asyncio.run(api_storage()))

    assert payload["available"] is True
    assert payload["snapshot"]["state"] == "warning"
    assert payload["database"]["available"] is False
    assert not database_path.exists()


class _FakeStorageSocket:
    def __init__(self, authorization: str = "") -> None:
        self.headers = {"authorization": authorization}
        self.accepted = False
        self.closed: tuple[int, str] | None = None
        self.messages: list[dict[str, object]] = []

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, *, code: int, reason: str) -> None:
        self.closed = (code, reason)

    async def send_json(self, payload: dict[str, object]) -> None:
        self.messages.append(payload)
        raise WebSocketDisconnect()


def test_storage_websocket_rejects_unauthenticated_and_streams_to_authenticated_viewer() -> None:
    unauthorized = _FakeStorageSocket()
    asyncio.run(websocket_storage(unauthorized))  # pyright: ignore[reportArgumentType]
    assert unauthorized.accepted is False
    assert unauthorized.closed == (4401, "dashboard authentication required")

    encoded = base64.b64encode(b"observer:read-only").decode("ascii")
    authorized = _FakeStorageSocket(f"Basic {encoded}")
    expected = {"schema_version": 1, "available": False, "snapshot": None}
    with (
        patch.dict(
            os.environ,
            {
                "BONGUS_VIEWER_USERNAME": "observer",
                "BONGUS_VIEWER_PASSWORD": "read-only",
            },
            clear=True,
        ),
        patch("bongus.monitoring.web_dashboard._storage_payload", return_value=expected),
    ):
        asyncio.run(websocket_storage(authorized))  # pyright: ignore[reportArgumentType]

    assert authorized.accepted is True
    assert authorized.closed is None
    assert authorized.messages == [expected]


@pytest.mark.parametrize(
    ("snapshot_result", "expected_degraded", "expected_cap"),
    [
        (
            {"available": True, "snapshot": _snapshot_payload(state="healthy")},
            False,
            DEFAULT_SUPPORT_BUNDLE_MAX_BYTES,
        ),
        (
            {"available": True, "snapshot": _snapshot_payload(state="degraded")},
            True,
            _DEGRADED_SUPPORT_BUNDLE_MAX_BYTES,
        ),
        (
            {"available": False, "snapshot": None},
            True,
            _DEGRADED_SUPPORT_BUNDLE_MAX_BYTES,
        ),
    ],
)
def test_support_bundle_uses_64mb_cap_and_reduces_content_when_storage_is_degraded(
    snapshot_result: dict[str, object],
    expected_degraded: bool,
    expected_cap: int,
) -> None:
    def _write_small_bundle(destination, _project_root, **_kwargs):
        destination.write(b"small-bundle")
        return {}

    with (
        patch("bongus.monitoring.web_dashboard._current_storage_snapshot", return_value=snapshot_result),
        patch(
            "bongus.monitoring.web_dashboard.write_support_bundle",
            side_effect=_write_small_bundle,
        ) as write_bundle,
    ):
        response = asyncio.run(download_logs())

    assert write_bundle.call_args.kwargs["max_uncompressed_bytes"] == expected_cap
    assert write_bundle.call_args.kwargs["degraded"] is expected_degraded
    assert response.headers["content-length"] == str(len(b"small-bundle"))
    assert response.headers["x-bongus-support-bundle-mode"] == (
        "degraded" if expected_degraded else "normal"
    )
    assert response.background is not None
    asyncio.run(response.background())


def test_support_bundle_refuses_output_larger_than_hard_download_cap() -> None:
    def _write_oversized_bundle(destination, _project_root, **_kwargs):
        destination.write(b"x" * 33)
        return {}

    with (
        patch(
            "bongus.monitoring.web_dashboard._current_storage_snapshot",
            return_value={"available": True, "snapshot": _snapshot_payload(state="healthy")},
        ),
        patch("bongus.monitoring.web_dashboard.DEFAULT_SUPPORT_BUNDLE_MAX_BYTES", 32),
        patch(
            "bongus.monitoring.web_dashboard.write_support_bundle",
            side_effect=_write_oversized_bundle,
        ),
    ):
        with pytest.raises(HTTPException) as raised:
            asyncio.run(download_logs())

    assert raised.value.status_code == 507


def test_support_bundle_endpoint_streams_a_valid_bounded_zip(tmp_path: Path) -> None:
    _write_snapshot(tmp_path / "runtime" / "storage_health.json", state="healthy")
    log_path = tmp_path / "scripts" / "logs" / "live_trader.log"
    log_path.parent.mkdir(parents=True)
    log_path.write_text("operator diagnostic\n", encoding="utf-8")

    async def _download() -> tuple[StreamingResponse, bytes]:
        with patch("bongus.monitoring.web_dashboard.PROJECT_ROOT", str(tmp_path)):
            response = await download_logs()
            chunks: list[bytes] = []
            async for chunk in response.body_iterator:
                chunks.append(chunk.encode("utf-8") if isinstance(chunk, str) else bytes(chunk))
            body = b"".join(chunks)
            if response.background is not None:
                await response.background()
            return response, body

    response, body = asyncio.run(_download())

    assert int(response.headers["content-length"]) == len(body)
    assert len(body) <= DEFAULT_SUPPORT_BUNDLE_MAX_BYTES
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        assert "current/scripts/logs/live_trader.log" in archive.namelist()
        manifest = json.loads(archive.read("manifest.json"))
    assert manifest["degraded"] is False
    assert manifest["max_uncompressed_bytes"] == DEFAULT_SUPPORT_BUNDLE_MAX_BYTES
