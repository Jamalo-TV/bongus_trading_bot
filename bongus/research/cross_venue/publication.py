"""Durable publication from the isolated research store to immutable artifacts.

The SQLite store is the append-only capture journal.  This module provides the
idempotent export stage used by the collector service. Rows are encoded as
exact universal artifact rows, grouped only within one immutable partition and
published as Zstd Parquet objects with hash-bound manifests. Existing manifests
form the export index when a process restarts and replays the SQLite backlog.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from bongus.research.cross_venue.artifacts import (
    ArtifactIntegrityError,
    ArtifactPartition,
    ArtifactRow,
    GapRow,
    ParquetArtifactWriter,
    PublishedArtifact,
    verify_dataset,
)
from bongus.research.cross_venue.schema import exact_wire
from bongus.research.cross_venue.storage import (
    OpportunitySnapshot,
    RawSnapshotRecord,
    ResearchStore,
    canonical_json_bytes,
)


def raw_snapshot_artifact_row(record: RawSnapshotRecord) -> ArtifactRow:
    """Preserve a captured HTTP response without lossy JSON re-encoding."""

    return ArtifactRow(
        event_id=record.event_id,
        dataset=record.dataset,
        venue=record.venue,
        canonical_asset=record.canonical_asset,
        venue_symbol=record.venue_symbol,
        contract_id=record.contract_id,
        event_type="raw_http_snapshot",
        source_time_ns=record.source_time_ns,
        capture_time_ns=record.capture_time_ns,
        receive_time_ns=record.receive_time_ns,
        available_time_ns=record.available_time_ns,
        persistence_time_ns=record.persistence_time_ns,
        sequence_id=record.sequence_id,
        connection_id=record.connection_id,
        code_sha256=record.code_sha256,
        configuration_sha256=record.configuration_sha256,
        quality_flags=record.quality_flags,
        payload={
            "endpoint": record.endpoint,
            "request_method": record.request_method,
            "http_status": record.http_status,
            "response_headers": dict(record.response_headers),
            "content_sha256": record.content_sha256,
            "payload_base64": base64.b64encode(record.payload_bytes).decode("ascii"),
        },
    )


def opportunity_snapshot_artifact_row(
    record: OpportunitySnapshot,
    *,
    source_available_time_ns: int | None = None,
) -> ArtifactRow:
    """Encode the normalized opportunity with the total-capital denominator."""

    capital = record.reserved_capital
    source_available = record.capture_time_ns if source_available_time_ns is None else source_available_time_ns
    if (
        isinstance(source_available, bool)
        or not isinstance(source_available, int)
        or source_available < 0
        or source_available > record.capture_time_ns
    ):
        raise ValueError("opportunity source availability must be causal at decision time")
    payload = exact_wire(
        {
            "source_event_ids": record.source_event_ids,
            "decision_time_ns": record.capture_time_ns,
            "source_available_time_ns": source_available,
            "long_venue": record.long_venue,
            "short_venue": record.short_venue,
            "matched_base_quantity": record.matched_base_quantity,
            "binance_long_entry_price": record.binance_long_entry_price,
            "hyperliquid_short_entry_price": record.hyperliquid_short_entry_price,
            "holding_period_days": record.holding_period_days,
            "expected_funding_pnl_usd": record.expected_funding_pnl_usd,
            "expected_executable_price_pnl_usd": record.expected_executable_price_pnl_usd,
            "expected_commissions_usd": record.expected_commissions_usd,
            "stablecoin_conversion_cost_usd": record.stablecoin_conversion_cost_usd,
            "collateral_opportunity_cost_usd": record.collateral_opportunity_cost_usd,
            "repair_failure_cost_usd": record.repair_failure_cost_usd,
            "reserved_capital": {
                "binance_collateral_usd": capital.binance_collateral_usd,
                "hyperliquid_collateral_usd": capital.hyperliquid_collateral_usd,
                "liquidation_buffers_usd": capital.liquidation_buffers_usd,
                "idle_transfer_buffer_usd": capital.idle_transfer_buffer_usd,
                "total_reserved_capital_usd": capital.total_usd,
            },
            "expected_net_pnl_usd": record.expected_net_pnl_usd,
            "expected_return_on_reserved_capital": record.expected_return_on_reserved_capital,
            "simple_annualized_return": record.simple_annualized_return,
        }
    )
    if not isinstance(payload, Mapping):
        raise TypeError("opportunity artifact payload must be an exact object")
    return ArtifactRow(
        event_id=record.event_id,
        dataset="opportunity_snapshots",
        venue=record.long_venue,
        canonical_asset=record.canonical_asset,
        venue_symbol=record.canonical_asset.value,
        contract_id=f"{record.long_venue.value}-long:{record.short_venue.value}-short",
        event_type="normalized_opportunity_snapshot",
        source_time_ns=record.capture_time_ns,
        capture_time_ns=record.capture_time_ns,
        receive_time_ns=record.receive_time_ns,
        available_time_ns=record.available_time_ns,
        persistence_time_ns=record.persistence_time_ns,
        code_sha256=record.code_sha256,
        configuration_sha256=record.configuration_sha256,
        quality_flags=record.quality_flags,
        payload=payload,
    )


def gap_artifact_row(record: RawSnapshotRecord) -> ArtifactRow | None:
    """Return a permanent explicit gap row for a rejected/failed collection."""

    failure_flags = tuple(
        flag for flag in record.quality_flags if flag in {"transport_failure", "public_response_rejected"}
    )
    if 200 <= record.http_status < 300 and not failure_flags:
        return None
    reason = "+".join(failure_flags) if failure_flags else f"http_status_{record.http_status}"
    return GapRow.deterministic(
        dataset=record.dataset,
        venue=record.venue,
        canonical_asset=record.canonical_asset,
        venue_symbol=record.venue_symbol,
        contract_id=record.contract_id,
        scheduled_time_ns=record.capture_time_ns,
        capture_time_ns=record.capture_time_ns,
        receive_time_ns=record.receive_time_ns,
        available_time_ns=record.available_time_ns,
        persistence_time_ns=record.persistence_time_ns,
        reason=reason,
        dropped_snapshots=1,
        code_sha256=record.code_sha256,
        configuration_sha256=record.configuration_sha256,
    ).as_artifact_row()


@dataclass(frozen=True, slots=True)
class PublicationResult:
    raw_rows: int
    opportunity_rows: int
    gap_rows: int
    manifest_sha256s: tuple[str, ...]

    @property
    def published_artifacts(self) -> int:
        return len(self.manifest_sha256s)

    @property
    def report_sha256(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self)).hexdigest()


class ResearchArtifactPublisher:
    """Idempotent SQLite-backlog and online publication stage."""

    def __init__(self, writer: ParquetArtifactWriter) -> None:
        self.writer = writer
        self._published_row_hashes: dict[str, str] = {}
        manifest_paths = tuple(sorted(writer.root.rglob("*.parquet.manifest.json"), key=str))
        if manifest_paths:
            report = verify_dataset(writer.root, backend=writer.backend)
            if not report.valid or report.exact_duplicate_event_ids:
                raise ArtifactIntegrityError(
                    "existing artifact dataset must be valid and duplicate-free before publication"
                )
            for manifest_path in manifest_paths:
                data_path = manifest_path.with_suffix("").with_suffix("")
                inspection = writer.backend.inspect(data_path)
                for event_id, row_hash, _available, _row_json in inspection.event_rows:
                    self._published_row_hashes[event_id] = row_hash

    @staticmethod
    def _row_sha256(row: ArtifactRow) -> str:
        return hashlib.sha256(canonical_json_bytes(row.row_payload)).hexdigest()

    def _publish_rows(self, rows: Iterable[ArtifactRow]) -> tuple[PublishedArtifact, ...]:
        grouped: dict[
            tuple[str, str, str, int, str, str, str],
            list[ArtifactRow],
        ] = {}
        pending_row_hashes: dict[str, str] = {}
        for row in rows:
            row_hash = self._row_sha256(row)
            existing = self._published_row_hashes.get(row.event_id)
            if existing is not None:
                if existing != row_hash:
                    raise ArtifactIntegrityError(
                        f"published event ID conflicts with current journal row: {row.event_id}"
                    )
                continue
            pending = pending_row_hashes.get(row.event_id)
            if pending is not None:
                if pending != row_hash:
                    raise ArtifactIntegrityError(f"publication batch contains conflicting event ID: {row.event_id}")
                continue
            pending_row_hashes[row.event_id] = row_hash
            partition = ArtifactPartition.for_row(row)
            key = (
                partition.dataset,
                partition.venue.value,
                partition.utc_date,
                partition.utc_hour,
                partition.venue_symbol,
                row.code_sha256,
                row.configuration_sha256,
            )
            grouped.setdefault(key, []).append(row)
        published: list[PublishedArtifact] = []
        for key in sorted(grouped):
            batch = tuple(
                sorted(
                    grouped[key],
                    key=lambda row: (row.available_time_ns, row.event_id),
                )
            )
            item = self.writer.write(batch)
            published.append(item)
            for row in batch:
                self._published_row_hashes[row.event_id] = self._row_sha256(row)
        return tuple(published)

    def publish_raw_snapshot(self, record: RawSnapshotRecord) -> tuple[PublishedArtifact, ...]:
        rows = [raw_snapshot_artifact_row(record)]
        gap = gap_artifact_row(record)
        if gap is not None:
            rows.append(gap)
        return self._publish_rows(rows)

    def publish_opportunity_snapshot(
        self,
        record: OpportunitySnapshot,
        *,
        source_available_time_ns: int | None = None,
    ) -> PublishedArtifact:
        published = self._publish_rows(
            (
                opportunity_snapshot_artifact_row(
                    record,
                    source_available_time_ns=source_available_time_ns,
                ),
            )
        )
        if len(published) != 1:
            raise ArtifactIntegrityError(
                "opportunity snapshot was already published; use publish_records for idempotent replay"
            )
        return published[0]

    def publish_records(
        self,
        *,
        raw_records: Iterable[RawSnapshotRecord] = (),
        opportunity_records: Iterable[OpportunitySnapshot] = (),
    ) -> PublicationResult:
        raw_records = tuple(raw_records)
        source_availability = {record.event_id: record.available_time_ns for record in raw_records}
        artifact_rows: list[ArtifactRow] = []
        for record in raw_records:
            artifact_rows.append(raw_snapshot_artifact_row(record))
            gap = gap_artifact_row(record)
            if gap is not None:
                artifact_rows.append(gap)
        gap_count = len(artifact_rows) - len(raw_records)
        opportunity_records = tuple(opportunity_records)
        for record in opportunity_records:
            missing = tuple(event_id for event_id in record.source_event_ids if event_id not in source_availability)
            if missing:
                raise ValueError(
                    "opportunity artifact sources are absent from the publication batch: " + ",".join(missing)
                )
            artifact_rows.append(
                opportunity_snapshot_artifact_row(
                    record,
                    source_available_time_ns=max(source_availability[event_id] for event_id in record.source_event_ids),
                )
            )
        published = self._publish_rows(artifact_rows)
        return PublicationResult(
            len(raw_records),
            len(opportunity_records),
            gap_count,
            tuple(sorted(item.manifest.manifest_sha256 for item in published)),
        )

    def publish_store(self, store: ResearchStore) -> PublicationResult:
        """Replay the complete append-only journal; exact reruns are harmless."""

        return self.publish_records(
            raw_records=store.iter_raw_snapshots(),
            opportunity_records=store.iter_opportunity_snapshots(),
        )


__all__ = [
    "PublicationResult",
    "ResearchArtifactPublisher",
    "gap_artifact_row",
    "opportunity_snapshot_artifact_row",
    "raw_snapshot_artifact_row",
]
