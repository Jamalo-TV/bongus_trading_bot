"""Deterministic fixture replay ordered strictly by data availability."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Protocol, cast

from bongus.research.cross_venue.schema import CanonicalAsset, Venue
from bongus.research.cross_venue.storage import (
    RawSnapshotRecord,
    RequestMethod,
    ResearchAsset,
    ResearchStore,
    canonical_json_bytes,
)


class FixtureReplayError(RuntimeError):
    """A fixture cannot satisfy the immutable causal replay contract."""


def _reject_nonfinite(value: str) -> object:
    raise ValueError(f"non-finite fixture number is forbidden: {value}")


def _mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise FixtureReplayError(f"{field_name} must be a JSON object")
    return value


def _sequence(value: object, field_name: str) -> Sequence[object]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise FixtureReplayError(f"{field_name} must be a JSON array")
    return value


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FixtureReplayError(f"{field_name} must be a non-empty string")
    return value.strip()


def _integer(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise FixtureReplayError(f"{field_name} must be an exact integer")
    try:
        result = int(value)
    except ValueError as exc:
        raise FixtureReplayError(f"{field_name} must be an exact integer") from exc
    if not isinstance(value, int) and value.strip() != str(result):
        raise FixtureReplayError(f"{field_name} must be an integer string")
    return result


def _fixture_record(value: object, index: int) -> RawSnapshotRecord:
    row = _mapping(value, f"fixture[{index}]")
    payload = row.get("payload")
    payload_bytes = canonical_json_bytes(payload)
    expected_hash = row.get("content_sha256")
    if expected_hash is None:
        raise FixtureReplayError(f"fixture[{index}] requires content_sha256")
    expected = _text(expected_hash, "content_sha256").casefold()
    if expected != hashlib.sha256(payload_bytes).hexdigest():
        raise FixtureReplayError(f"fixture[{index}] content hash mismatch")
    raw_asset = _text(row.get("canonical_asset"), "canonical_asset")
    asset: ResearchAsset = "UNIVERSE" if raw_asset == "UNIVERSE" else CanonicalAsset(raw_asset)
    headers_row = _mapping(row.get("response_headers", {}), "response_headers")
    headers = {
        _text(key, "response header name"): _text(item, "response header value") for key, item in headers_row.items()
    }
    flags = tuple(_text(item, "quality flag") for item in _sequence(row.get("quality_flags", []), "quality_flags"))
    request_method = _text(row.get("request_method"), "request_method")
    if request_method not in ("GET", "POST", "FIXTURE"):
        raise FixtureReplayError("request_method is outside the fixture contract")
    return RawSnapshotRecord(
        event_id=_text(row.get("event_id"), "event_id"),
        dataset=_text(row.get("dataset"), "dataset"),
        venue=Venue(_text(row.get("venue"), "venue")),
        canonical_asset=asset,
        venue_symbol=_text(row.get("venue_symbol"), "venue_symbol"),
        contract_id=_text(row.get("contract_id"), "contract_id"),
        endpoint=_text(row.get("endpoint"), "endpoint"),
        request_method=cast(RequestMethod, request_method),
        source_time_ns=_integer(row.get("source_time_ns"), "source_time_ns"),
        capture_time_ns=_integer(row.get("capture_time_ns"), "capture_time_ns"),
        receive_time_ns=_integer(row.get("receive_time_ns"), "receive_time_ns"),
        available_time_ns=_integer(row.get("available_time_ns"), "available_time_ns"),
        persistence_time_ns=_integer(row.get("persistence_time_ns"), "persistence_time_ns"),
        http_status=_integer(row.get("http_status"), "http_status"),
        response_headers=headers,
        payload_bytes=payload_bytes,
        code_sha256=_text(row.get("code_sha256"), "code_sha256"),
        configuration_sha256=_text(row.get("configuration_sha256"), "configuration_sha256"),
        sequence_id=_text(row.get("sequence_id", "none"), "sequence_id"),
        connection_id=_text(row.get("connection_id", "fixture"), "connection_id"),
        quality_flags=flags,
    )


def load_raw_snapshot_fixture(path: str | Path) -> tuple[RawSnapshotRecord, ...]:
    fixture_path = Path(path).resolve()
    if not fixture_path.is_file():
        raise FixtureReplayError(f"fixture does not exist: {fixture_path}")
    try:
        root = json.loads(
            fixture_path.read_text(encoding="utf-8"),
            parse_float=Decimal,
            parse_int=int,
            parse_constant=_reject_nonfinite,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise FixtureReplayError(f"fixture is not valid exact JSON: {fixture_path}") from exc
    values: object = root.get("snapshots") if isinstance(root, Mapping) else root
    return tuple(_fixture_record(value, index) for index, value in enumerate(_sequence(values, "snapshots")))


class ReplayHandler(Protocol):
    def __call__(self, record: RawSnapshotRecord, index: CausalSnapshotIndex) -> None: ...


@dataclass(frozen=True, slots=True)
class FixtureReplayResult:
    processed_events: int
    exact_duplicates: int
    input_reordered: bool
    first_available_time_ns: int | None
    last_available_time_ns: int | None
    report_sha256: str


class CausalSnapshotIndex:
    """As-of index that never exposes a snapshot before availability time."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, Venue, str], list[RawSnapshotRecord]] = {}

    @staticmethod
    def _asset_text(asset: ResearchAsset) -> str:
        return asset.value if isinstance(asset, CanonicalAsset) else asset

    def add(self, record: RawSnapshotRecord) -> None:
        key = (record.dataset, record.venue, self._asset_text(record.canonical_asset))
        values = self._records.setdefault(key, [])
        if values and record.available_time_ns < values[-1].available_time_ns:
            raise FixtureReplayError("causal index requires availability-ordered records")
        values.append(record)

    def latest(
        self,
        *,
        dataset: str,
        venue: Venue,
        canonical_asset: ResearchAsset,
        as_of_time_ns: int,
    ) -> RawSnapshotRecord | None:
        key = (dataset, venue, self._asset_text(canonical_asset))
        candidates = self._records.get(key, ())
        for record in reversed(candidates):
            if record.available_time_ns <= as_of_time_ns:
                return record
        return None


class FixtureReplay:
    """Replay immutable fixtures and optionally persist them in research.db."""

    def run(
        self,
        records: Iterable[RawSnapshotRecord],
        *,
        handler: ReplayHandler | None = None,
        store: ResearchStore | None = None,
    ) -> FixtureReplayResult:
        original = tuple(records)
        ordered = tuple(
            sorted(
                original,
                key=lambda item: (
                    item.available_time_ns,
                    item.capture_time_ns,
                    item.event_id,
                ),
            )
        )
        input_reordered = tuple(item.event_id for item in original) != tuple(item.event_id for item in ordered)
        seen: dict[str, RawSnapshotRecord] = {}
        exact_duplicates = 0
        index = CausalSnapshotIndex()
        report_rows: list[Mapping[str, object]] = []
        for record in ordered:
            previous = seen.get(record.event_id)
            if previous is not None:
                if previous != record:
                    raise FixtureReplayError(f"conflicting duplicate fixture event: {record.event_id}")
                exact_duplicates += 1
                continue
            seen[record.event_id] = record
            index.add(record)
            if store is not None:
                store.append_raw_snapshot(record)
            if handler is not None:
                handler(record, index)
            report_rows.append(
                {
                    "event_id": record.event_id,
                    "available_time_ns": record.available_time_ns,
                    "content_sha256": record.content_sha256,
                }
            )
        report_hash = hashlib.sha256(canonical_json_bytes(report_rows)).hexdigest()
        unique = tuple(seen.values())
        return FixtureReplayResult(
            processed_events=len(unique),
            exact_duplicates=exact_duplicates,
            input_reordered=input_reordered,
            first_available_time_ns=(min(item.available_time_ns for item in unique) if unique else None),
            last_available_time_ns=(max(item.available_time_ns for item in unique) if unique else None),
            report_sha256=report_hash,
        )


__all__ = [
    "CausalSnapshotIndex",
    "FixtureReplay",
    "FixtureReplayError",
    "FixtureReplayResult",
    "ReplayHandler",
    "load_raw_snapshot_fixture",
]
