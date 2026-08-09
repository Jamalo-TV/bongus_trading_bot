"""Fail-closed, offline migration of the legacy monolithic SQLite store.

The migration deliberately has no integration with the running trader.  It
opens the source and its required backup through immutable read-only SQLite
URIs, builds three databases in a private sibling staging directory, verifies
their logical content, and publishes the complete directory with one rename.
The caller must stop every process that can access the source before invoking
this module.

Run ``python -m bongus.engine.offline_storage_migration --help`` for the
operator-facing CLI.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
import ctypes
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import errno
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import struct
import sys
import tempfile
from types import TracebackType
from typing import Any, Final, Protocol
from urllib.parse import quote

from bongus.engine.cooldown_manager import CooldownManager
from bongus.engine.database_backup import BackupError, BackupResult, verify_backup
from bongus.engine.state_store import (
    APPLICATION_ID,
    CURRENT_SCHEMA_VERSION,
    _apply_migrations,
)
from bongus.market_data.feed_recovery import FeedCursorStore
from bongus.portfolio.capital_reservations import CapitalReservationBook
from bongus.supervisor.store import SUPERVISOR_SCHEMA


MIGRATION_FORMAT: Final = "bongus-offline-storage-split-v1"
MANIFEST_FILENAME: Final = "migration-manifest.json"
DEFAULT_REQUIRED_HEADROOM_BYTES: Final = 512_000_000
COPY_BATCH_ROWS: Final = 2_000
_REPARSE_POINT_ATTRIBUTE: Final = 0x0400


class MigrationError(RuntimeError):
    """Base exception for an offline migration that cannot prove safety."""


class SourceNotQuiescentError(MigrationError):
    """Raised when the source can be changing or SQLite sidecars exist."""


class SchemaRoutingError(MigrationError):
    """Raised when a source schema contains an unclassified object."""


class DiskUsage(Protocol):
    @property
    def free(self) -> int: ...


DiskUsageProbe = Callable[[Path], DiskUsage]
FaultInjector = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class TableRoute:
    database: str
    tier: str
    description: str


# This is intentionally exhaustive instead of using name patterns.  A new
# table is safety-relevant until an operator-reviewed release classifies it.
TABLE_ROUTES: Final[Mapping[str, TableRoute]] = {
    # Hot state and durable recovery watermarks.
    "schema_meta": TableRoute("state.db", "A", "schema and split activation state"),
    "positions": TableRoute("state.db", "A", "open and recovery positions"),
    "portfolio_stats": TableRoute("state.db", "A", "current portfolio projection"),
    "risk_state": TableRoute("state.db", "A", "latched risk and runtime state"),
    "pending_intents": TableRoute("state.db", "A", "pending execution intents"),
    "telemetry_receipts": TableRoute("state.db", "A", "telemetry replay watermarks"),
    "execution_command_sequences": TableRoute("state.db", "A", "durable producer sequences"),
    "execution_command_outbox": TableRoute("state.db", "A", "durable command outbox"),
    "exchange_statement_cursors": TableRoute("state.db", "A", "exchange recovery cursors"),
    "feed_cursors": TableRoute("state.db", "A", "market-data recovery cursors"),
    "cooldown_entries": TableRoute("state.db", "A", "restart-safe risk cooldowns"),
    # Reservation events stay beside their parent because their declared
    # foreign key cannot cross SQLite files.
    "capital_reservations": TableRoute("state.db", "A", "gross and collateral reservations"),
    "capital_reservation_events": TableRoute("state.db", "A", "reservation lifecycle"),
    "parameter_promotions": TableRoute("state.db", "A", "active governance state"),
    "ai_report_proposals": TableRoute("state.db", "A", "operator-governed proposals"),
    "supervisor_runtime": TableRoute("state.db", "A", "supervisor recovery watermarks"),
    "supervisor_incidents": TableRoute("state.db", "A", "active supervised incidents"),
    "supervisor_incident_events": TableRoute(
        "state.db", "A", "restart-safe incident transition journal"
    ),
    "supervisor_recommendations": TableRoute(
        "state.db", "A", "operator-governed recommendations"
    ),
    "supervisor_reports": TableRoute("state.db", "A", "delivered supervisor reports"),
    "supervisor_alerts": TableRoute("state.db", "A", "durable alert deduplication"),
    # Immutable economic and execution evidence plus bounded operational audit.
    "trade_history": TableRoute("audit.db", "A", "completed economic cycles"),
    "execution_events": TableRoute("audit.db", "A", "order and fill lifecycle"),
    "economic_ledger_events": TableRoute("audit.db", "A", "immutable economic ledger"),
    "exchange_statement_entries": TableRoute("audit.db", "A", "immutable exchange statements"),
    "execution_decisions": TableRoute("audit.db", "A", "hash-bound execution decisions"),
    "lifecycle_events": TableRoute("audit.db", "A", "immutable lifecycle journal"),
    "validation_snapshots": TableRoute("audit.db", "A", "promotion validation evidence"),
    "archive_batch_manifests": TableRoute("audit.db", "A", "verified archival batches"),
    "feed_recovery_events": TableRoute("audit.db", "B", "bounded feed recovery evidence"),
    "health_samples": TableRoute("audit.db", "B", "bounded operational health history"),
    # Reproducible research evidence.  These are the only tables that may be
    # omitted, and only because the verified backup remains authoritative.
    "candidate_snapshots": TableRoute("research.db", "C", "candidate funnel evidence"),
    "opportunity_scores": TableRoute("research.db", "C", "opportunity rankings"),
    "feature_snapshots": TableRoute("research.db", "C", "legacy feature evidence"),
    "execution_quality": TableRoute("research.db", "C", "execution quality samples"),
    "model_shadow_decisions": TableRoute("research.db", "C", "counterfactual decisions"),
    "market_samples": TableRoute("research.db", "C", "reproducible market samples"),
    "market_hourly_aggregates": TableRoute(
        "research.db", "C", "bounded hourly market rollups"
    ),
}

DESTINATION_NAMES: Final = ("state.db", "audit.db", "research.db")


@dataclass(frozen=True, slots=True)
class FileIdentity:
    size_bytes: int
    modified_ns: int
    sha256: str


@dataclass(frozen=True, slots=True)
class SchemaObject:
    object_type: str
    name: str
    table_name: str
    sql: str


@dataclass(frozen=True, slots=True)
class TableEvidence:
    name: str
    create_sql: str
    columns: tuple[str, ...]
    order_by: tuple[str, ...]
    row_count: int
    content_sha256: str
    routed_content_sha256: str


@dataclass(frozen=True, slots=True)
class DatabaseEvidence:
    application_id: int
    user_version: int
    auto_vacuum: int
    page_size: int
    page_count: int
    quick_check: str
    integrity_check: str
    foreign_key_violations: int
    tables: Mapping[str, TableEvidence]
    sqlite_sequences: Mapping[str, int]
    schema_objects: tuple[SchemaObject, ...]
    canonical_sha256: str


@dataclass(frozen=True, slots=True)
class MigrationPreflight:
    source_path: Path
    backup_path: Path
    backup_manifest_path: Path
    output_directory: Path
    retain_research: bool
    required_headroom_bytes: int
    estimated_operation_bytes: int
    required_free_bytes: int
    observed_free_bytes: int
    source_identity: FileIdentity
    backup_identity: FileIdentity
    backup_manifest_identity: FileIdentity
    source_evidence: DatabaseEvidence

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "READY",
            "source_path": str(self.source_path),
            "backup_path": str(self.backup_path),
            "backup_manifest_path": str(self.backup_manifest_path),
            "output_directory": str(self.output_directory),
            "retain_research": self.retain_research,
            "required_headroom_bytes": self.required_headroom_bytes,
            "estimated_operation_bytes": self.estimated_operation_bytes,
            "required_free_bytes": self.required_free_bytes,
            "observed_free_bytes": self.observed_free_bytes,
            "source_identity": asdict(self.source_identity),
            "backup_identity": asdict(self.backup_identity),
            "backup_manifest_identity": asdict(self.backup_manifest_identity),
            "source": _database_summary(self.source_evidence),
            "routing": _routing_summary(self.source_evidence, self.retain_research),
        }


@dataclass(frozen=True, slots=True)
class MigrationResult:
    output_directory: Path
    manifest_path: Path
    manifest_sha256: str
    destination_paths: Mapping[str, Path]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "PUBLISHED",
            "output_directory": str(self.output_directory),
            "manifest_path": str(self.manifest_path),
            "manifest_sha256": self.manifest_sha256,
            "destinations": {
                name: str(path) for name, path in sorted(self.destination_paths.items())
            },
        }


@dataclass(frozen=True, slots=True)
class PublishedMigrationVerification:
    """Identity proven for a published three-database migration directory."""

    output_directory: Path
    manifest_path: Path
    manifest_sha256: str
    destination_paths: Mapping[str, Path]


class _ReadOnlyDatabase:
    """Context manager that can only create an immutable read-only handle."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection: sqlite3.Connection | None = None

    def __enter__(self) -> sqlite3.Connection:
        encoded = quote(self.path.resolve(strict=True).as_posix(), safe="/:")
        connection = sqlite3.connect(
            f"file:{encoded}?mode=ro&immutable=1",
            uri=True,
            timeout=30,
        )
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA temp_store=MEMORY")
        self.connection = connection
        return connection

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _manifest_digest(payload: Mapping[str, Any]) -> str:
    unsigned = {key: value for key, value in payload.items() if key != "manifest_sha256"}
    return hashlib.sha256(_canonical_json_bytes(unsigned)).hexdigest()


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDWR)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        # Windows does not permit directory handles through os.open.
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _is_link_or_reparse(path: Path, metadata: os.stat_result) -> bool:
    return path.is_symlink() or bool(
        getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT_ATTRIBUTE
    )


def _safe_regular_file(path: Path, *, label: str) -> Path:
    candidate = path.absolute()
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise MigrationError(f"{label} does not exist: {candidate}") from exc
    if _is_link_or_reparse(candidate, metadata) or not stat.S_ISREG(metadata.st_mode):
        raise MigrationError(f"{label} must be a regular non-link/reparse file: {candidate}")
    return candidate.resolve(strict=True)


def _safe_output_parent(output: Path) -> Path:
    parent = output.absolute().parent
    try:
        metadata = parent.lstat()
    except OSError as exc:
        raise MigrationError(f"output parent does not exist: {parent}") from exc
    if _is_link_or_reparse(parent, metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise MigrationError(f"output parent must be a regular non-link/reparse directory: {parent}")
    return parent.resolve(strict=True)


def _path_lexists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise MigrationError(f"cannot inspect path: {path}: {exc}") from exc
    return True


def _assert_no_sidecars(path: Path, *, label: str) -> None:
    present = [
        sidecar
        for sidecar in (Path(f"{path}-wal"), Path(f"{path}-shm"))
        if _path_lexists(sidecar)
    ]
    if present:
        names = ", ".join(str(item) for item in present)
        raise SourceNotQuiescentError(
            f"{label} has SQLite WAL/SHM sidecars and is not quiescent: {names}"
        )


def _file_identity(path: Path) -> FileIdentity:
    before = path.stat()
    digest = _sha256_file(path)
    after = path.stat()
    before_tuple = (int(before.st_size), int(before.st_mtime_ns))
    after_tuple = (int(after.st_size), int(after.st_mtime_ns))
    if before_tuple != after_tuple:
        raise SourceNotQuiescentError(f"database changed while it was being hashed: {path}")
    return FileIdentity(
        size_bytes=after_tuple[0],
        modified_ns=after_tuple[1],
        sha256=digest,
    )


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _integrity_result(connection: sqlite3.Connection, pragma: str) -> str:
    rows = connection.execute(f"PRAGMA {pragma}").fetchall()
    result = "; ".join(str(row[0]) for row in rows)
    if result.lower() != "ok":
        raise MigrationError(f"SQLite {pragma} failed: {result[:500]}")
    return result


def _schema_objects(connection: sqlite3.Connection) -> tuple[SchemaObject, ...]:
    unsupported = connection.execute(
        """
        SELECT type, name
        FROM sqlite_master
        WHERE type NOT IN ('table', 'index', 'trigger')
          AND name NOT LIKE 'sqlite_%'
        ORDER BY type, name
        """
    ).fetchall()
    if unsupported:
        rendered = ", ".join(f"{row[0]}:{row[1]}" for row in unsupported)
        raise SchemaRoutingError(f"unsupported SQLite schema objects: {rendered}")

    rows = connection.execute(
        """
        SELECT type, name, tbl_name, sql
        FROM sqlite_master
        WHERE type IN ('index', 'trigger')
          AND name NOT LIKE 'sqlite_%'
          AND sql IS NOT NULL
        ORDER BY type, name
        """
    ).fetchall()
    return tuple(
        SchemaObject(
            object_type=str(row[0]),
            name=str(row[1]),
            table_name=str(row[2]),
            sql=str(row[3]),
        )
        for row in rows
    )


def _table_layout(
    connection: sqlite3.Connection,
    table_name: str,
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    if row is None or row[0] is None:
        raise SchemaRoutingError(f"table has no replayable CREATE statement: {table_name}")
    info = connection.execute(f"PRAGMA table_xinfo({_quote_identifier(table_name)})").fetchall()
    columns = tuple(str(item[1]) for item in info if int(item[6]) == 0)
    if not columns:
        raise SchemaRoutingError(f"table has no copyable columns: {table_name}")
    primary = tuple(
        str(item[1])
        for item in sorted(info, key=lambda value: int(value[5]) or 1_000_000)
        if int(item[5]) > 0 and int(item[6]) == 0
    )
    return str(row[0]), columns, primary or ("_rowid_",)


def _encode_value(value: Any) -> bytes:
    if value is None:
        return b"n"
    if isinstance(value, bytes):
        return b"b" + len(value).to_bytes(8, "big") + value
    if isinstance(value, int):
        encoded = str(value).encode("ascii")
        return b"i" + len(encoded).to_bytes(8, "big") + encoded
    if isinstance(value, float):
        return b"f" + struct.pack(">d", value)
    if isinstance(value, str):
        encoded = value.encode("utf-8", errors="surrogatepass")
        return b"s" + len(encoded).to_bytes(8, "big") + encoded
    raise MigrationError(f"unsupported SQLite value type: {type(value).__name__}")


def _new_table_digest(table_name: str, columns: Sequence[str]) -> hashlib._Hash:
    digest = hashlib.sha256()
    digest.update(b"bongus-table-content-v1\0")
    digest.update(table_name.encode("utf-8"))
    digest.update(b"\0")
    for column in columns:
        encoded = column.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    return digest


def _ordered_rows(
    connection: sqlite3.Connection,
    table_name: str,
    columns: Sequence[str],
    order_by: Sequence[str],
) -> sqlite3.Cursor:
    selected = ", ".join(_quote_identifier(item) for item in columns)
    ordering = ", ".join(_quote_identifier(item) if item != "_rowid_" else item for item in order_by)
    return connection.execute(
        f"SELECT {selected} FROM {_quote_identifier(table_name)} ORDER BY {ordering}"
    )


def _hash_table(
    connection: sqlite3.Connection,
    table_name: str,
    create_sql: str,
    columns: tuple[str, ...],
    order_by: tuple[str, ...],
) -> TableEvidence:
    digest = _new_table_digest(table_name, columns)
    routed_columns = tuple(sorted(columns))
    routed_digest = _new_table_digest(table_name, routed_columns)
    routed_indexes = tuple(columns.index(column) for column in routed_columns)
    count = 0
    cursor = _ordered_rows(connection, table_name, columns, order_by)
    while rows := cursor.fetchmany(COPY_BATCH_ROWS):
        for row in rows:
            digest.update(b"\x1e")
            for value in row:
                encoded = _encode_value(value)
                digest.update(len(encoded).to_bytes(8, "big"))
                digest.update(encoded)
            routed_digest.update(b"\x1e")
            for index in routed_indexes:
                encoded = _encode_value(row[index])
                routed_digest.update(len(encoded).to_bytes(8, "big"))
                routed_digest.update(encoded)
            count += 1
    return TableEvidence(
        name=table_name,
        create_sql=create_sql,
        columns=columns,
        order_by=order_by,
        row_count=count,
        content_sha256=digest.hexdigest(),
        routed_content_sha256=routed_digest.hexdigest(),
    )


def _canonical_evidence_hash(
    *,
    application_id: int,
    user_version: int,
    auto_vacuum: int,
    page_size: int,
    tables: Mapping[str, TableEvidence],
    sqlite_sequences: Mapping[str, int],
    schema_objects: Sequence[SchemaObject],
) -> str:
    payload = {
        "format": "bongus-sqlite-logical-v1",
        "application_id": application_id,
        "user_version": user_version,
        "auto_vacuum": auto_vacuum,
        "page_size": page_size,
        "tables": {
            name: {
                "create_sql": evidence.create_sql,
                "columns": list(evidence.columns),
                "order_by": list(evidence.order_by),
                "row_count": evidence.row_count,
                "content_sha256": evidence.content_sha256,
            }
            for name, evidence in sorted(tables.items())
        },
        "sqlite_sequences": dict(sorted(sqlite_sequences.items())),
        "schema_objects": [asdict(item) for item in schema_objects],
    }
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def _inspect_database(
    connection: sqlite3.Connection,
    *,
    require_current_source_schema: bool = True,
) -> DatabaseEvidence:
    quick_check = _integrity_result(connection, "quick_check")
    integrity_check = _integrity_result(connection, "integrity_check")
    foreign_rows = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_rows:
        sample = "; ".join(str(tuple(row)) for row in foreign_rows[:10])
        raise MigrationError(f"SQLite foreign_key_check failed: {sample}")

    application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
    user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    auto_vacuum = int(connection.execute("PRAGMA auto_vacuum").fetchone()[0])
    page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
    page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
    table_names = tuple(
        str(row[0])
        for row in connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type='table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()
    )
    unknown = sorted(set(table_names) - set(TABLE_ROUTES))
    if unknown:
        raise SchemaRoutingError(
            "unclassified tables make migration unsafe: " + ", ".join(unknown)
        )
    if require_current_source_schema:
        missing_base = sorted(
            {
                "schema_meta",
                "positions",
                "risk_state",
                "pending_intents",
                "execution_events",
                "economic_ledger_events",
                "exchange_statement_entries",
            }
            - set(table_names)
        )
        if missing_base:
            raise SchemaRoutingError(
                "source is not a current StateWriter schema; missing: "
                + ", ".join(missing_base)
            )

    for table_name in table_names:
        foreign_keys = connection.execute(
            f"PRAGMA foreign_key_list({_quote_identifier(table_name)})"
        ).fetchall()
        for foreign_key in foreign_keys:
            referenced_table = str(foreign_key[2])
            if referenced_table not in TABLE_ROUTES:
                raise SchemaRoutingError(
                    f"{table_name} has a foreign key to unclassified table "
                    f"{referenced_table}"
                )
            if TABLE_ROUTES[referenced_table].database != TABLE_ROUTES[table_name].database:
                raise SchemaRoutingError(
                    f"foreign key cannot cross split databases: {table_name} -> "
                    f"{referenced_table}"
                )

    objects = _schema_objects(connection)
    object_unknown = sorted(
        {item.table_name for item in objects if item.table_name not in table_names}
    )
    if object_unknown:
        raise SchemaRoutingError(
            "schema objects reference unclassified tables: " + ", ".join(object_unknown)
        )

    tables: dict[str, TableEvidence] = {}
    for table_name in table_names:
        create_sql, columns, order_by = _table_layout(connection, table_name)
        tables[table_name] = _hash_table(
            connection,
            table_name,
            create_sql,
            columns,
            order_by,
        )
    has_sequences = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'"
    ).fetchone()
    sqlite_sequences = (
        {
            str(row[0]): int(row[1])
            for row in connection.execute(
                "SELECT name, seq FROM sqlite_sequence ORDER BY name"
            ).fetchall()
        }
        if has_sequences is not None
        else {}
    )
    orphan_sequences = sorted(set(sqlite_sequences) - set(table_names))
    if orphan_sequences:
        raise SchemaRoutingError(
            "sqlite_sequence references unclassified tables: "
            + ", ".join(orphan_sequences)
        )
    canonical = _canonical_evidence_hash(
        application_id=application_id,
        user_version=user_version,
        auto_vacuum=auto_vacuum,
        page_size=page_size,
        tables=tables,
        sqlite_sequences=sqlite_sequences,
        schema_objects=objects,
    )
    return DatabaseEvidence(
        application_id=application_id,
        user_version=user_version,
        auto_vacuum=auto_vacuum,
        page_size=page_size,
        page_count=page_count,
        quick_check=quick_check,
        integrity_check=integrity_check,
        foreign_key_violations=0,
        tables=tables,
        sqlite_sequences=sqlite_sequences,
        schema_objects=objects,
        canonical_sha256=canonical,
    )


@lru_cache(maxsize=1)
def canonical_runtime_evidence() -> DatabaseEvidence:
    """Return the complete current runtime schema with empty table evidence.

    Legacy deployments may not yet contain schemas owned by a newer auxiliary
    process (for example the supervisor).  Migration must still publish the
    complete current three-store contract; otherwise its artifact cannot pass
    first activation.  Existing source rows are copied into these canonical
    tables after exact column compatibility is proven.
    """

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
        _apply_migrations(connection)
        cooldowns = CooldownManager(connection=connection)
        feed_cursors = FeedCursorStore(connection=connection)
        reservations = CapitalReservationBook(connection=connection)
        connection.executescript(SUPERVISOR_SCHEMA)
        cooldowns.close()
        feed_cursors.close()
        reservations.close()
        evidence = _inspect_database(
            connection,
            require_current_source_schema=False,
        )
    finally:
        connection.close()
    missing = sorted(set(TABLE_ROUTES) - set(evidence.tables))
    unknown = sorted(set(evidence.tables) - set(TABLE_ROUTES))
    if missing or unknown:
        raise SchemaRoutingError(
            "canonical runtime routing is not exhaustive: "
            f"missing={missing}, unclassified={unknown}"
        )
    return evidence


def _database_summary(evidence: DatabaseEvidence) -> dict[str, Any]:
    return {
        "application_id": evidence.application_id,
        "user_version": evidence.user_version,
        "auto_vacuum": evidence.auto_vacuum,
        "page_size": evidence.page_size,
        "page_count": evidence.page_count,
        "quick_check": evidence.quick_check,
        "integrity_check": evidence.integrity_check,
        "foreign_key_violations": evidence.foreign_key_violations,
        "canonical_sha256": evidence.canonical_sha256,
        "sqlite_sequences": dict(sorted(evidence.sqlite_sequences.items())),
        "tables": {
            name: {
                "row_count": table.row_count,
                "content_sha256": table.content_sha256,
            }
            for name, table in sorted(evidence.tables.items())
        },
    }


def _routing_summary(
    evidence: DatabaseEvidence,
    retain_research: bool,
) -> dict[str, dict[str, Any]]:
    canonical = canonical_runtime_evidence()
    result: dict[str, dict[str, Any]] = {}
    for name, route in sorted(TABLE_ROUTES.items()):
        source_table = evidence.tables.get(name)
        table = source_table or canonical.tables[name]
        result[name] = {
            "database": TABLE_ROUTES[name].database,
            "tier": TABLE_ROUTES[name].tier,
            "description": TABLE_ROUTES[name].description,
            "source_row_count": table.row_count,
            "source_content_sha256": table.content_sha256,
            # A missing legacy table has no rows to omit; its canonical empty
            # schema is retained in every published artifact.
            "retained": (
                source_table is None
                or TABLE_ROUTES[name].tier != "C"
                or retain_research
            ),
        }
    return result


def _assert_expected_source_schema(evidence: DatabaseEvidence) -> None:
    if evidence.application_id != APPLICATION_ID:
        raise SchemaRoutingError(
            f"source application_id is {evidence.application_id}, expected {APPLICATION_ID}"
        )
    if evidence.user_version != CURRENT_SCHEMA_VERSION:
        raise SchemaRoutingError(
            f"source user_version is {evidence.user_version}, expected {CURRENT_SCHEMA_VERSION}"
        )


def _assert_canonical_backup_match(
    source: DatabaseEvidence,
    backup: DatabaseEvidence,
) -> None:
    if source.canonical_sha256 != backup.canonical_sha256:
        raise MigrationError(
            "verified backup does not canonically match the quiescent source: "
            f"source={source.canonical_sha256}, backup={backup.canonical_sha256}"
        )


def _verify_manifest_source_name(verified: BackupResult, source: Path) -> None:
    if verified.manifest.source_name != source.name:
        raise MigrationError(
            "backup manifest source name does not match the requested source: "
            f"manifest={verified.manifest.source_name!r}, source={source.name!r}"
        )


def preflight_migration(
    source_db_path: str | os.PathLike[str],
    backup_manifest_path: str | os.PathLike[str],
    output_directory: str | os.PathLike[str],
    *,
    retain_research: bool = False,
    required_headroom_bytes: int = DEFAULT_REQUIRED_HEADROOM_BYTES,
    disk_usage_probe: DiskUsageProbe = shutil.disk_usage,
) -> MigrationPreflight:
    """Prove source, backup, schema, routing, and peak-space prerequisites.

    This function does not create the output directory or write any file.
    """

    source = _safe_regular_file(Path(source_db_path), label="source database")
    output_candidate = Path(output_directory).absolute()
    output_parent = _safe_output_parent(output_candidate)
    output = output_parent / output_candidate.name
    if _path_lexists(output):
        raise MigrationError(f"refusing to overwrite an existing output path: {output}")
    _assert_no_sidecars(source, label="source database")
    source_identity = _file_identity(source)

    manifest_candidate = _safe_regular_file(
        Path(backup_manifest_path),
        label="verified backup manifest",
    )
    manifest_identity_before = _file_identity(manifest_candidate)
    try:
        verified = verify_backup(manifest_candidate)
    except BackupError as exc:
        raise MigrationError(f"required backup is not independently verified: {exc}") from exc
    backup_manifest_identity = _file_identity(verified.manifest_path)
    if manifest_identity_before != backup_manifest_identity:
        raise MigrationError("backup manifest changed during independent verification")
    _verify_manifest_source_name(verified, source)
    backup = _safe_regular_file(verified.backup_path, label="verified backup database")
    try:
        same_file = os.path.samefile(source, backup)
    except OSError as exc:
        raise MigrationError(f"cannot prove source/backup independence: {exc}") from exc
    if same_file:
        raise MigrationError("verified backup must be physically independent from the source")
    _assert_no_sidecars(backup, label="verified backup database")
    backup_identity = _file_identity(backup)

    try:
        with _ReadOnlyDatabase(source) as source_connection:
            source_evidence = _inspect_database(source_connection)
        with _ReadOnlyDatabase(backup) as backup_connection:
            backup_evidence = _inspect_database(backup_connection)
    except sqlite3.Error as exc:
        raise MigrationError(f"SQLite validation failed: {exc}") from exc

    _assert_expected_source_schema(source_evidence)
    _assert_canonical_backup_match(source_evidence, backup_evidence)
    if source_identity != _file_identity(source):
        raise SourceNotQuiescentError("source database changed during preflight")
    if backup_identity != _file_identity(backup):
        raise MigrationError("verified backup changed during preflight")
    if backup_manifest_identity != _file_identity(verified.manifest_path):
        raise MigrationError("verified backup manifest changed during preflight")
    _assert_no_sidecars(source, label="source database")
    _assert_no_sidecars(backup, label="verified backup database")

    # A full-size logical output plus a 5%/16 MB SQLite build margin is a
    # conservative upper bound even when Tier-C rows are omitted.
    logical_source_bytes = max(
        source_identity.size_bytes,
        source_evidence.page_count * source_evidence.page_size,
    )
    build_margin = max(16_000_000, logical_source_bytes // 20)
    estimated_operation_bytes = logical_source_bytes + build_margin
    required_headroom = max(0, int(required_headroom_bytes))
    required_free = estimated_operation_bytes + required_headroom
    try:
        free = int(disk_usage_probe(output_parent).free)
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        raise MigrationError(f"cannot determine output-volume free space: {exc}") from exc
    if free < required_free:
        raise MigrationError(
            "insufficient peak space for offline migration: "
            f"free={free}, required={required_free} "
            f"(operation={estimated_operation_bytes}, headroom={required_headroom})"
        )

    return MigrationPreflight(
        source_path=source,
        backup_path=backup,
        backup_manifest_path=verified.manifest_path,
        output_directory=output,
        retain_research=bool(retain_research),
        required_headroom_bytes=required_headroom,
        estimated_operation_bytes=estimated_operation_bytes,
        required_free_bytes=required_free,
        observed_free_bytes=free,
        source_identity=source_identity,
        backup_identity=backup_identity,
        backup_manifest_identity=backup_manifest_identity,
        source_evidence=source_evidence,
    )


def _tables_for_destination(
    evidence: DatabaseEvidence,
    destination: str,
) -> tuple[TableEvidence, ...]:
    return tuple(
        evidence.tables[name]
        for name in sorted(evidence.tables)
        if TABLE_ROUTES[name].database == destination
    )


def _objects_for_destination(
    evidence: DatabaseEvidence,
    destination: str,
) -> tuple[SchemaObject, ...]:
    return tuple(
        item
        for item in evidence.schema_objects
        if TABLE_ROUTES[item.table_name].database == destination
    )


def _copy_table_rows(
    source: sqlite3.Connection,
    destination: sqlite3.Connection,
    table: TableEvidence,
) -> None:
    columns_sql = ", ".join(_quote_identifier(item) for item in table.columns)
    placeholders = ", ".join("?" for _ in table.columns)
    insert_sql = (
        f"INSERT INTO {_quote_identifier(table.name)} ({columns_sql}) "
        f"VALUES ({placeholders})"
    )
    cursor = _ordered_rows(source, table.name, table.columns, table.order_by)
    copied = 0
    while rows := cursor.fetchmany(COPY_BATCH_ROWS):
        destination.executemany(insert_sql, rows)
        copied += len(rows)
    if copied != table.row_count:
        raise SourceNotQuiescentError(
            f"source row count changed while copying {table.name}: "
            f"expected={table.row_count}, copied={copied}"
        )


def _copy_sqlite_sequences(
    source: sqlite3.Connection,
    destination: sqlite3.Connection,
    table_names: set[str],
) -> None:
    source_has_sequence = source.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'"
    ).fetchone()
    destination_has_sequence = destination.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'"
    ).fetchone()
    if source_has_sequence is None or destination_has_sequence is None:
        return
    rows = source.execute("SELECT name, seq FROM sqlite_sequence ORDER BY name").fetchall()
    destination.execute("DELETE FROM sqlite_sequence")
    destination.executemany(
        "INSERT INTO sqlite_sequence(name, seq) VALUES (?, ?)",
        ((str(name), int(sequence)) for name, sequence in rows if str(name) in table_names),
    )


def _build_destination(
    *,
    source: sqlite3.Connection,
    path: Path,
    database_name: str,
    source_evidence: DatabaseEvidence,
    retain_research: bool,
    fault_injector: FaultInjector | None,
) -> None:
    tables = _tables_for_destination(source_evidence, database_name)
    canonical = canonical_runtime_evidence()
    expected_names = sorted(
        name for name, route in TABLE_ROUTES.items() if route.database == database_name
    )
    for table in tables:
        canonical_table = canonical.tables[table.name]
        if set(table.columns) != set(canonical_table.columns):
            missing = sorted(set(canonical_table.columns) - set(table.columns))
            unexpected = sorted(set(table.columns) - set(canonical_table.columns))
            raise SchemaRoutingError(
                f"{table.name} columns do not match the current runtime schema: "
                f"missing={missing}, unexpected={unexpected}, "
                f"source={list(table.columns)}, current={list(canonical_table.columns)}"
            )
    objects = tuple(
        item
        for item in canonical.schema_objects
        if TABLE_ROUTES[item.table_name].database == database_name
    )
    synchronous = "NORMAL" if database_name == "research.db" else "FULL"
    with closing(sqlite3.connect(path, timeout=30)) as destination:
        destination.execute(f"PRAGMA page_size={source_evidence.page_size}")
        destination.execute("PRAGMA auto_vacuum=INCREMENTAL")
        destination.execute(f"PRAGMA application_id={source_evidence.application_id}")
        destination.execute(f"PRAGMA user_version={source_evidence.user_version}")
        destination.execute("PRAGMA journal_mode=DELETE")
        destination.execute(f"PRAGMA synchronous={synchronous}")
        destination.execute("PRAGMA foreign_keys=OFF")
        destination.execute("PRAGMA temp_store=MEMORY")
        destination.execute("BEGIN IMMEDIATE")
        try:
            for table_name in expected_names:
                destination.execute(canonical.tables[table_name].create_sql)
            for table in tables:
                should_copy = retain_research or TABLE_ROUTES[table.name].tier != "C"
                if should_copy:
                    _copy_table_rows(source, destination, table)
                if fault_injector is not None:
                    fault_injector(f"after_table:{table.name}")
            _copy_sqlite_sequences(source, destination, {table.name for table in tables})
            for item in objects:
                destination.execute(item.sql)
            destination.commit()
        except Exception:
            destination.rollback()
            raise
        destination.execute("PRAGMA foreign_keys=ON")
    _fsync_file(path)


def _assert_destination_matches(
    *,
    database_name: str,
    source: DatabaseEvidence,
    destination: DatabaseEvidence,
    retain_research: bool,
) -> None:
    canonical = canonical_runtime_evidence()
    expected_names = {
        name for name, route in TABLE_ROUTES.items() if route.database == database_name
    }
    if set(destination.tables) != expected_names:
        raise MigrationError(
            f"{database_name} table routing mismatch: "
            f"expected={sorted(expected_names)}, actual={sorted(destination.tables)}"
        )
    if destination.application_id != source.application_id:
        raise MigrationError(f"{database_name} application_id mismatch")
    if destination.user_version != source.user_version:
        raise MigrationError(f"{database_name} user_version mismatch")
    if destination.auto_vacuum != 2:
        raise MigrationError(
            f"{database_name} did not convert to auto_vacuum=INCREMENTAL"
        )
    for name in sorted(expected_names):
        source_table = source.tables.get(name)
        destination_table = destination.tables[name]
        canonical_table = canonical.tables[name]
        if destination_table.create_sql != canonical_table.create_sql:
            raise MigrationError(
                f"{database_name}.{name} does not use the canonical runtime schema"
            )
        if source_table is None:
            if destination_table.row_count != 0:
                raise MigrationError(
                    f"new canonical table {database_name}.{name} is not empty"
                )
            continue
        retained = retain_research or TABLE_ROUTES[name].tier != "C"
        if retained:
            if (
                destination_table.row_count != source_table.row_count
                or destination_table.routed_content_sha256
                != source_table.routed_content_sha256
            ):
                raise MigrationError(
                    f"{database_name}.{name} retained row count/content hash mismatch"
                )
        elif destination_table.row_count != 0:
            raise MigrationError(f"omitted Tier-C table {database_name}.{name} is not empty")

    expected_objects = tuple(
        item
        for item in canonical.schema_objects
        if TABLE_ROUTES[item.table_name].database == database_name
    )
    if destination.schema_objects != expected_objects:
        raise MigrationError(f"{database_name} indexes/triggers do not match the source")
    expected_sequences = {
        name: sequence
        for name, sequence in source.sqlite_sequences.items()
        if TABLE_ROUTES[name].database == database_name
    }
    if dict(destination.sqlite_sequences) != expected_sequences:
        raise MigrationError(f"{database_name} sqlite_sequence watermarks do not match")


def _atomic_json_write(path: Path, payload: Mapping[str, Any]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.rename(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _destination_manifest_entry(
    path: Path,
    evidence: DatabaseEvidence,
) -> dict[str, Any]:
    result = _database_summary(evidence)
    result.update(
        {
            "filename": path.name,
            "size_bytes": path.stat().st_size,
            "file_sha256": _sha256_file(path),
        }
    )
    return result


def _build_manifest(
    *,
    preflight: MigrationPreflight,
    destination_evidence: Mapping[str, DatabaseEvidence],
    destination_paths: Mapping[str, Path],
) -> dict[str, Any]:
    routes = _routing_summary(preflight.source_evidence, preflight.retain_research)
    omissions = [
        {
            "table": name,
            "tier": route["tier"],
            "source_row_count": route["source_row_count"],
            "source_content_sha256": route["source_content_sha256"],
            "reason": "legacy reproducible Tier-C evidence retained in verified backup",
            "authoritative_backup_sha256": preflight.backup_identity.sha256,
        }
        for name, route in routes.items()
        if not bool(route["retained"])
    ]
    payload: dict[str, Any] = {
        "format": MIGRATION_FORMAT,
        "created_at": _utc_now(),
        "publication": {
            "method": "sibling staging directory plus atomic no-overwrite rename",
            "source_was_modified": False,
            "source_was_deleted_or_renamed": False,
        },
        "source": {
            "path": str(preflight.source_path),
            "file_identity": asdict(preflight.source_identity),
            **_database_summary(preflight.source_evidence),
        },
        "authoritative_backup": {
            "path": str(preflight.backup_path),
            "manifest_path": str(preflight.backup_manifest_path),
            "file_identity": asdict(preflight.backup_identity),
            "manifest_file_identity": asdict(preflight.backup_manifest_identity),
            "canonical_source_match": True,
            "canonical_sha256": preflight.source_evidence.canonical_sha256,
        },
        "space_preflight": {
            "estimated_operation_bytes": preflight.estimated_operation_bytes,
            "required_headroom_bytes": preflight.required_headroom_bytes,
            "required_free_bytes": preflight.required_free_bytes,
            "observed_free_bytes": preflight.observed_free_bytes,
        },
        "policy": {
            "retain_research": preflight.retain_research,
            "tier_c_omission_requires_authoritative_backup": True,
        },
        "routes": routes,
        "omissions": omissions,
        "destinations": {
            name: _destination_manifest_entry(destination_paths[name], destination_evidence[name])
            for name in DESTINATION_NAMES
        },
        "validation": {
            "source_stable_before_and_after": True,
            "backup_stable_before_and_after": True,
            "all_retained_table_counts_and_hashes_match": True,
            "all_database_quick_checks": "ok",
            "all_database_integrity_checks": "ok",
            "all_foreign_key_violation_counts": 0,
            "all_destination_auto_vacuum": "INCREMENTAL",
        },
    }
    payload["manifest_sha256"] = _manifest_digest(payload)
    return payload


def verify_published_migration(
    output_directory: str | os.PathLike[str],
    *,
    verify_destination_hashes: bool = True,
) -> PublishedMigrationVerification:
    """Verify one atomically published migration before runtime activation.

    The first activation verifies the immutable file identities and complete
    schema/content evidence recorded by the manifest.  Once runtime writes are
    enabled those identities necessarily change, so restarts set
    ``verify_destination_hashes=False`` and use the persisted activation marker
    to bind the still-validated manifest identity to exact live role schemas.
    """

    candidate = Path(output_directory)
    if not candidate.is_absolute():
        raise MigrationError("published migration directory must be an absolute path")
    try:
        directory_metadata = candidate.lstat()
    except OSError as exc:
        raise MigrationError(
            f"published migration directory does not exist: {candidate}"
        ) from exc
    if _is_link_or_reparse(candidate, directory_metadata) or not stat.S_ISDIR(
        directory_metadata.st_mode
    ):
        raise MigrationError(
            "published migration directory must be a regular non-link/reparse directory"
        )
    directory = candidate.resolve(strict=True)
    manifest_path = _safe_regular_file(
        directory / MANIFEST_FILENAME,
        label="published migration manifest",
    )
    if manifest_path.parent != directory:
        raise MigrationError("published migration manifest escapes its directory")
    manifest_identity_before = _file_identity(manifest_path)
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MigrationError(f"published migration manifest is unreadable: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise MigrationError("published migration manifest is not a JSON object")
    if str(payload.get("format", "")) != MIGRATION_FORMAT:
        raise MigrationError("published migration manifest format is unsupported")
    manifest_sha256 = str(payload.get("manifest_sha256", "")).lower()
    if (
        len(manifest_sha256) != 64
        or any(character not in "0123456789abcdef" for character in manifest_sha256)
        or _manifest_digest(payload) != manifest_sha256
    ):
        raise MigrationError("published migration manifest hash is invalid")

    destinations = payload.get("destinations")
    if not isinstance(destinations, Mapping) or set(destinations) != set(
        DESTINATION_NAMES
    ):
        raise MigrationError(
            "published migration manifest must bind exactly state.db, audit.db, and research.db"
        )

    destination_paths: dict[str, Path] = {}
    identities_before: dict[str, FileIdentity] = {}
    for database_name in DESTINATION_NAMES:
        entry = destinations.get(database_name)
        if not isinstance(entry, Mapping):
            raise MigrationError(
                f"published migration manifest lacks {database_name} evidence"
            )
        if str(entry.get("filename", "")) != database_name:
            raise MigrationError(
                f"published migration manifest has unsafe filename for {database_name}"
            )
        path = _safe_regular_file(
            directory / database_name,
            label=f"published {database_name}",
        )
        if path.parent != directory or path.name != database_name:
            raise MigrationError(f"published {database_name} escapes its directory")
        destination_paths[database_name] = path

        expected_tables = {
            name for name, route in TABLE_ROUTES.items() if route.database == database_name
        }
        table_evidence = entry.get("tables")
        if not isinstance(table_evidence, Mapping) or set(table_evidence) != expected_tables:
            raise MigrationError(
                f"published migration manifest has invalid table routing for {database_name}"
            )
        if int(entry.get("application_id", -1)) != APPLICATION_ID:
            raise MigrationError(
                f"published migration manifest has invalid application_id for {database_name}"
            )
        if int(entry.get("user_version", -1)) != CURRENT_SCHEMA_VERSION:
            raise MigrationError(
                f"published migration manifest has invalid user_version for {database_name}"
            )
        if int(entry.get("auto_vacuum", -1)) != 2:
            raise MigrationError(
                f"published migration manifest has invalid auto_vacuum for {database_name}"
            )

        if not verify_destination_hashes:
            continue
        _assert_no_sidecars(path, label=f"published {database_name}")
        identity = _file_identity(path)
        identities_before[database_name] = identity
        if int(entry.get("size_bytes", -1)) != identity.size_bytes:
            raise MigrationError(f"published {database_name} size does not match manifest")
        if str(entry.get("file_sha256", "")).lower() != identity.sha256:
            raise MigrationError(f"published {database_name} hash does not match manifest")
        try:
            with _ReadOnlyDatabase(path) as connection:
                evidence = _inspect_database(
                    connection,
                    require_current_source_schema=False,
                )
        except sqlite3.Error as exc:
            raise MigrationError(
                f"published {database_name} SQLite validation failed: {exc}"
            ) from exc
        expected_summary = {
            key: value
            for key, value in entry.items()
            if key not in {"filename", "size_bytes", "file_sha256"}
        }
        if _database_summary(evidence) != expected_summary:
            raise MigrationError(
                f"published {database_name} schema/content evidence does not match manifest"
            )

    if verify_destination_hashes:
        for database_name, path in destination_paths.items():
            if _file_identity(path) != identities_before[database_name]:
                raise SourceNotQuiescentError(
                    f"published {database_name} changed during activation verification"
                )
    if _file_identity(manifest_path) != manifest_identity_before:
        raise SourceNotQuiescentError(
            "published migration manifest changed during activation verification"
        )
    return PublishedMigrationVerification(
        output_directory=directory,
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha256,
        destination_paths=destination_paths,
    )


def _publish_no_overwrite(staging: Path, output: Path) -> None:
    if _path_lexists(output):
        raise MigrationError(f"refusing to overwrite an existing output path: {output}")
    if os.name == "nt":
        try:
            # Windows rename is atomic and refuses every existing destination.
            os.rename(staging, output)
        except FileExistsError as exc:
            raise MigrationError(
                f"output path appeared during publication: {output}"
            ) from exc
    elif sys.platform.startswith("linux"):
        # Plain POSIX rename may replace an empty destination directory between
        # the check above and publication.  Linux renameat2 closes that race.
        try:
            renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
        except AttributeError as exc:  # pragma: no cover - legacy libc only
            raise MigrationError(
                "atomic no-overwrite directory publication is unavailable"
            ) from exc
        renameat2.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        renameat2.restype = ctypes.c_int
        at_fdcwd = -100
        rename_noreplace = 1
        result = int(
            renameat2(
                at_fdcwd,
                os.fsencode(staging),
                at_fdcwd,
                os.fsencode(output),
                rename_noreplace,
            )
        )
        if result != 0:
            error_number = ctypes.get_errno()
            if error_number == errno.EEXIST:
                raise MigrationError(f"output path appeared during publication: {output}")
            raise MigrationError(
                "atomic no-overwrite publication failed: "
                f"{os.strerror(error_number)}"
            )
    else:  # pragma: no cover - Bongus production is Windows; fail closed elsewhere.
        raise MigrationError(
            "atomic no-overwrite directory publication is unsupported on this platform"
        )
    _fsync_directory(output.parent)


def execute_migration(
    source_db_path: str | os.PathLike[str],
    backup_manifest_path: str | os.PathLike[str],
    output_directory: str | os.PathLike[str],
    *,
    retain_research: bool = False,
    required_headroom_bytes: int = DEFAULT_REQUIRED_HEADROOM_BYTES,
    disk_usage_probe: DiskUsageProbe = shutil.disk_usage,
    fault_injector: FaultInjector | None = None,
) -> MigrationResult:
    """Build, verify, and atomically publish a one-time split migration."""

    preflight = preflight_migration(
        source_db_path,
        backup_manifest_path,
        output_directory,
        retain_research=retain_research,
        required_headroom_bytes=required_headroom_bytes,
        disk_usage_probe=disk_usage_probe,
    )
    output_parent = preflight.output_directory.parent
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{preflight.output_directory.name}.migration-",
            dir=output_parent,
        )
    )
    published = False
    try:
        if fault_injector is not None:
            fault_injector("after_staging_created")
        _assert_no_sidecars(preflight.source_path, label="source database")
        if _file_identity(preflight.source_path) != preflight.source_identity:
            raise SourceNotQuiescentError("source database changed after preflight")

        destination_paths = {name: staging / name for name in DESTINATION_NAMES}
        try:
            with _ReadOnlyDatabase(preflight.source_path) as source_connection:
                for name in DESTINATION_NAMES:
                    _build_destination(
                        source=source_connection,
                        path=destination_paths[name],
                        database_name=name,
                        source_evidence=preflight.source_evidence,
                        retain_research=preflight.retain_research,
                        fault_injector=fault_injector,
                    )
        except sqlite3.Error as exc:
            raise MigrationError(f"SQLite migration copy failed: {exc}") from exc

        destination_evidence: dict[str, DatabaseEvidence] = {}
        for name in DESTINATION_NAMES:
            path = destination_paths[name]
            _assert_no_sidecars(path, label=f"staged {name}")
            try:
                with _ReadOnlyDatabase(path) as connection:
                    evidence = _inspect_database(
                        connection,
                        require_current_source_schema=False,
                    )
            except sqlite3.Error as exc:
                raise MigrationError(f"staged {name} validation failed: {exc}") from exc
            _assert_destination_matches(
                database_name=name,
                source=preflight.source_evidence,
                destination=evidence,
                retain_research=preflight.retain_research,
            )
            destination_evidence[name] = evidence
            _fsync_file(path)

        if fault_injector is not None:
            fault_injector("before_final_source_check")
        _assert_no_sidecars(preflight.source_path, label="source database")
        if _file_identity(preflight.source_path) != preflight.source_identity:
            raise SourceNotQuiescentError("source database changed during migration")
        _assert_no_sidecars(preflight.backup_path, label="verified backup database")
        if _file_identity(preflight.backup_path) != preflight.backup_identity:
            raise MigrationError("authoritative backup changed during migration")
        if (
            _file_identity(preflight.backup_manifest_path)
            != preflight.backup_manifest_identity
        ):
            raise MigrationError("authoritative backup manifest changed during migration")

        manifest = _build_manifest(
            preflight=preflight,
            destination_evidence=destination_evidence,
            destination_paths=destination_paths,
        )
        manifest_path = staging / MANIFEST_FILENAME
        _atomic_json_write(manifest_path, manifest)
        _fsync_file(manifest_path)
        _fsync_directory(staging)
        if fault_injector is not None:
            fault_injector("before_publish")
        _publish_no_overwrite(staging, preflight.output_directory)
        published = True

        final_manifest = preflight.output_directory / MANIFEST_FILENAME
        final_payload = json.loads(final_manifest.read_text(encoding="utf-8"))
        if not isinstance(final_payload, Mapping):
            raise MigrationError("published migration manifest is not a JSON object")
        expected_digest = str(final_payload.get("manifest_sha256", ""))
        if not expected_digest or _manifest_digest(final_payload) != expected_digest:
            raise MigrationError("published migration manifest hash is invalid")
        return MigrationResult(
            output_directory=preflight.output_directory,
            manifest_path=final_manifest,
            manifest_sha256=expected_digest,
            destination_paths={
                name: preflight.output_directory / name for name in DESTINATION_NAMES
            },
        )
    finally:
        if not published and staging.exists():
            # ``staging`` is the exact private directory returned by mkdtemp;
            # never derive a cleanup target from source or output contents.
            shutil.rmtree(staging)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Offline, read-only-source migration from the legacy state.db into "
            "state.db, audit.db, and research.db. Stop all Bongus processes first."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("dry-run", "execute"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--source", type=Path, required=True)
        subparser.add_argument("--backup-manifest", type=Path, required=True)
        subparser.add_argument("--output", type=Path, required=True)
        subparser.add_argument(
            "--retain-research",
            action="store_true",
            help="copy legacy Tier-C rows instead of retaining them only in the verified backup",
        )
        subparser.add_argument(
            "--required-headroom-bytes",
            type=int,
            default=DEFAULT_REQUIRED_HEADROOM_BYTES,
        )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "dry-run":
            report = preflight_migration(
                args.source,
                args.backup_manifest,
                args.output,
                retain_research=bool(args.retain_research),
                required_headroom_bytes=int(args.required_headroom_bytes),
            )
            payload = report.to_dict()
        else:
            result = execute_migration(
                args.source,
                args.backup_manifest,
                args.output,
                retain_research=bool(args.retain_research),
                required_headroom_bytes=int(args.required_headroom_bytes),
            )
            payload = result.to_dict()
    except (MigrationError, OSError, sqlite3.Error) as exc:
        print(f"offline migration refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through module CLI tests
    raise SystemExit(main())
