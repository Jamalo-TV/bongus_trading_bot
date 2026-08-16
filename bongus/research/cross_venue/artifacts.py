"""Immutable partitioned Parquet evidence and retention verification.

The module remains importable with only the Python standard library.  Writing
or deeply verifying Parquet fails closed unless a real PyArrow backend with
Zstandard support is available; no alternate format is ever given a
``.parquet`` suffix.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal, Protocol, cast

from bongus.research.cross_venue.schema import (
    PUBLIC_ENVIRONMENT,
    SCHEMA_VERSION,
    CanonicalAsset,
    Venue,
    deterministic_event_id,
    epoch_nanoseconds,
    exact_wire,
)
from bongus.research.cross_venue.storage import canonical_json_bytes

try:
    import pyarrow as _pyarrow_module
    import pyarrow.parquet as _parquet_module
except ModuleNotFoundError:
    _PYARROW: Any = None
    _PARQUET: Any = None
else:
    _PYARROW = _pyarrow_module
    _PARQUET = _parquet_module


ARTIFACT_MANIFEST_VERSION: Final[int] = 1
NANOSECONDS_PER_DAY: Final[int] = 86_400_000_000_000
RAW_BOOK_RETENTION_DAYS: Final[int] = 14
COMPACT_RETENTION_DAYS: Final[int] = 180

RetentionClass = Literal["raw_14d_min", "compact_180d_min", "permanent"]

_SAFE_DATASET = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SAFE_SYMBOL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RAW_BOOK_DATASETS: Final[frozenset[str]] = frozenset({"bbo", "top20_book"})
_PERMANENT_DATASETS: Final[frozenset[str]] = frozenset(
    {
        "collection_gaps",
        "contract_metadata",
        "decision_snapshots",
        "episode_outcomes",
        "fee_profiles",
        "final_funding_settlements",
        "funding_intervals",
        "funding_reconciliation",
        "manifests",
    }
)


class ArtifactError(RuntimeError):
    """Base failure for immutable research artifacts."""


class ParquetBackendUnavailable(ArtifactError):
    """A real Zstd Parquet backend is required for this operation."""


class ArtifactIntegrityError(ArtifactError):
    """An artifact or manifest violates its immutable contract."""


def _required_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _sha256_text(value: str, field_name: str) -> str:
    normalized = _required_text(value, field_name).casefold()
    if _SHA256.fullmatch(normalized) is None:
        raise ValueError(f"{field_name} must be a SHA-256 hex digest")
    return normalized


def _safe_dataset(value: str) -> str:
    normalized = _required_text(value, "dataset")
    if _SAFE_DATASET.fullmatch(normalized) is None:
        raise ValueError("dataset must be a lowercase partition-safe identifier")
    return normalized


def _safe_symbol(value: str) -> str:
    normalized = _required_text(value, "venue_symbol")
    if _SAFE_SYMBOL.fullmatch(normalized) is None:
        raise ValueError("venue_symbol must be a partition-safe identifier")
    return normalized


def _canonical_asset(value: CanonicalAsset | Literal["UNIVERSE"]) -> CanonicalAsset | Literal["UNIVERSE"]:
    if isinstance(value, CanonicalAsset) or value == "UNIVERSE":
        return value
    raise TypeError("canonical_asset must use the fixed universe or UNIVERSE")


def _timestamp_chain(
    capture_time_ns: int | str,
    receive_time_ns: int | str,
    available_time_ns: int | str,
    persistence_time_ns: int | str,
) -> tuple[int, int, int, int]:
    capture = epoch_nanoseconds(capture_time_ns, "capture_time_ns")
    receive = epoch_nanoseconds(receive_time_ns, "receive_time_ns")
    available = epoch_nanoseconds(available_time_ns, "available_time_ns")
    persistence = epoch_nanoseconds(persistence_time_ns, "persistence_time_ns")
    if not capture <= receive <= available <= persistence:
        raise ValueError("timestamps must satisfy capture <= receive <= availability <= persistence")
    return capture, receive, available, persistence


def retention_class_for_dataset(dataset: str) -> RetentionClass:
    normalized = _safe_dataset(dataset)
    if normalized in _RAW_BOOK_DATASETS:
        return "raw_14d_min"
    if normalized in _PERMANENT_DATASETS:
        return "permanent"
    return "compact_180d_min"


def required_retain_until_ns(retention_class: RetentionClass, maximum_available_time_ns: int) -> int | None:
    maximum = epoch_nanoseconds(maximum_available_time_ns, "maximum_available_time_ns")
    if retention_class == "permanent":
        return None
    days = RAW_BOOK_RETENTION_DAYS if retention_class == "raw_14d_min" else COMPACT_RETENTION_DAYS
    return maximum + days * NANOSECONDS_PER_DAY


@dataclass(frozen=True, slots=True)
class ArtifactRow:
    """Universal exact event envelope stored inside an immutable partition."""

    event_id: str
    dataset: str
    venue: Venue
    canonical_asset: CanonicalAsset | Literal["UNIVERSE"]
    venue_symbol: str
    contract_id: str
    event_type: str
    source_time_ns: int
    capture_time_ns: int
    receive_time_ns: int
    available_time_ns: int
    persistence_time_ns: int
    code_sha256: str
    configuration_sha256: str
    payload: Mapping[str, object]
    sequence_id: str = "none"
    connection_id: str = "none"
    quality_flags: tuple[str, ...] = ()
    schema_version: str = SCHEMA_VERSION
    environment: str = PUBLIC_ENVIRONMENT

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION or self.environment != PUBLIC_ENVIRONMENT:
            raise ValueError("artifact rows require the fixed public v1 envelope")
        object.__setattr__(self, "event_id", _required_text(self.event_id, "event_id"))
        object.__setattr__(self, "dataset", _safe_dataset(self.dataset))
        if not isinstance(self.venue, Venue):
            raise TypeError("artifact venue must use the fixed Venue enum")
        object.__setattr__(self, "canonical_asset", _canonical_asset(self.canonical_asset))
        object.__setattr__(self, "venue_symbol", _safe_symbol(self.venue_symbol))
        for name in ("contract_id", "event_type", "sequence_id", "connection_id"):
            object.__setattr__(self, name, _required_text(getattr(self, name), name))
        source = epoch_nanoseconds(self.source_time_ns, "source_time_ns")
        capture, receive, available, persistence = _timestamp_chain(
            self.capture_time_ns,
            self.receive_time_ns,
            self.available_time_ns,
            self.persistence_time_ns,
        )
        object.__setattr__(self, "source_time_ns", source)
        object.__setattr__(self, "capture_time_ns", capture)
        object.__setattr__(self, "receive_time_ns", receive)
        object.__setattr__(self, "available_time_ns", available)
        object.__setattr__(self, "persistence_time_ns", persistence)
        object.__setattr__(self, "code_sha256", _sha256_text(self.code_sha256, "code_sha256"))
        object.__setattr__(
            self,
            "configuration_sha256",
            _sha256_text(self.configuration_sha256, "configuration_sha256"),
        )
        if not isinstance(self.payload, Mapping):
            raise TypeError("artifact payload must be a mapping")
        wire_payload = exact_wire(self.payload)
        if not isinstance(wire_payload, Mapping):
            raise TypeError("artifact payload must encode as an exact object")
        object.__setattr__(self, "payload", dict(wire_payload))
        flags = tuple(sorted({_required_text(flag, "quality flag") for flag in self.quality_flags}))
        object.__setattr__(self, "quality_flags", flags)

    @property
    def row_payload(self) -> Mapping[str, object]:
        return cast(
            Mapping[str, object],
            exact_wire(
                {
                    "event_id": self.event_id,
                    "schema_version": self.schema_version,
                    "environment": self.environment,
                    "dataset": self.dataset,
                    "venue": self.venue,
                    "canonical_asset": self.canonical_asset,
                    "venue_symbol": self.venue_symbol,
                    "contract_id": self.contract_id,
                    "event_type": self.event_type,
                    "source_time_ns": self.source_time_ns,
                    "capture_time_ns": self.capture_time_ns,
                    "receive_time_ns": self.receive_time_ns,
                    "available_time_ns": self.available_time_ns,
                    "persistence_time_ns": self.persistence_time_ns,
                    "sequence_id": self.sequence_id,
                    "connection_id": self.connection_id,
                    "code_sha256": self.code_sha256,
                    "configuration_sha256": self.configuration_sha256,
                    "quality_flags": self.quality_flags,
                    "payload": self.payload,
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class GapRow:
    event_id: str
    dataset: str
    venue: Venue
    canonical_asset: CanonicalAsset | Literal["UNIVERSE"]
    venue_symbol: str
    contract_id: str
    scheduled_time_ns: int
    capture_time_ns: int
    receive_time_ns: int
    available_time_ns: int
    persistence_time_ns: int
    reason: str
    dropped_snapshots: int
    code_sha256: str
    configuration_sha256: str

    def __post_init__(self) -> None:
        if isinstance(self.dropped_snapshots, bool) or not isinstance(self.dropped_snapshots, int):
            raise TypeError("dropped_snapshots must be an exact integer")
        if self.dropped_snapshots <= 0:
            raise ValueError("dropped_snapshots must be positive")
        object.__setattr__(self, "reason", _required_text(self.reason, "reason"))
        epoch_nanoseconds(self.scheduled_time_ns, "scheduled_time_ns")

    def as_artifact_row(self) -> ArtifactRow:
        return ArtifactRow(
            event_id=self.event_id,
            dataset="collection_gaps",
            venue=self.venue,
            canonical_asset=self.canonical_asset,
            venue_symbol=self.venue_symbol,
            contract_id=self.contract_id,
            event_type="collection_gap",
            source_time_ns=self.scheduled_time_ns,
            capture_time_ns=self.capture_time_ns,
            receive_time_ns=self.receive_time_ns,
            available_time_ns=self.available_time_ns,
            persistence_time_ns=self.persistence_time_ns,
            code_sha256=self.code_sha256,
            configuration_sha256=self.configuration_sha256,
            quality_flags=("gap", self.reason),
            payload={
                "affected_dataset": _safe_dataset(self.dataset),
                "scheduled_time_ns": self.scheduled_time_ns,
                "dropped_snapshots": self.dropped_snapshots,
                "reason": self.reason,
            },
        )

    @classmethod
    def deterministic(
        cls,
        *,
        dataset: str,
        venue: Venue,
        canonical_asset: CanonicalAsset | Literal["UNIVERSE"],
        venue_symbol: str,
        contract_id: str,
        scheduled_time_ns: int,
        capture_time_ns: int,
        receive_time_ns: int,
        available_time_ns: int,
        persistence_time_ns: int,
        reason: str,
        dropped_snapshots: int,
        code_sha256: str,
        configuration_sha256: str,
    ) -> GapRow:
        event_id = deterministic_event_id(
            "collection-gap",
            _safe_dataset(dataset),
            venue.value,
            canonical_asset.value if isinstance(canonical_asset, CanonicalAsset) else canonical_asset,
            _safe_symbol(venue_symbol),
            str(epoch_nanoseconds(scheduled_time_ns, "scheduled_time_ns")),
            _required_text(reason, "reason"),
        )
        return cls(
            event_id=event_id,
            dataset=dataset,
            venue=venue,
            canonical_asset=canonical_asset,
            venue_symbol=venue_symbol,
            contract_id=contract_id,
            scheduled_time_ns=scheduled_time_ns,
            capture_time_ns=capture_time_ns,
            receive_time_ns=receive_time_ns,
            available_time_ns=available_time_ns,
            persistence_time_ns=persistence_time_ns,
            reason=reason,
            dropped_snapshots=dropped_snapshots,
            code_sha256=code_sha256,
            configuration_sha256=configuration_sha256,
        )


@dataclass(frozen=True, slots=True)
class ArtifactPartition:
    dataset: str
    venue: Venue
    utc_date: str
    utc_hour: int
    venue_symbol: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "dataset", _safe_dataset(self.dataset))
        if not isinstance(self.venue, Venue):
            raise TypeError("partition venue must use the fixed Venue enum")
        try:
            parsed_date = datetime.strptime(self.utc_date, "%Y-%m-%d").date()
        except (TypeError, ValueError) as exc:
            raise ValueError("utc_date must be YYYY-MM-DD") from exc
        if parsed_date.isoformat() != self.utc_date:
            raise ValueError("utc_date must be canonical YYYY-MM-DD")
        if isinstance(self.utc_hour, bool) or not isinstance(self.utc_hour, int) or not 0 <= self.utc_hour <= 23:
            raise ValueError("utc_hour must be in [0, 23]")
        object.__setattr__(self, "venue_symbol", _safe_symbol(self.venue_symbol))

    @classmethod
    def for_row(cls, row: ArtifactRow) -> ArtifactPartition:
        instant = datetime.fromtimestamp(row.available_time_ns // 1_000_000_000, tz=UTC)
        return cls(row.dataset, row.venue, instant.date().isoformat(), instant.hour, row.venue_symbol)

    @property
    def relative_directory(self) -> Path:
        return Path(
            f"dataset={self.dataset}",
            f"venue={self.venue.value}",
            f"date={self.utc_date}",
            f"hour={self.utc_hour:02d}",
            f"symbol={self.venue_symbol}",
        )


@dataclass(frozen=True, slots=True)
class ParquetInspection:
    row_count: int
    compressions: tuple[str, ...]
    event_rows: tuple[tuple[str, str, int, str], ...]


class ParquetBackend(Protocol):
    name: str

    def write(self, path: Path, rows: Sequence[Mapping[str, object]]) -> None: ...

    def inspect(self, path: Path) -> ParquetInspection: ...


class PyArrowZstdBackend:
    name = "pyarrow-zstd"

    def __init__(self) -> None:
        if _PYARROW is None or _PARQUET is None:
            raise ParquetBackendUnavailable("PyArrow with Parquet/Zstd support is unavailable")

    def write(self, path: Path, rows: Sequence[Mapping[str, object]]) -> None:
        table = _PYARROW.Table.from_pylist(list(rows))
        try:
            _PARQUET.write_table(
                table,
                path,
                compression="zstd",
                use_dictionary=False,
                write_statistics=True,
            )
        except Exception as exc:
            raise ParquetBackendUnavailable("PyArrow cannot write Zstd Parquet") from exc

    def inspect(self, path: Path) -> ParquetInspection:
        try:
            parquet_file = _PARQUET.ParquetFile(path)
            metadata = parquet_file.metadata
            compressions = tuple(
                str(metadata.row_group(group).column(column).compression).upper()
                for group in range(metadata.num_row_groups)
                for column in range(metadata.row_group(group).num_columns)
            )
            table = parquet_file.read(columns=["event_id", "row_sha256", "available_time_ns", "row_json"])
            values = table.to_pylist()
        except Exception as exc:
            raise ArtifactIntegrityError(f"invalid Parquet artifact: {path}") from exc
        event_rows = tuple(
            (
                str(value["event_id"]),
                str(value["row_sha256"]),
                int(value["available_time_ns"]),
                str(value["row_json"]),
            )
            for value in values
        )
        return ParquetInspection(int(metadata.num_rows), compressions, event_rows)


def parquet_backend_available() -> bool:
    return _PYARROW is not None and _PARQUET is not None


@dataclass(frozen=True, slots=True)
class ArtifactManifest:
    dataset: str
    venue: Venue
    utc_date: str
    utc_hour: int
    venue_symbol: str
    relative_data_path: str
    row_count: int
    byte_count: int
    minimum_available_time_ns: int
    maximum_available_time_ns: int
    file_sha256: str
    rows_sha256: str
    code_sha256: str
    configuration_sha256: str
    retention_class: RetentionClass
    retain_until_ns: int | None
    published_time_ns: int
    manifest_sha256: str
    manifest_version: int = ARTIFACT_MANIFEST_VERSION
    format: Literal["parquet"] = "parquet"
    compression: Literal["zstd"] = "zstd"

    @property
    def partition(self) -> ArtifactPartition:
        return ArtifactPartition(self.dataset, self.venue, self.utc_date, self.utc_hour, self.venue_symbol)

    def unsigned_payload(self) -> Mapping[str, object]:
        return {
            "manifest_version": self.manifest_version,
            "format": self.format,
            "compression": self.compression,
            "dataset": self.dataset,
            "venue": self.venue,
            "utc_date": self.utc_date,
            "utc_hour": self.utc_hour,
            "venue_symbol": self.venue_symbol,
            "relative_data_path": self.relative_data_path,
            "row_count": self.row_count,
            "byte_count": self.byte_count,
            "minimum_available_time_ns": self.minimum_available_time_ns,
            "maximum_available_time_ns": self.maximum_available_time_ns,
            "file_sha256": self.file_sha256,
            "rows_sha256": self.rows_sha256,
            "code_sha256": self.code_sha256,
            "configuration_sha256": self.configuration_sha256,
            "retention_class": self.retention_class,
            "retain_until_ns": self.retain_until_ns,
            "published_time_ns": self.published_time_ns,
        }

    def as_wire(self) -> Mapping[str, object]:
        return {
            **cast(Mapping[str, object], exact_wire(self.unsigned_payload())),
            "manifest_sha256": self.manifest_sha256,
        }


def _manifest_from_payload(payload: Mapping[str, object]) -> ArtifactManifest:
    try:
        retention = str(payload["retention_class"])
        if retention not in ("raw_14d_min", "compact_180d_min", "permanent"):
            raise ValueError("unknown retention class")
        retain_value = payload.get("retain_until_ns")
        manifest = ArtifactManifest(
            manifest_version=int(str(payload["manifest_version"])),
            format=cast(Literal["parquet"], str(payload["format"])),
            compression=cast(Literal["zstd"], str(payload["compression"])),
            dataset=_safe_dataset(str(payload["dataset"])),
            venue=Venue(str(payload["venue"])),
            utc_date=str(payload["utc_date"]),
            utc_hour=int(str(payload["utc_hour"])),
            venue_symbol=_safe_symbol(str(payload["venue_symbol"])),
            relative_data_path=str(payload["relative_data_path"]),
            row_count=int(str(payload["row_count"])),
            byte_count=int(str(payload["byte_count"])),
            minimum_available_time_ns=int(str(payload["minimum_available_time_ns"])),
            maximum_available_time_ns=int(str(payload["maximum_available_time_ns"])),
            file_sha256=_sha256_text(str(payload["file_sha256"]), "file_sha256"),
            rows_sha256=_sha256_text(str(payload["rows_sha256"]), "rows_sha256"),
            code_sha256=_sha256_text(str(payload["code_sha256"]), "code_sha256"),
            configuration_sha256=_sha256_text(
                str(payload["configuration_sha256"]),
                "configuration_sha256",
            ),
            retention_class=cast(RetentionClass, retention),
            retain_until_ns=None if retain_value is None else int(str(retain_value)),
            published_time_ns=int(str(payload["published_time_ns"])),
            manifest_sha256=_sha256_text(str(payload["manifest_sha256"]), "manifest_sha256"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ArtifactIntegrityError("artifact manifest has an invalid schema") from exc
    if manifest.manifest_version != ARTIFACT_MANIFEST_VERSION:
        raise ArtifactIntegrityError("unsupported artifact manifest version")
    if manifest.format != "parquet" or manifest.compression != "zstd":
        raise ArtifactIntegrityError("artifact must be Zstd Parquet")
    if manifest.row_count <= 0 or manifest.byte_count <= 0:
        raise ArtifactIntegrityError("artifact manifest counts must be positive")
    if manifest.minimum_available_time_ns > manifest.maximum_available_time_ns:
        raise ArtifactIntegrityError("artifact manifest time range is reversed")
    expected_retention = retention_class_for_dataset(manifest.dataset)
    expected_until = required_retain_until_ns(expected_retention, manifest.maximum_available_time_ns)
    if manifest.retention_class != expected_retention or manifest.retain_until_ns != expected_until:
        raise ArtifactIntegrityError("artifact retention metadata violates policy")
    expected_hash = hashlib.sha256(canonical_json_bytes(manifest.unsigned_payload())).hexdigest()
    if manifest.manifest_sha256 != expected_hash:
        raise ArtifactIntegrityError("artifact manifest hash mismatch")
    expected_prefix = manifest.partition.relative_directory.as_posix() + "/"
    normalized_relative = Path(manifest.relative_data_path).as_posix()
    if not normalized_relative.startswith(expected_prefix) or Path(normalized_relative).name.endswith(".tmp"):
        raise ArtifactIntegrityError("artifact data path does not match its partition")
    if Path(normalized_relative).is_absolute() or ".." in Path(normalized_relative).parts:
        raise ArtifactIntegrityError("artifact data path must remain relative and contained")
    return manifest


def load_artifact_manifest(path: str | Path) -> ArtifactManifest:
    manifest_path = Path(path)
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ArtifactIntegrityError(f"manifest is missing or linked: {manifest_path}")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArtifactIntegrityError(f"manifest is not valid JSON: {manifest_path}") from exc
    if not isinstance(payload, Mapping):
        raise ArtifactIntegrityError("artifact manifest must be a JSON object")
    return _manifest_from_payload(payload)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | int(getattr(os, "O_DIRECTORY", 0))
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
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
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
        _fsync_directory(path.parent)
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def _ensure_unlinked_tree(root: Path, directory: Path) -> None:
    if root.exists() and root.is_symlink():
        raise ArtifactIntegrityError("artifact root cannot be a symbolic link")
    relative = directory.relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        if current.exists() and current.is_symlink():
            raise ArtifactIntegrityError(f"artifact partition traverses a symbolic link: {current}")


def _backend_rows(rows: Sequence[ArtifactRow]) -> tuple[Mapping[str, object], ...]:
    encoded: list[Mapping[str, object]] = []
    for row in rows:
        row_json = canonical_json_bytes(row.row_payload).decode("utf-8")
        encoded.append(
            {
                "event_id": row.event_id,
                "row_sha256": hashlib.sha256(row_json.encode("utf-8")).hexdigest(),
                "schema_version": row.schema_version,
                "environment": row.environment,
                "dataset": row.dataset,
                "venue": row.venue.value,
                "canonical_asset": (
                    row.canonical_asset.value
                    if isinstance(row.canonical_asset, CanonicalAsset)
                    else row.canonical_asset
                ),
                "venue_symbol": row.venue_symbol,
                "contract_id": row.contract_id,
                "event_type": row.event_type,
                "source_time_ns": row.source_time_ns,
                "capture_time_ns": row.capture_time_ns,
                "receive_time_ns": row.receive_time_ns,
                "available_time_ns": row.available_time_ns,
                "persistence_time_ns": row.persistence_time_ns,
                "sequence_id": row.sequence_id,
                "connection_id": row.connection_id,
                "code_sha256": row.code_sha256,
                "configuration_sha256": row.configuration_sha256,
                "quality_flags_json": canonical_json_bytes(row.quality_flags).decode("utf-8"),
                "row_json": row_json,
            }
        )
    return tuple(encoded)


@dataclass(frozen=True, slots=True)
class PublishedArtifact:
    data_path: Path
    manifest_path: Path
    manifest: ArtifactManifest


class ParquetArtifactWriter:
    """Publish one immutable, partition-pure Zstd Parquet artifact."""

    def __init__(self, root: str | Path, *, backend: ParquetBackend | None = None) -> None:
        raw_root = Path(root).expanduser().absolute()
        if raw_root.exists() and raw_root.is_symlink():
            raise ArtifactIntegrityError("artifact root cannot be a symbolic link")
        raw_root.mkdir(parents=True, exist_ok=True)
        self.root = raw_root.resolve()
        self.backend = backend or PyArrowZstdBackend()

    def write(self, rows: Sequence[ArtifactRow]) -> PublishedArtifact:
        if not rows:
            raise ValueError("a Parquet artifact requires at least one row")
        ordered = tuple(sorted(rows, key=lambda row: (row.available_time_ns, row.event_id)))
        event_ids = tuple(row.event_id for row in ordered)
        if len(event_ids) != len(set(event_ids)):
            raise ArtifactIntegrityError("a Parquet artifact cannot contain duplicate event IDs")
        partition = ArtifactPartition.for_row(ordered[0])
        if any(ArtifactPartition.for_row(row) != partition for row in ordered[1:]):
            raise ArtifactIntegrityError("all rows must belong to one exact partition")
        code_hashes = {row.code_sha256 for row in ordered}
        configuration_hashes = {row.configuration_sha256 for row in ordered}
        if len(code_hashes) != 1 or len(configuration_hashes) != 1:
            raise ArtifactIntegrityError("one artifact cannot mix code or configuration hashes")
        directory = self.root / partition.relative_directory
        _ensure_unlinked_tree(self.root, directory)
        directory.mkdir(parents=True, exist_ok=True)
        backend_rows = _backend_rows(ordered)
        rows_hash = hashlib.sha256(canonical_json_bytes(backend_rows)).hexdigest()
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=directory,
                prefix=".part-",
                suffix=".parquet.tmp",
                delete=False,
            ) as handle:
                temporary_name = handle.name
            temporary_path = Path(temporary_name)
            self.backend.write(temporary_path, backend_rows)
            with temporary_path.open("rb+") as handle:
                os.fsync(handle.fileno())
            file_hash = _hash_file(temporary_path)
            final_name = f"part-{file_hash}.parquet"
            data_path = directory / final_name
            if data_path.exists():
                if data_path.is_symlink() or _hash_file(data_path) != file_hash:
                    raise ArtifactIntegrityError("conflicting immutable Parquet artifact")
                temporary_path.unlink()
            else:
                os.replace(temporary_path, data_path)
            temporary_name = None
            _fsync_directory(directory)
        finally:
            if temporary_name is not None:
                Path(temporary_name).unlink(missing_ok=True)

        relative_data = data_path.relative_to(self.root).as_posix()
        available_times = tuple(row.available_time_ns for row in ordered)
        retention_class = retention_class_for_dataset(partition.dataset)
        unsigned = {
            "manifest_version": ARTIFACT_MANIFEST_VERSION,
            "format": "parquet",
            "compression": "zstd",
            "dataset": partition.dataset,
            "venue": partition.venue,
            "utc_date": partition.utc_date,
            "utc_hour": partition.utc_hour,
            "venue_symbol": partition.venue_symbol,
            "relative_data_path": relative_data,
            "row_count": len(ordered),
            "byte_count": data_path.stat().st_size,
            "minimum_available_time_ns": min(available_times),
            "maximum_available_time_ns": max(available_times),
            "file_sha256": file_hash,
            "rows_sha256": rows_hash,
            "code_sha256": next(iter(code_hashes)),
            "configuration_sha256": next(iter(configuration_hashes)),
            "retention_class": retention_class,
            "retain_until_ns": required_retain_until_ns(retention_class, max(available_times)),
            "published_time_ns": max(row.persistence_time_ns for row in ordered),
        }
        manifest_hash = hashlib.sha256(canonical_json_bytes(unsigned)).hexdigest()
        manifest = _manifest_from_payload(
            {
                **cast(Mapping[str, object], exact_wire(unsigned)),
                "manifest_sha256": manifest_hash,
            }
        )
        manifest_path = data_path.with_suffix(".parquet.manifest.json")
        encoded_manifest = canonical_json_bytes(manifest.as_wire()) + b"\n"
        if manifest_path.exists():
            if manifest_path.is_symlink() or manifest_path.read_bytes() != encoded_manifest:
                raise ArtifactIntegrityError("conflicting immutable artifact manifest")
        else:
            _atomic_write_bytes(manifest_path, encoded_manifest)
        return PublishedArtifact(data_path, manifest_path, manifest)


@dataclass(frozen=True, slots=True)
class VerifiedArtifact:
    manifest_path: Path
    data_path: Path
    manifest: ArtifactManifest


def verify_artifact(
    root: str | Path,
    manifest_path: str | Path,
    *,
    backend: ParquetBackend | None = None,
) -> VerifiedArtifact:
    raw_root = Path(root).expanduser().absolute()
    if raw_root.is_symlink():
        raise ArtifactIntegrityError("dataset root cannot be a symbolic link")
    resolved_root = raw_root.resolve()
    raw_manifest = Path(manifest_path).absolute()
    if raw_manifest.is_symlink():
        raise ArtifactIntegrityError("artifact manifest cannot be a symbolic link")
    resolved_manifest = raw_manifest.resolve()
    if not resolved_manifest.is_relative_to(resolved_root):
        raise ArtifactIntegrityError("artifact manifest escapes the dataset root")
    _ensure_unlinked_tree(resolved_root, resolved_manifest.parent)
    manifest = load_artifact_manifest(resolved_manifest)
    raw_data_path = resolved_root / manifest.relative_data_path
    if raw_data_path.is_symlink():
        raise ArtifactIntegrityError("artifact payload cannot be a symbolic link")
    _ensure_unlinked_tree(resolved_root, raw_data_path.parent)
    data_path = raw_data_path.resolve()
    if not data_path.is_relative_to(resolved_root) or not data_path.is_file():
        raise ArtifactIntegrityError("artifact payload is missing, linked, or outside the dataset root")
    if data_path.stat().st_size != manifest.byte_count or _hash_file(data_path) != manifest.file_sha256:
        raise ArtifactIntegrityError("artifact byte count or SHA-256 mismatch")
    parquet_backend = backend or PyArrowZstdBackend()
    inspection = parquet_backend.inspect(data_path)
    if inspection.row_count != manifest.row_count:
        raise ArtifactIntegrityError("Parquet row count does not match manifest")
    if not inspection.compressions or any(value != "ZSTD" for value in inspection.compressions):
        raise ArtifactIntegrityError("Parquet artifact is not entirely Zstd-compressed")
    if inspection.event_rows:
        minimum = min(value[2] for value in inspection.event_rows)
        maximum = max(value[2] for value in inspection.event_rows)
        if (minimum, maximum) != (
            manifest.minimum_available_time_ns,
            manifest.maximum_available_time_ns,
        ):
            raise ArtifactIntegrityError("Parquet time range does not match manifest")
        rows_for_hash: list[Mapping[str, object]] = []
        for event_id, row_sha256, available_time_ns, row_json in inspection.event_rows:
            if hashlib.sha256(row_json.encode("utf-8")).hexdigest() != row_sha256:
                raise ArtifactIntegrityError(f"row hash mismatch: {event_id}")
            try:
                row_payload = json.loads(row_json)
            except json.JSONDecodeError as exc:
                raise ArtifactIntegrityError(f"row JSON is invalid: {event_id}") from exc
            if not isinstance(row_payload, Mapping):
                raise ArtifactIntegrityError(f"row JSON is not an object: {event_id}")
            if (
                row_payload.get("event_id") != event_id
                or row_payload.get("schema_version") != SCHEMA_VERSION
                or row_payload.get("environment") != PUBLIC_ENVIRONMENT
                or row_payload.get("dataset") != manifest.dataset
                or row_payload.get("venue") != manifest.venue.value
                or row_payload.get("venue_symbol") != manifest.venue_symbol
                or row_payload.get("available_time_ns") != available_time_ns
                or row_payload.get("code_sha256") != manifest.code_sha256
                or row_payload.get("configuration_sha256") != manifest.configuration_sha256
            ):
                raise ArtifactIntegrityError(f"row envelope does not match manifest: {event_id}")
            instant = datetime.fromtimestamp(available_time_ns // 1_000_000_000, tz=UTC)
            if instant.date().isoformat() != manifest.utc_date or instant.hour != manifest.utc_hour:
                raise ArtifactIntegrityError(f"row availability is outside manifest partition: {event_id}")
            rows_for_hash.append(
                {
                    "event_id": event_id,
                    "row_sha256": row_sha256,
                    "schema_version": row_payload["schema_version"],
                    "environment": row_payload["environment"],
                    "dataset": row_payload["dataset"],
                    "venue": row_payload["venue"],
                    "canonical_asset": row_payload["canonical_asset"],
                    "venue_symbol": row_payload["venue_symbol"],
                    "contract_id": row_payload["contract_id"],
                    "event_type": row_payload["event_type"],
                    "source_time_ns": row_payload["source_time_ns"],
                    "capture_time_ns": row_payload["capture_time_ns"],
                    "receive_time_ns": row_payload["receive_time_ns"],
                    "available_time_ns": available_time_ns,
                    "persistence_time_ns": row_payload["persistence_time_ns"],
                    "sequence_id": row_payload["sequence_id"],
                    "connection_id": row_payload["connection_id"],
                    "code_sha256": row_payload["code_sha256"],
                    "configuration_sha256": row_payload["configuration_sha256"],
                    "quality_flags_json": canonical_json_bytes(row_payload["quality_flags"]).decode("utf-8"),
                    "row_json": row_json,
                }
            )
        if hashlib.sha256(canonical_json_bytes(rows_for_hash)).hexdigest() != manifest.rows_sha256:
            raise ArtifactIntegrityError("artifact canonical rows hash mismatch")
    return VerifiedArtifact(resolved_manifest, data_path, manifest)


@dataclass(frozen=True, slots=True)
class DatasetVerificationReport:
    manifest_count: int
    row_count: int
    byte_count: int
    exact_duplicate_event_ids: tuple[str, ...]
    conflicting_event_ids: tuple[str, ...]
    future_data_event_ids: tuple[str, ...]
    orphan_parquet_paths: tuple[str, ...]
    temporary_paths: tuple[str, ...]
    manifest_sha256s: tuple[str, ...]
    parquet_backend: str

    @property
    def valid(self) -> bool:
        return (
            self.manifest_count > 0
            and self.row_count > 0
            and not (
                self.conflicting_event_ids
                or self.future_data_event_ids
                or self.orphan_parquet_paths
                or self.temporary_paths
            )
        )

    @property
    def report_sha256(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self)).hexdigest()


def verify_dataset(root: str | Path, *, backend: ParquetBackend | None = None) -> DatasetVerificationReport:
    raw_root = Path(root).expanduser().absolute()
    if raw_root.is_symlink():
        raise ArtifactIntegrityError("dataset root cannot be a symbolic link")
    resolved_root = raw_root.resolve()
    if not resolved_root.is_dir():
        raise ArtifactIntegrityError("dataset root must be an existing non-linked directory")
    parquet_backend = backend or PyArrowZstdBackend()
    manifest_paths = tuple(sorted(resolved_root.rglob("*.parquet.manifest.json"), key=str))
    verified = tuple(verify_artifact(resolved_root, path, backend=parquet_backend) for path in manifest_paths)
    declared = {item.data_path.resolve() for item in verified}
    actual = {path.resolve() for path in resolved_root.rglob("*.parquet") if not path.is_symlink()}
    orphan_paths = tuple(sorted(path.relative_to(resolved_root).as_posix() for path in actual - declared))
    temporary_paths = tuple(
        sorted(
            path.relative_to(resolved_root).as_posix()
            for path in resolved_root.rglob("*.tmp")
            if path.is_file() or path.is_symlink()
        )
    )
    identities: dict[str, str] = {}
    exact_duplicates: set[str] = set()
    conflicts: set[str] = set()
    future: set[str] = set()
    for item in verified:
        inspection = parquet_backend.inspect(item.data_path)
        for event_id, row_hash, _available, row_json in inspection.event_rows:
            existing = identities.get(event_id)
            if existing is not None:
                if existing == row_hash:
                    exact_duplicates.add(event_id)
                else:
                    conflicts.add(event_id)
            identities[event_id] = row_hash
            payload = json.loads(row_json)
            decision_time = payload.get("payload", {}).get("decision_time_ns")
            source_available = payload.get("payload", {}).get("source_available_time_ns")
            if (
                decision_time is not None
                and source_available is not None
                and int(source_available) > int(decision_time)
            ):
                future.add(event_id)
    return DatasetVerificationReport(
        manifest_count=len(verified),
        row_count=sum(item.manifest.row_count for item in verified),
        byte_count=sum(item.manifest.byte_count for item in verified),
        exact_duplicate_event_ids=tuple(sorted(exact_duplicates)),
        conflicting_event_ids=tuple(sorted(conflicts)),
        future_data_event_ids=tuple(sorted(future)),
        orphan_parquet_paths=orphan_paths,
        temporary_paths=temporary_paths,
        manifest_sha256s=tuple(item.manifest.manifest_sha256 for item in verified),
        parquet_backend=parquet_backend.name,
    )


@dataclass(frozen=True, slots=True)
class RetentionAudit:
    as_of_time_ns: int
    permanent_artifacts: tuple[str, ...]
    protected_artifacts: tuple[str, ...]
    eligible_raw_book_artifacts: tuple[str, ...]
    eligible_compact_artifacts: tuple[str, ...]

    @property
    def report_sha256(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self)).hexdigest()


def audit_retention(manifests: Sequence[ArtifactManifest], *, as_of_time_ns: int) -> RetentionAudit:
    as_of = epoch_nanoseconds(as_of_time_ns, "as_of_time_ns")
    permanent: list[str] = []
    protected: list[str] = []
    raw_eligible: list[str] = []
    compact_eligible: list[str] = []
    for manifest in manifests:
        expected = retention_class_for_dataset(manifest.dataset)
        required_until = required_retain_until_ns(expected, manifest.maximum_available_time_ns)
        if manifest.retention_class != expected or manifest.retain_until_ns != required_until:
            raise ArtifactIntegrityError("manifest violates the fixed retention policy")
        if expected == "permanent":
            permanent.append(manifest.relative_data_path)
        elif required_until is not None and as_of < required_until:
            protected.append(manifest.relative_data_path)
        elif expected == "raw_14d_min":
            raw_eligible.append(manifest.relative_data_path)
        else:
            compact_eligible.append(manifest.relative_data_path)
    return RetentionAudit(
        as_of,
        tuple(sorted(permanent)),
        tuple(sorted(protected)),
        tuple(sorted(raw_eligible)),
        tuple(sorted(compact_eligible)),
    )


__all__ = [
    "ARTIFACT_MANIFEST_VERSION",
    "ArtifactError",
    "ArtifactIntegrityError",
    "ArtifactManifest",
    "ArtifactPartition",
    "ArtifactRow",
    "COMPACT_RETENTION_DAYS",
    "DatasetVerificationReport",
    "GapRow",
    "NANOSECONDS_PER_DAY",
    "ParquetArtifactWriter",
    "ParquetBackend",
    "ParquetBackendUnavailable",
    "ParquetInspection",
    "PublishedArtifact",
    "RAW_BOOK_RETENTION_DAYS",
    "RetentionAudit",
    "RetentionClass",
    "VerifiedArtifact",
    "audit_retention",
    "load_artifact_manifest",
    "parquet_backend_available",
    "required_retain_until_ns",
    "retention_class_for_dataset",
    "verify_artifact",
    "verify_dataset",
]
