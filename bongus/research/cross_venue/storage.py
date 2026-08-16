"""Dedicated append-only SQLite persistence for cross-venue research.

This module intentionally has no dependency on Bongus runtime persistence.  A
research store must be named ``research.db`` and carries an independent schema
version and migration ledger.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Final, Literal

from bongus.research.cross_venue.schema import (
    PUBLIC_ENVIRONMENT,
    SCHEMA_VERSION,
    CanonicalAsset,
    ReservedCapital,
    Venue,
    decimal_text,
    epoch_nanoseconds,
    exact_decimal,
    exact_wire,
    nonnegative_decimal,
    positive_decimal,
)

RESEARCH_DB_FILENAME: Final[str] = "research.db"
RESEARCH_SCHEMA_VERSION: Final[int] = 1
AggregateAsset = Literal["UNIVERSE"]
ResearchAsset = CanonicalAsset | AggregateAsset
RequestMethod = Literal["GET", "POST", "FIXTURE"]


class ResearchStorageError(RuntimeError):
    """Base error for an invalid or inconsistent research database."""


class ConflictingResearchEventError(ResearchStorageError):
    """A deterministic event ID was reused with different immutable content."""


class ResearchDatabasePathError(ResearchStorageError):
    """The requested database path violates the dedicated-store boundary."""


def validate_research_db_path(path: str | Path) -> Path:
    resolved = Path(path).expanduser().resolve()
    if resolved.name.casefold() != RESEARCH_DB_FILENAME:
        raise ResearchDatabasePathError(f"cross-venue persistence requires a dedicated {RESEARCH_DB_FILENAME} file")
    return resolved


def canonical_json_bytes(payload: object) -> bytes:
    """Encode exact JSON deterministically, representing Decimal as strings."""

    return json.dumps(
        exact_wire(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _required_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _sha256_text(value: str, field_name: str) -> str:
    normalized = _required_text(value, field_name).casefold()
    if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
        raise ValueError(f"{field_name} must be a SHA-256 hex digest")
    return normalized


def _research_asset(value: ResearchAsset) -> ResearchAsset:
    if isinstance(value, CanonicalAsset):
        return value
    if value == "UNIVERSE":
        return value
    raise ValueError("research asset must be fixed-v1 canonical or UNIVERSE")


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


@dataclass(frozen=True, slots=True)
class RawSnapshotRecord:
    event_id: str
    dataset: str
    venue: Venue
    canonical_asset: ResearchAsset
    venue_symbol: str
    contract_id: str
    endpoint: str
    request_method: RequestMethod
    source_time_ns: int
    capture_time_ns: int
    receive_time_ns: int
    available_time_ns: int
    persistence_time_ns: int
    http_status: int
    response_headers: Mapping[str, str]
    payload_bytes: bytes
    code_sha256: str
    configuration_sha256: str
    sequence_id: str = "none"
    connection_id: str = "none"
    quality_flags: tuple[str, ...] = ()
    schema_version: str = SCHEMA_VERSION
    environment: str = PUBLIC_ENVIRONMENT

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION or self.environment != PUBLIC_ENVIRONMENT:
            raise ValueError("raw snapshots require the fixed public v1 envelope")
        if not isinstance(self.venue, Venue):
            raise TypeError("raw snapshot venue must use the fixed Venue enum")
        object.__setattr__(self, "canonical_asset", _research_asset(self.canonical_asset))
        for name in (
            "event_id",
            "dataset",
            "venue_symbol",
            "contract_id",
            "sequence_id",
            "connection_id",
        ):
            object.__setattr__(self, name, _required_text(getattr(self, name), name))
        endpoint = _required_text(self.endpoint, "endpoint")
        if not endpoint.startswith("/") or "?" in endpoint or "://" in endpoint:
            raise ValueError("endpoint must be a query-free path")
        object.__setattr__(self, "endpoint", endpoint)
        if self.request_method not in ("GET", "POST", "FIXTURE"):
            raise ValueError("request_method must be GET, POST, or FIXTURE")
        object.__setattr__(
            self,
            "source_time_ns",
            epoch_nanoseconds(self.source_time_ns, "source_time_ns"),
        )
        capture, receive, available, persistence = _timestamp_chain(
            self.capture_time_ns,
            self.receive_time_ns,
            self.available_time_ns,
            self.persistence_time_ns,
        )
        object.__setattr__(self, "capture_time_ns", capture)
        object.__setattr__(self, "receive_time_ns", receive)
        object.__setattr__(self, "available_time_ns", available)
        object.__setattr__(self, "persistence_time_ns", persistence)
        if (
            isinstance(self.http_status, bool)
            or not isinstance(self.http_status, int)
            or not 100 <= self.http_status <= 599
        ):
            raise ValueError("http_status must be an exact HTTP status integer")
        if not isinstance(self.payload_bytes, bytes):
            raise TypeError("payload_bytes must preserve an exact byte representation")
        object.__setattr__(self, "code_sha256", _sha256_text(self.code_sha256, "code_sha256"))
        object.__setattr__(
            self,
            "configuration_sha256",
            _sha256_text(self.configuration_sha256, "configuration_sha256"),
        )
        normalized_headers: dict[str, str] = {}
        for raw_name, raw_value in self.response_headers.items():
            name = _required_text(raw_name, "response header name").casefold()
            if not isinstance(raw_value, str):
                raise ValueError(f"response header {name} must be a string")
            value = raw_value
            if name in normalized_headers:
                raise ValueError(f"duplicate response header after normalization: {name}")
            normalized_headers[name] = value
        object.__setattr__(
            self,
            "response_headers",
            MappingProxyType(dict(sorted(normalized_headers.items()))),
        )
        flags = tuple(sorted({_required_text(flag, "quality flag") for flag in self.quality_flags}))
        object.__setattr__(self, "quality_flags", flags)

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(self.payload_bytes).hexdigest()


@dataclass(frozen=True, slots=True)
class OpportunitySnapshot:
    event_id: str
    canonical_asset: CanonicalAsset
    capture_time_ns: int
    receive_time_ns: int
    available_time_ns: int
    persistence_time_ns: int
    source_event_ids: tuple[str, ...]
    matched_base_quantity: Decimal
    binance_long_entry_price: Decimal
    hyperliquid_short_entry_price: Decimal
    holding_period_days: Decimal
    expected_funding_pnl_usd: Decimal
    expected_executable_price_pnl_usd: Decimal
    expected_commissions_usd: Decimal
    stablecoin_conversion_cost_usd: Decimal
    collateral_opportunity_cost_usd: Decimal
    repair_failure_cost_usd: Decimal
    reserved_capital: ReservedCapital
    code_sha256: str
    configuration_sha256: str
    long_venue: Venue = Venue.BINANCE
    short_venue: Venue = Venue.HYPERLIQUID
    quality_flags: tuple[str, ...] = ()
    schema_version: str = SCHEMA_VERSION
    environment: str = PUBLIC_ENVIRONMENT

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION or self.environment != PUBLIC_ENVIRONMENT:
            raise ValueError("opportunities require the fixed public v1 envelope")
        if not isinstance(self.canonical_asset, CanonicalAsset):
            raise TypeError("opportunity canonical_asset must use the fixed enum")
        if self.long_venue is not Venue.BINANCE or self.short_venue is not Venue.HYPERLIQUID:
            raise ValueError("v1 opportunities require Binance-long and Hyperliquid-short")
        object.__setattr__(self, "event_id", _required_text(self.event_id, "event_id"))
        capture, receive, available, persistence = _timestamp_chain(
            self.capture_time_ns,
            self.receive_time_ns,
            self.available_time_ns,
            self.persistence_time_ns,
        )
        object.__setattr__(self, "capture_time_ns", capture)
        object.__setattr__(self, "receive_time_ns", receive)
        object.__setattr__(self, "available_time_ns", available)
        object.__setattr__(self, "persistence_time_ns", persistence)
        source_ids = tuple(_required_text(event_id, "source_event_id") for event_id in self.source_event_ids)
        if not source_ids or len(source_ids) != len(set(source_ids)):
            raise ValueError("opportunity requires unique source event IDs")
        object.__setattr__(self, "source_event_ids", source_ids)
        for name in (
            "matched_base_quantity",
            "binance_long_entry_price",
            "hyperliquid_short_entry_price",
            "holding_period_days",
        ):
            object.__setattr__(self, name, positive_decimal(getattr(self, name), name))
        for name in (
            "expected_funding_pnl_usd",
            "expected_executable_price_pnl_usd",
        ):
            object.__setattr__(self, name, exact_decimal(getattr(self, name), name))
        for name in (
            "expected_commissions_usd",
            "stablecoin_conversion_cost_usd",
            "collateral_opportunity_cost_usd",
            "repair_failure_cost_usd",
        ):
            object.__setattr__(self, name, nonnegative_decimal(getattr(self, name), name))
        if not isinstance(self.reserved_capital, ReservedCapital):
            raise TypeError("opportunity requires the exact ReservedCapital contract")
        object.__setattr__(self, "code_sha256", _sha256_text(self.code_sha256, "code_sha256"))
        object.__setattr__(
            self,
            "configuration_sha256",
            _sha256_text(self.configuration_sha256, "configuration_sha256"),
        )
        flags = tuple(sorted({_required_text(flag, "quality flag") for flag in self.quality_flags}))
        object.__setattr__(self, "quality_flags", flags)

    @property
    def expected_net_pnl_usd(self) -> Decimal:
        return (
            self.expected_funding_pnl_usd
            + self.expected_executable_price_pnl_usd
            - self.expected_commissions_usd
            - self.stablecoin_conversion_cost_usd
            - self.collateral_opportunity_cost_usd
            - self.repair_failure_cost_usd
        )

    @property
    def total_reserved_capital_usd(self) -> Decimal:
        return self.reserved_capital.total_usd

    @property
    def expected_return_on_reserved_capital(self) -> Decimal:
        return self.expected_net_pnl_usd / self.total_reserved_capital_usd

    @property
    def simple_annualized_return(self) -> Decimal:
        return self.expected_return_on_reserved_capital * Decimal("365") / self.holding_period_days


_MIGRATION_1: Final[tuple[str, ...]] = (
    """
    CREATE TABLE raw_snapshots (
        event_id TEXT PRIMARY KEY,
        schema_version TEXT NOT NULL,
        environment TEXT NOT NULL,
        dataset TEXT NOT NULL,
        venue TEXT NOT NULL,
        canonical_asset TEXT NOT NULL,
        venue_symbol TEXT NOT NULL,
        contract_id TEXT NOT NULL,
        endpoint TEXT NOT NULL,
        request_method TEXT NOT NULL,
        source_time_ns INTEGER NOT NULL,
        capture_time_ns INTEGER NOT NULL,
        receive_time_ns INTEGER NOT NULL,
        available_time_ns INTEGER NOT NULL,
        persistence_time_ns INTEGER NOT NULL,
        sequence_id TEXT NOT NULL,
        connection_id TEXT NOT NULL,
        http_status INTEGER NOT NULL,
        response_headers_json TEXT NOT NULL,
        content_sha256 TEXT NOT NULL,
        payload_bytes BLOB NOT NULL,
        code_sha256 TEXT NOT NULL,
        configuration_sha256 TEXT NOT NULL,
        quality_flags_json TEXT NOT NULL
    ) STRICT
    """,
    "CREATE INDEX raw_snapshots_available_idx ON raw_snapshots(available_time_ns, event_id)",
    """
    CREATE TABLE opportunity_snapshots (
        event_id TEXT PRIMARY KEY,
        schema_version TEXT NOT NULL,
        environment TEXT NOT NULL,
        canonical_asset TEXT NOT NULL,
        long_venue TEXT NOT NULL,
        short_venue TEXT NOT NULL,
        capture_time_ns INTEGER NOT NULL,
        receive_time_ns INTEGER NOT NULL,
        available_time_ns INTEGER NOT NULL,
        persistence_time_ns INTEGER NOT NULL,
        source_event_ids_json TEXT NOT NULL,
        matched_base_quantity TEXT NOT NULL,
        binance_long_entry_price TEXT NOT NULL,
        hyperliquid_short_entry_price TEXT NOT NULL,
        holding_period_days TEXT NOT NULL,
        expected_funding_pnl_usd TEXT NOT NULL,
        expected_executable_price_pnl_usd TEXT NOT NULL,
        expected_commissions_usd TEXT NOT NULL,
        stablecoin_conversion_cost_usd TEXT NOT NULL,
        collateral_opportunity_cost_usd TEXT NOT NULL,
        repair_failure_cost_usd TEXT NOT NULL,
        binance_collateral_usd TEXT NOT NULL,
        hyperliquid_collateral_usd TEXT NOT NULL,
        liquidation_buffers_usd TEXT NOT NULL,
        idle_transfer_buffer_usd TEXT NOT NULL,
        total_reserved_capital_usd TEXT NOT NULL,
        expected_net_pnl_usd TEXT NOT NULL,
        expected_return_on_reserved_capital TEXT NOT NULL,
        simple_annualized_return TEXT NOT NULL,
        code_sha256 TEXT NOT NULL,
        configuration_sha256 TEXT NOT NULL,
        quality_flags_json TEXT NOT NULL
    ) STRICT
    """,
    "CREATE INDEX opportunity_available_idx ON opportunity_snapshots(available_time_ns, event_id)",
    """
    CREATE TRIGGER raw_snapshots_no_update
    BEFORE UPDATE ON raw_snapshots BEGIN
        SELECT RAISE(ABORT, 'raw snapshots are append-only');
    END
    """,
    """
    CREATE TRIGGER raw_snapshots_no_delete
    BEFORE DELETE ON raw_snapshots BEGIN
        SELECT RAISE(ABORT, 'raw snapshots are append-only');
    END
    """,
    """
    CREATE TRIGGER opportunity_snapshots_no_update
    BEFORE UPDATE ON opportunity_snapshots BEGIN
        SELECT RAISE(ABORT, 'opportunity snapshots are append-only');
    END
    """,
    """
    CREATE TRIGGER opportunity_snapshots_no_delete
    BEFORE DELETE ON opportunity_snapshots BEGIN
        SELECT RAISE(ABORT, 'opportunity snapshots are append-only');
    END
    """,
    """
    CREATE TRIGGER research_schema_migrations_no_update
    BEFORE UPDATE ON research_schema_migrations BEGIN
        SELECT RAISE(ABORT, 'research migrations are append-only');
    END
    """,
    """
    CREATE TRIGGER research_schema_migrations_no_delete
    BEFORE DELETE ON research_schema_migrations BEGIN
        SELECT RAISE(ABORT, 'research migrations are append-only');
    END
    """,
)


class ResearchStore:
    """Independent versioned store for immutable research evidence."""

    def __init__(self, path: str | Path = RESEARCH_DB_FILENAME) -> None:
        self.path = validate_research_db_path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = FULL")
        self._apply_migrations()

    def _apply_migrations(self) -> None:
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS research_schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                checksum_sha256 TEXT NOT NULL,
                applied_at_unix_ns INTEGER NOT NULL
            ) STRICT
            """
        )
        version = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
        if version > RESEARCH_SCHEMA_VERSION:
            raise ResearchStorageError("research database schema is newer than this code")
        migration_checksum = hashlib.sha256("\n".join(_MIGRATION_1).encode("utf-8")).hexdigest()
        existing = self._connection.execute(
            "SELECT checksum_sha256 FROM research_schema_migrations WHERE version = 1"
        ).fetchone()
        if existing is not None and existing[0] != migration_checksum:
            raise ResearchStorageError("research migration checksum mismatch")
        if version < 1:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                for statement in _MIGRATION_1:
                    self._connection.execute(statement)
                self._connection.execute(
                    """
                    INSERT INTO research_schema_migrations(
                        version, name, checksum_sha256, applied_at_unix_ns
                    ) VALUES (?, ?, ?, unixepoch('subsec') * 1000000000)
                    """,
                    (1, "initial_append_only_research_store", migration_checksum),
                )
                self._connection.execute("PRAGMA user_version = 1")
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
        row = self._connection.execute("SELECT MAX(version) FROM research_schema_migrations").fetchone()
        recorded = int(row[0] or 0)
        current = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
        if current != RESEARCH_SCHEMA_VERSION or recorded != RESEARCH_SCHEMA_VERSION:
            raise ResearchStorageError("research schema metadata is incomplete")

    @property
    def schema_version(self) -> int:
        return int(self._connection.execute("PRAGMA user_version").fetchone()[0])

    def migration_metadata(self) -> tuple[Mapping[str, object], ...]:
        rows = self._connection.execute(
            """
            SELECT version, name, checksum_sha256, applied_at_unix_ns
            FROM research_schema_migrations ORDER BY version
            """
        ).fetchall()
        return tuple(MappingProxyType(dict(row)) for row in rows)

    @staticmethod
    def _raw_values(record: RawSnapshotRecord) -> tuple[object, ...]:
        return (
            record.event_id,
            record.schema_version,
            record.environment,
            record.dataset,
            record.venue.value,
            record.canonical_asset.value
            if isinstance(record.canonical_asset, CanonicalAsset)
            else record.canonical_asset,
            record.venue_symbol,
            record.contract_id,
            record.endpoint,
            record.request_method,
            record.source_time_ns,
            record.capture_time_ns,
            record.receive_time_ns,
            record.available_time_ns,
            record.persistence_time_ns,
            record.sequence_id,
            record.connection_id,
            record.http_status,
            canonical_json_bytes(record.response_headers).decode("utf-8"),
            record.content_sha256,
            record.payload_bytes,
            record.code_sha256,
            record.configuration_sha256,
            canonical_json_bytes(record.quality_flags).decode("utf-8"),
        )

    def append_raw_snapshot(self, record: RawSnapshotRecord) -> bool:
        columns = (
            "event_id, schema_version, environment, dataset, venue, canonical_asset, "
            "venue_symbol, contract_id, endpoint, request_method, source_time_ns, "
            "capture_time_ns, receive_time_ns, available_time_ns, persistence_time_ns, "
            "sequence_id, connection_id, http_status, response_headers_json, "
            "content_sha256, payload_bytes, code_sha256, configuration_sha256, "
            "quality_flags_json"
        )
        values = self._raw_values(record)
        placeholders = ", ".join("?" for _ in values)
        try:
            self._connection.execute(f"INSERT INTO raw_snapshots({columns}) VALUES ({placeholders})", values)
            return True
        except sqlite3.IntegrityError as exc:
            existing = self._connection.execute(
                f"SELECT {columns} FROM raw_snapshots WHERE event_id = ?",
                (record.event_id,),
            ).fetchone()
            if existing is not None and tuple(existing) == values:
                return False
            raise ConflictingResearchEventError(f"conflicting raw snapshot event_id: {record.event_id}") from exc

    @staticmethod
    def _opportunity_values(record: OpportunitySnapshot) -> tuple[object, ...]:
        capital = record.reserved_capital
        return (
            record.event_id,
            record.schema_version,
            record.environment,
            record.canonical_asset.value,
            record.long_venue.value,
            record.short_venue.value,
            record.capture_time_ns,
            record.receive_time_ns,
            record.available_time_ns,
            record.persistence_time_ns,
            canonical_json_bytes(record.source_event_ids).decode("utf-8"),
            decimal_text(record.matched_base_quantity),
            decimal_text(record.binance_long_entry_price),
            decimal_text(record.hyperliquid_short_entry_price),
            decimal_text(record.holding_period_days),
            decimal_text(record.expected_funding_pnl_usd),
            decimal_text(record.expected_executable_price_pnl_usd),
            decimal_text(record.expected_commissions_usd),
            decimal_text(record.stablecoin_conversion_cost_usd),
            decimal_text(record.collateral_opportunity_cost_usd),
            decimal_text(record.repair_failure_cost_usd),
            decimal_text(capital.binance_collateral_usd),
            decimal_text(capital.hyperliquid_collateral_usd),
            decimal_text(capital.liquidation_buffers_usd),
            decimal_text(capital.idle_transfer_buffer_usd),
            decimal_text(record.total_reserved_capital_usd),
            decimal_text(record.expected_net_pnl_usd),
            decimal_text(record.expected_return_on_reserved_capital),
            decimal_text(record.simple_annualized_return),
            record.code_sha256,
            record.configuration_sha256,
            canonical_json_bytes(record.quality_flags).decode("utf-8"),
        )

    def append_opportunity_snapshot(self, record: OpportunitySnapshot) -> bool:
        source_placeholders = ", ".join("?" for _ in record.source_event_ids)
        source_rows = self._connection.execute(
            f"""
            SELECT event_id, available_time_ns FROM raw_snapshots
            WHERE event_id IN ({source_placeholders})
            """,
            record.source_event_ids,
        ).fetchall()
        if len(source_rows) != len(record.source_event_ids):
            raise ResearchStorageError("opportunity source events must already exist in research.db")
        if any(row["available_time_ns"] > record.capture_time_ns for row in source_rows):
            raise ResearchStorageError("opportunity cannot join a source before its availability time")
        columns = (
            "event_id, schema_version, environment, canonical_asset, long_venue, "
            "short_venue, capture_time_ns, receive_time_ns, available_time_ns, "
            "persistence_time_ns, "
            "source_event_ids_json, matched_base_quantity, binance_long_entry_price, "
            "hyperliquid_short_entry_price, holding_period_days, "
            "expected_funding_pnl_usd, expected_executable_price_pnl_usd, "
            "expected_commissions_usd, stablecoin_conversion_cost_usd, "
            "collateral_opportunity_cost_usd, repair_failure_cost_usd, "
            "binance_collateral_usd, hyperliquid_collateral_usd, "
            "liquidation_buffers_usd, idle_transfer_buffer_usd, "
            "total_reserved_capital_usd, expected_net_pnl_usd, "
            "expected_return_on_reserved_capital, simple_annualized_return, "
            "code_sha256, configuration_sha256, quality_flags_json"
        )
        values = self._opportunity_values(record)
        placeholders = ", ".join("?" for _ in values)
        try:
            self._connection.execute(
                f"INSERT INTO opportunity_snapshots({columns}) VALUES ({placeholders})",
                values,
            )
            return True
        except sqlite3.IntegrityError as exc:
            existing = self._connection.execute(
                f"SELECT {columns} FROM opportunity_snapshots WHERE event_id = ?",
                (record.event_id,),
            ).fetchone()
            if existing is not None and tuple(existing) == values:
                return False
            raise ConflictingResearchEventError(f"conflicting opportunity event_id: {record.event_id}") from exc

    def iter_raw_snapshots(self) -> Iterator[RawSnapshotRecord]:
        rows = self._connection.execute("SELECT * FROM raw_snapshots ORDER BY available_time_ns, event_id")
        for row in rows:
            payload = bytes(row["payload_bytes"])
            if hashlib.sha256(payload).hexdigest() != row["content_sha256"]:
                raise ResearchStorageError(f"raw snapshot content hash mismatch: {row['event_id']}")
            asset: ResearchAsset = (
                "UNIVERSE" if row["canonical_asset"] == "UNIVERSE" else CanonicalAsset(row["canonical_asset"])
            )
            headers = json.loads(row["response_headers_json"])
            flags = json.loads(row["quality_flags_json"])
            yield RawSnapshotRecord(
                event_id=row["event_id"],
                dataset=row["dataset"],
                venue=Venue(row["venue"]),
                canonical_asset=asset,
                venue_symbol=row["venue_symbol"],
                contract_id=row["contract_id"],
                endpoint=row["endpoint"],
                request_method=row["request_method"],
                source_time_ns=row["source_time_ns"],
                capture_time_ns=row["capture_time_ns"],
                receive_time_ns=row["receive_time_ns"],
                available_time_ns=row["available_time_ns"],
                persistence_time_ns=row["persistence_time_ns"],
                http_status=row["http_status"],
                response_headers=headers,
                payload_bytes=payload,
                code_sha256=row["code_sha256"],
                configuration_sha256=row["configuration_sha256"],
                sequence_id=row["sequence_id"],
                connection_id=row["connection_id"],
                quality_flags=tuple(flags),
                schema_version=row["schema_version"],
                environment=row["environment"],
            )

    def iter_opportunity_snapshots(self) -> Iterator[OpportunitySnapshot]:
        rows = self._connection.execute("SELECT * FROM opportunity_snapshots ORDER BY available_time_ns, event_id")
        for row in rows:
            record = OpportunitySnapshot(
                event_id=row["event_id"],
                canonical_asset=CanonicalAsset(row["canonical_asset"]),
                long_venue=Venue(row["long_venue"]),
                short_venue=Venue(row["short_venue"]),
                capture_time_ns=row["capture_time_ns"],
                receive_time_ns=row["receive_time_ns"],
                available_time_ns=row["available_time_ns"],
                persistence_time_ns=row["persistence_time_ns"],
                source_event_ids=tuple(json.loads(row["source_event_ids_json"])),
                matched_base_quantity=Decimal(row["matched_base_quantity"]),
                binance_long_entry_price=Decimal(row["binance_long_entry_price"]),
                hyperliquid_short_entry_price=Decimal(row["hyperliquid_short_entry_price"]),
                holding_period_days=Decimal(row["holding_period_days"]),
                expected_funding_pnl_usd=Decimal(row["expected_funding_pnl_usd"]),
                expected_executable_price_pnl_usd=Decimal(row["expected_executable_price_pnl_usd"]),
                expected_commissions_usd=Decimal(row["expected_commissions_usd"]),
                stablecoin_conversion_cost_usd=Decimal(row["stablecoin_conversion_cost_usd"]),
                collateral_opportunity_cost_usd=Decimal(row["collateral_opportunity_cost_usd"]),
                repair_failure_cost_usd=Decimal(row["repair_failure_cost_usd"]),
                reserved_capital=ReservedCapital(
                    binance_collateral_usd=Decimal(row["binance_collateral_usd"]),
                    hyperliquid_collateral_usd=Decimal(row["hyperliquid_collateral_usd"]),
                    liquidation_buffers_usd=Decimal(row["liquidation_buffers_usd"]),
                    idle_transfer_buffer_usd=Decimal(row["idle_transfer_buffer_usd"]),
                ),
                code_sha256=row["code_sha256"],
                configuration_sha256=row["configuration_sha256"],
                quality_flags=tuple(json.loads(row["quality_flags_json"])),
                schema_version=row["schema_version"],
                environment=row["environment"],
            )
            calculated = (
                decimal_text(record.total_reserved_capital_usd),
                decimal_text(record.expected_net_pnl_usd),
                decimal_text(record.expected_return_on_reserved_capital),
                decimal_text(record.simple_annualized_return),
            )
            stored = (
                row["total_reserved_capital_usd"],
                row["expected_net_pnl_usd"],
                row["expected_return_on_reserved_capital"],
                row["simple_annualized_return"],
            )
            if calculated != stored:
                raise ResearchStorageError(f"opportunity derived-value integrity mismatch: {record.event_id}")
            yield record

    def execute_readonly(self, sql: str, parameters: tuple[object, ...] = ()) -> tuple[sqlite3.Row, ...]:
        normalized = sql.lstrip().casefold()
        if not normalized.startswith("select"):
            raise ResearchStorageError("research diagnostic SQL must be read-only")
        return tuple(self._connection.execute(sql, parameters).fetchall())

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> ResearchStore:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()


__all__ = [
    "AggregateAsset",
    "ConflictingResearchEventError",
    "OpportunitySnapshot",
    "RESEARCH_DB_FILENAME",
    "RESEARCH_SCHEMA_VERSION",
    "RawSnapshotRecord",
    "RequestMethod",
    "ResearchAsset",
    "ResearchDatabasePathError",
    "ResearchStorageError",
    "ResearchStore",
    "canonical_json_bytes",
    "validate_research_db_path",
]
