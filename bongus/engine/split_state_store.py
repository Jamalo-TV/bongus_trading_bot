"""Role-separated runtime persistence for state, audit, and research data.

The legacy :class:`~bongus.engine.state_store.StateWriter` deliberately remains
the single-file implementation used by embedded callers and tests.  Production
uses the classes in this module: each SQLite file is initialized with only the
tables assigned to its storage role, and method calls are routed to the owning
file.

Cross-file operations use an evidence-first ordering.  Immutable audit evidence
is committed before the mutable state projection.  A crash can therefore leave
a projection that needs replay, but can never leave an undocumented state
transition.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
from typing import Any, Final
from urllib.parse import quote

from bongus.engine.exchange_statements import (
    ExchangeStatementIngestionResult,
    NormalizedExchangeStatement,
    ingest_exchange_statement,
    normalize_binance_futures_income,
    normalize_binance_margin_interest,
)
from bongus.engine.offline_storage_migration import (
    MANIFEST_FILENAME,
    MigrationError,
    TABLE_ROUTES,
    canonical_runtime_evidence,
    verify_published_migration,
)
from bongus.engine.state_store import (
    APPLICATION_ID,
    CURRENT_SCHEMA_VERSION,
    LifecycleRebuildError,
    StateReader,
    StateWriter,
    Trade,
    DEFAULT_STATE_DB_MAX_BYTES,
    DEFAULT_WAL_JOURNAL_LIMIT_BYTES,
    _now,
    _prepare_binance_futures_income_statement,
)


class SplitStoreError(RuntimeError):
    """Raised when a database cannot be proven to have exactly one role."""


ROLE_NAMES: Final = ("state.db", "audit.db", "research.db")
_AUDIT_MAX_BYTES: Final = 1_500_000_000
_RESEARCH_MAX_BYTES: Final = 4_000_000_000
_REPARSE_POINT_ATTRIBUTE: Final = 0x0400
_ACTIVATION_MODE_KEY: Final = "split_store_activation_mode"
_ACTIVATION_IDENTITY_KEY: Final = "split_store_activation_identity"
_MIGRATION_ACTIVATION_MODE: Final = "migration-manifest-v1"
_FRESH_ACTIVATION_MODE: Final = "fresh-split-v1"
_RESEARCH_HOURLY_RETENTION_DAYS: Final = 90
SPLIT_ROLE_DATABASE_MAX_BYTES: Final = {
    "state.db": DEFAULT_STATE_DB_MAX_BYTES,
    "audit.db": _AUDIT_MAX_BYTES,
    "research.db": _RESEARCH_MAX_BYTES,
}
SPLIT_ROLE_WAL_MAX_BYTES: Final = DEFAULT_WAL_JOURNAL_LIMIT_BYTES


def _fresh_activation_identity() -> str:
    payload = {
        "mode": _FRESH_ACTIVATION_MODE,
        "application_id": APPLICATION_ID,
        "schema_version": CURRENT_SCHEMA_VERSION,
        "routes": {
            name: route.database for name, route in sorted(TABLE_ROUTES.items())
        },
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


_FRESH_ACTIVATION_IDENTITY: Final = _fresh_activation_identity()

_AUDIT_WRITER_METHODS: Final = frozenset(
    {
        "clear_trade_history",
        "clear_execution_events",
        "record_trade",
        "record_execution_decision",
        "record_validation_snapshot",
        "record_execution_event",
        "record_execution_tca",
        "record_execution_tca_ack",
        "record_execution_tca_fill",
        "record_execution_tca_terminal",
        "record_execution_tca_markout",
        "record_opportunity_funnel_event",
        "record_execution_and_economic_fill",
        "record_execution_and_economic_funding",
        "record_execution_and_economic_events",
        "record_economic_events",
        "record_economic_fill",
        "record_economic_commission",
        "record_economic_funding",
        "record_economic_realized_pnl",
        "record_economic_borrow_interest",
        "record_economic_balance_adjustment",
        "record_health_sample",
    }
)

_RESEARCH_WRITER_METHODS: Final = frozenset(
    {
        "record_candidate_snapshots",
        "record_opportunity_scores",
        "record_feature_snapshot",
        "record_feature_snapshots",
        "record_execution_quality",
        "record_shadow_decision",
        "upsert_market_sample",
        "record_market_sample",
    }
)

_AUDIT_READER_METHODS: Final = frozenset(
    {
        "get_trades",
        "get_pnl_attribution",
        "get_validation_snapshots",
        "get_execution_events",
        "get_execution_decision",
        "get_execution_tca",
        "get_latest_execution_tca_intent",
        "get_opportunity_funnel_events",
        "summarize_opportunity_funnel",
        "get_economic_ledger_events",
        "project_economic_ledger",
        "reconcile_economic_ledger",
        "get_exchange_statement_entries",
        "get_execution_events_since",
        "get_health_samples",
        "get_latest_validation_snapshot",
        "estimate_trade_execution_cost",
        "get_trade_execution_cost_evidence",
        "get_trade_economic_cashflows",
        "get_trade_funding_cashflows",
        "get_partial_exit_lifecycle_events",
    }
)

_RESEARCH_READER_METHODS: Final = frozenset(
    {
        "get_candidate_snapshots",
        "get_opportunity_scores",
        "get_feature_snapshots",
        "get_execution_quality",
        "get_shadow_decisions",
        "get_candidate_snapshot",
        "has_execution_quality_sample",
        "get_market_samples",
        "get_market_hourly_aggregates",
    }
)


def _canonical_schema() -> tuple[dict[str, str], tuple[tuple[str, str, str], ...]]:
    evidence = canonical_runtime_evidence()
    tables = {
        name: table.create_sql for name, table in evidence.tables.items()
    }
    objects = tuple(
        (item.object_type, item.table_name, item.sql)
        for item in evidence.schema_objects
    )
    return tables, objects


def _expected_tables(role: str) -> set[str]:
    if role not in ROLE_NAMES:
        raise ValueError(f"unsupported storage role: {role!r}")
    return {
        table_name
        for table_name, route in TABLE_ROUTES.items()
        if route.database == role
    }


def _validate_role_schema(connection: sqlite3.Connection, role: str) -> None:
    expected = _expected_tables(role)
    rows = connection.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    actual = {str(row[0]) for row in rows}
    if actual != expected:
        raise SplitStoreError(
            f"{role} schema routing mismatch: "
            f"missing={sorted(expected - actual)}, extra={sorted(actual - expected)}; "
            "run the verified offline storage migration before activation"
        )
    canonical_tables, canonical_objects = _canonical_schema()
    actual_table_sql = {str(row[0]): str(row[1]) for row in rows}
    schema_mismatches = [
        table_name
        for table_name in sorted(expected)
        if actual_table_sql.get(table_name) != canonical_tables.get(table_name)
    ]
    if schema_mismatches:
        raise SplitStoreError(
            f"{role} table schemas differ from the canonical runtime schema: "
            f"{schema_mismatches}"
        )
    expected_objects = {
        (kind, table_name, sql)
        for kind, table_name, sql in canonical_objects
        if table_name in expected
    }
    actual_objects = {
        (str(row[0]), str(row[1]), str(row[2]))
        for row in connection.execute(
            "SELECT type, tbl_name, sql FROM sqlite_master "
            "WHERE type IN ('index', 'trigger') AND sql IS NOT NULL"
        ).fetchall()
    }
    if actual_objects != expected_objects:
        raise SplitStoreError(
            f"{role} indexes/triggers differ from the canonical runtime schema"
        )
    application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
    user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if application_id != APPLICATION_ID:
        raise SplitStoreError(
            f"{role} has unexpected application_id {application_id}"
        )
    if user_version != CURRENT_SCHEMA_VERSION:
        raise SplitStoreError(
            f"{role} schema version {user_version} is not {CURRENT_SCHEMA_VERSION}"
        )


def initialize_role_database(path: str | Path, role: str) -> None:
    """Create or validate one exact role database without touching other files."""

    database_path = Path(path).resolve()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    exists_with_content = database_path.is_file() and database_path.stat().st_size > 0
    connection = sqlite3.connect(database_path, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        try:
            if exists_with_content:
                _validate_role_schema(connection, role)
                return

            canonical_tables, canonical_objects = _canonical_schema()
            unknown = set(canonical_tables) - set(TABLE_ROUTES)
            missing = set(TABLE_ROUTES) - set(canonical_tables)
            if unknown or missing:
                raise SplitStoreError(
                    "storage table routing is not exhaustive: "
                    f"unclassified={sorted(unknown)}, unavailable={sorted(missing)}"
                )
            expected = _expected_tables(role)
            connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
            connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("BEGIN IMMEDIATE")
            try:
                for table_name in sorted(expected):
                    connection.execute(canonical_tables[table_name])
                for _kind, table_name, sql in canonical_objects:
                    if table_name in expected:
                        connection.execute(sql)
                if role == "state.db":
                    connection.execute(
                        "INSERT INTO schema_meta(key, value) VALUES ('schema_version', ?)",
                        (str(CURRENT_SCHEMA_VERSION),),
                    )
                connection.execute(f"PRAGMA user_version={CURRENT_SCHEMA_VERSION}")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.execute("PRAGMA foreign_keys=ON")
            _validate_role_schema(connection, role)
        finally:
            # Existing role databases return after validation.  Keep closure in
            # a finally block so that fast path cannot leave a Windows file
            # handle dependent on cyclic garbage collection.
            connection.close()
    except Exception:
        if not exists_with_content:
            for candidate in (
                database_path,
                Path(f"{database_path}-wal"),
                Path(f"{database_path}-shm"),
            ):
                try:
                    candidate.unlink(missing_ok=True)
                except OSError:
                    pass
        raise


def _path_lexists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise SplitStoreError(f"cannot inspect split-store path {path}: {exc}") from exc
    return True


def _is_link_or_reparse(path: Path, metadata: os.stat_result) -> bool:
    return path.is_symlink() or bool(
        getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT_ATTRIBUTE
    )


def _normalize_role_paths(
    *,
    state_path: str,
    audit_path: str,
    research_path: str,
    create_root: bool,
) -> tuple[Path, dict[str, Path]]:
    supplied = {
        "state.db": Path(state_path).expanduser(),
        "audit.db": Path(audit_path).expanduser(),
        "research.db": Path(research_path).expanduser(),
    }
    for role, candidate in supplied.items():
        if not candidate.is_absolute():
            raise SplitStoreError(f"{role} path must be absolute")
        if candidate.name != role:
            raise SplitStoreError(
                f"{role} must use the fixed filename {role!r} inside one common data root"
            )

    resolved_parents = {
        candidate.parent.resolve(strict=False) for candidate in supplied.values()
    }
    if len(resolved_parents) != 1:
        raise SplitStoreError(
            "state.db, audit.db, and research.db must share one common data root"
        )
    root = resolved_parents.pop()
    if not root.exists():
        if not create_root:
            raise SplitStoreError(f"split-store data root does not exist: {root}")
        root.mkdir(parents=True, exist_ok=True)
    try:
        root_metadata = root.lstat()
    except OSError as exc:
        raise SplitStoreError(f"split-store data root is unavailable: {root}") from exc
    if _is_link_or_reparse(root, root_metadata) or not stat.S_ISDIR(
        root_metadata.st_mode
    ):
        raise SplitStoreError(
            "split-store data root must be a regular non-link/reparse directory"
        )
    root = root.resolve(strict=True)

    normalized: dict[str, Path] = {}
    for role, candidate in supplied.items():
        resolved = candidate.resolve(strict=False)
        if resolved != root / role:
            raise SplitStoreError(f"{role} path escapes the common data root")
        if _path_lexists(candidate):
            metadata = candidate.lstat()
            if _is_link_or_reparse(candidate, metadata) or not stat.S_ISREG(
                metadata.st_mode
            ):
                raise SplitStoreError(
                    f"{role} must be a regular non-link/reparse file"
                )
        normalized[role] = root / role
    return root, normalized


def _readonly_role_connection(
    path: Path,
    *,
    immutable: bool,
) -> sqlite3.Connection:
    encoded_path = quote(path.resolve(strict=True).as_posix(), safe="/:")
    immutable_query = "&immutable=1" if immutable else ""
    connection = sqlite3.connect(
        f"file:{encoded_path}?mode=ro{immutable_query}",
        uri=True,
        timeout=30,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def _validate_role_paths(
    paths: Mapping[str, Path],
    *,
    immutable: bool,
) -> None:
    for role in ROLE_NAMES:
        try:
            connection = _readonly_role_connection(paths[role], immutable=immutable)
            try:
                _validate_role_schema(connection, role)
            finally:
                connection.close()
        except (OSError, sqlite3.Error) as exc:
            raise SplitStoreError(f"could not validate {role}: {exc}") from exc


def _read_activation_marker(state_path: Path) -> tuple[str | None, str | None]:
    try:
        connection = _readonly_role_connection(state_path, immutable=True)
        try:
            rows = connection.execute(
                "SELECT key, value FROM schema_meta WHERE key IN (?, ?)",
                (_ACTIVATION_MODE_KEY, _ACTIVATION_IDENTITY_KEY),
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise SplitStoreError(f"could not read split-store activation marker: {exc}") from exc
    values = {str(row["key"]): str(row["value"]) for row in rows}
    mode = values.get(_ACTIVATION_MODE_KEY)
    identity = values.get(_ACTIVATION_IDENTITY_KEY)
    if (mode is None) != (identity is None):
        raise SplitStoreError("split-store activation marker is incomplete")
    return mode, identity


def _write_activation_marker(state_path: Path, *, mode: str, identity: str) -> None:
    connection = sqlite3.connect(state_path, timeout=30)
    try:
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("BEGIN IMMEDIATE")
        connection.executemany(
            "INSERT INTO schema_meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (
                (_ACTIVATION_MODE_KEY, mode),
                (_ACTIVATION_IDENTITY_KEY, identity),
            ),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    if _read_activation_marker(state_path) != (mode, identity):
        raise SplitStoreError("split-store activation marker was not durably persisted")


def _verified_manifest(
    root: Path,
    paths: Mapping[str, Path],
    *,
    verify_destination_hashes: bool,
) -> str:
    try:
        verification = verify_published_migration(
            root,
            verify_destination_hashes=verify_destination_hashes,
        )
    except MigrationError as exc:
        raise SplitStoreError(f"published storage migration is invalid: {exc}") from exc
    expected_paths = {role: paths[role] for role in ROLE_NAMES}
    if dict(verification.destination_paths) != expected_paths:
        raise SplitStoreError("published storage migration paths do not match runtime paths")
    return verification.manifest_sha256


def _prepare_split_store(
    *,
    state_path: str,
    audit_path: str,
    research_path: str,
    allow_initialize: bool,
) -> dict[str, Path]:
    root, paths = _normalize_role_paths(
        state_path=state_path,
        audit_path=audit_path,
        research_path=research_path,
        create_root=allow_initialize,
    )
    present = {role: _path_lexists(paths[role]) for role in ROLE_NAMES}
    manifest_path = root / MANIFEST_FILENAME
    manifest_present = _path_lexists(manifest_path)

    if any(present.values()) and not all(present.values()):
        raise SplitStoreError(
            "partial split-store trio is unsafe; run the verified offline storage "
            "migration before activation"
        )
    if not any(present.values()):
        if not allow_initialize:
            raise SplitStoreError("split-store databases do not exist")
        if manifest_present:
            raise SplitStoreError(
                "migration manifest exists without its complete database trio"
            )
        for role in ROLE_NAMES:
            initialize_role_database(paths[role], role)
        _validate_role_paths(paths, immutable=True)
        _write_activation_marker(
            paths["state.db"],
            mode=_FRESH_ACTIVATION_MODE,
            identity=_FRESH_ACTIVATION_IDENTITY,
        )
        return paths

    mode, identity = _read_activation_marker(paths["state.db"])
    if mode is None:
        if not allow_initialize:
            raise SplitStoreError(
                "published migration has not completed its stopped activation"
            )
        if not manifest_present:
            raise SplitStoreError(
                "existing unmarked split-store trio requires its migration manifest"
            )
        manifest_identity = _verified_manifest(
            root,
            paths,
            verify_destination_hashes=True,
        )
        _validate_role_paths(paths, immutable=True)
        _write_activation_marker(
            paths["state.db"],
            mode=_MIGRATION_ACTIVATION_MODE,
            identity=manifest_identity,
        )
        return paths

    if mode == _MIGRATION_ACTIVATION_MODE:
        if not manifest_present:
            raise SplitStoreError("activated migrated split store is missing its manifest")
        manifest_identity = _verified_manifest(
            root,
            paths,
            verify_destination_hashes=False,
        )
        if identity != manifest_identity:
            raise SplitStoreError("split-store activation marker does not match its manifest")
    elif mode == _FRESH_ACTIVATION_MODE:
        if manifest_present:
            raise SplitStoreError(
                "fresh split store cannot be rebound to a migration manifest"
            )
        if identity != _FRESH_ACTIVATION_IDENTITY:
            raise SplitStoreError("fresh split-store activation marker is invalid")
    else:
        raise SplitStoreError(f"unsupported split-store activation mode: {mode!r}")
    _validate_role_paths(paths, immutable=False)
    return paths


class _RoleWriter(StateWriter):
    def __init__(
        self,
        db_path: str,
        *,
        role: str,
        context_provider: Callable[[], dict[str, str]] | None = None,
    ) -> None:
        initialize_role_database(db_path, role)
        self.storage_role = role
        self._context_provider = context_provider
        super().__init__(
            db_path=db_path,
            migrate=False,
            synchronous="NORMAL" if role == "research.db" else "FULL",
            max_database_bytes=SPLIT_ROLE_DATABASE_MAX_BYTES[role],
        )

    def _runtime_context(self) -> dict[str, str]:
        if self._context_provider is not None:
            return self._context_provider()
        return super()._runtime_context()


class _RoleReader(StateReader):
    def __init__(
        self,
        db_path: str,
        *,
        role: str,
        scope_provider: Callable[[], dict[str, str]] | None = None,
        market_price_provider: Callable[[str], float] | None = None,
    ) -> None:
        self.storage_role = role
        self._scope_provider = scope_provider
        self._market_price_provider = market_price_provider
        super().__init__(db_path=db_path)
        _validate_role_schema(self.conn, role)

    def _current_scope(self) -> dict[str, str]:
        if self._scope_provider is not None:
            return self._scope_provider()
        return super()._current_scope()

    def _latest_market_price(self, symbol: str) -> float:
        if self._market_price_provider is not None:
            return self._market_price_provider(symbol)
        return super()._latest_market_price(symbol)


class SplitStateWriter(StateWriter):
    """Route the StateWriter API across exact state/audit/research stores."""

    def __init__(self, *, state_path: str, audit_path: str, research_path: str) -> None:
        paths = _prepare_split_store(
            state_path=state_path,
            audit_path=audit_path,
            research_path=research_path,
            allow_initialize=True,
        )
        self.state = _RoleWriter(str(paths["state.db"]), role="state.db")
        try:
            self.audit = _RoleWriter(
                str(paths["audit.db"]),
                role="audit.db",
                context_provider=self.state._runtime_context,
            )
            try:
                self.research = _RoleWriter(
                    str(paths["research.db"]),
                    role="research.db",
                    context_provider=self.state._runtime_context,
                )
            except Exception:
                self.audit.close()
                raise
        except Exception:
            self.state.close()
            raise

    @property
    def conn(self) -> sqlite3.Connection:
        return self.state.conn

    @property
    def _guard_lock(self):  # type: ignore[no-untyped-def]
        return self.state._guard_lock

    @property
    def _cooldown_conn(self) -> sqlite3.Connection:
        return self.state._cooldown_conn

    @property
    def _feed_recovery_conn(self) -> sqlite3.Connection:
        return self.state._feed_recovery_conn

    @property
    def _feed_recovery_event_conn(self) -> sqlite3.Connection:
        return self.audit._feed_recovery_conn

    def __getattribute__(self, name: str) -> Any:
        if name in _AUDIT_WRITER_METHODS:
            return getattr(object.__getattribute__(self, "audit"), name)
        if name in _RESEARCH_WRITER_METHODS:
            return getattr(object.__getattribute__(self, "research"), name)
        return super().__getattribute__(name)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.state, name)

    def flush(self) -> None:
        # Immutable evidence is made durable before mutable state projections.
        self.audit.flush()
        self.state.flush()
        # Research is optional and commits last.  Its failure may be surfaced to
        # the caller, but cannot strand a critical hot-state commit behind it.
        self.research.flush()

    def record_exchange_statement(
        self,
        statement: NormalizedExchangeStatement,
    ) -> ExchangeStatementIngestionResult:
        with self.audit._exchange_statement_lock:
            result = ingest_exchange_statement(
                self.audit._statement_conn,
                statement,
                cursor_conn=self.state._statement_conn,
            )
            # The helper deliberately commits audit before advancing the state
            # cursor.  A retry repairs a lagging cursor from duplicate evidence.
            return result

    def record_binance_futures_income_statement(
        self,
        payload: Mapping[str, Any],
        *,
        account_id: str,
        trading_mode: str,
        strategy_id: str,
        venue: str = "BINANCE",
        runtime_mode: str = "",
        session_id: str = "",
        availability_time: str = "",
        code_hash: str = "",
        config_hash: str = "",
        schema_hash: str = "",
        cycle_id: str = "",
        intent_id: str = "",
    ) -> ExchangeStatementIngestionResult:
        statement = normalize_binance_futures_income(
            payload,
            account_id=account_id,
            trading_mode=trading_mode,
            strategy_id=strategy_id,
            venue=venue,
            runtime_mode=runtime_mode,
            session_id=session_id,
        )
        with self.audit._exchange_statement_lock:
            statement = _prepare_binance_futures_income_statement(
                self.audit._statement_conn,
                statement,
                availability_time=availability_time,
                code_hash=code_hash,
                config_hash=config_hash,
                schema_hash=schema_hash,
                cycle_id=cycle_id,
                intent_id=intent_id,
            )
            return self.record_exchange_statement(statement)

    def record_binance_margin_interest_statement(
        self,
        payload: Mapping[str, Any],
        *,
        account_id: str,
        trading_mode: str,
        strategy_id: str,
        venue: str = "BINANCE",
        runtime_mode: str = "",
        session_id: str = "",
        availability_time: str = "",
        code_hash: str = "",
        config_hash: str = "",
        schema_hash: str = "",
    ) -> ExchangeStatementIngestionResult:
        statement = normalize_binance_margin_interest(
            payload,
            account_id=account_id,
            trading_mode=trading_mode,
            strategy_id=strategy_id,
            venue=venue,
            runtime_mode=runtime_mode,
            session_id=session_id,
        )
        if statement.economic_event is not None:
            statement = replace(
                statement,
                economic_event=replace(
                    statement.economic_event,
                    availability_time=availability_time,
                    code_hash=code_hash,
                    config_hash=config_hash,
                    schema_hash=schema_hash,
                ),
            )
        return self.record_exchange_statement(statement)

    def project_entry_lifecycle(
        self,
        *,
        event_key: str,
        intent_id: str,
        event_time: str,
        position_fields: Mapping[str, Any],
        evidence: Mapping[str, Any],
    ) -> bool:
        symbol = str(position_fields.get("symbol") or "").upper()
        canonical = {"position": dict(position_fields), "evidence": dict(evidence)}
        with self.audit._lifecycle_lock:
            inserted = self.audit._claim_lifecycle_event(
                event_key=event_key,
                event_type="ENTRY_FILLED",
                symbol=symbol,
                intent_id=intent_id,
                event_time=event_time,
                payload=canonical,
            )
            self.audit.conn.commit()
        savepoint = "split_entry_lifecycle_projection"
        self.state.conn.execute(f"SAVEPOINT {savepoint}")
        try:
            self.state.upsert_position(**dict(position_fields), commit=False)
            if intent_id:
                state, reconciliation, sequence, reason = (
                    self.state._lifecycle_tombstone_fields(evidence)
                )
                self.state.tombstone_pending_intent(
                    intent_id,
                    lifecycle_state=state,
                    terminal_sequence=sequence,
                    reconciliation_status=reconciliation,
                    reason=reason,
                    tombstoned_at=event_time,
                    commit=False,
                )
            self.state.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            self.state.conn.commit()
        except Exception:
            self.state.conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            self.state.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        return inserted

    def project_exit_lifecycle(
        self,
        *,
        event_key: str,
        intent_id: str,
        event_time: str,
        trade: Trade,
        evidence: Mapping[str, Any],
    ) -> bool:
        canonical = {"trade": asdict(trade), "evidence": dict(evidence)}
        savepoint = "split_exit_lifecycle_audit"
        with self.audit._lifecycle_lock:
            self.audit.conn.execute(f"SAVEPOINT {savepoint}")
            try:
                inserted = self.audit._claim_lifecycle_event(
                    event_key=event_key,
                    event_type="EXIT_FILLED",
                    symbol=trade.symbol,
                    intent_id=intent_id,
                    event_time=event_time,
                    payload=canonical,
                )
                if inserted:
                    self.audit.record_trade(trade, commit=False)
                self.audit.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                self.audit.conn.commit()
            except Exception:
                self.audit.conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self.audit.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise
        state_savepoint = "split_exit_lifecycle_projection"
        self.state.conn.execute(f"SAVEPOINT {state_savepoint}")
        try:
            self.state.remove_position(trade.symbol, commit=False)
            if intent_id:
                state, reconciliation, sequence, reason = (
                    self.state._lifecycle_tombstone_fields(evidence)
                )
                self.state.tombstone_pending_intent(
                    intent_id,
                    lifecycle_state=state,
                    terminal_sequence=sequence,
                    reconciliation_status=reconciliation,
                    reason=reason,
                    tombstoned_at=event_time,
                    commit=False,
                )
            self.state.conn.execute(f"RELEASE SAVEPOINT {state_savepoint}")
            self.state.conn.commit()
        except Exception:
            self.state.conn.execute(f"ROLLBACK TO SAVEPOINT {state_savepoint}")
            self.state.conn.execute(f"RELEASE SAVEPOINT {state_savepoint}")
            raise
        return inserted

    def project_partial_exit_lifecycle(
        self,
        *,
        event_key: str,
        intent_id: str,
        event_time: str,
        remaining_position_fields: Mapping[str, Any],
        evidence: Mapping[str, Any],
    ) -> bool:
        """Persist partial-exit evidence before projecting residual hot state."""

        remaining = dict(remaining_position_fields)
        symbol = str(remaining.get("symbol") or "").upper()
        try:
            remaining_qty = Decimal(str(remaining.get("qty", "0")))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("partial exit residual quantity is not decimal-safe") from exc
        if not symbol or remaining_qty <= 0:
            raise ValueError("partial exit requires a positive residual position")
        canonical = {
            "remaining_position": remaining,
            "evidence": dict(evidence),
        }
        with self.audit._lifecycle_lock:
            inserted = self.audit._claim_lifecycle_event(
                event_key=event_key,
                event_type="PARTIAL_EXIT_FILLED",
                symbol=symbol,
                intent_id=intent_id,
                event_time=event_time,
                payload=canonical,
            )
            self.audit.conn.commit()
        savepoint = "split_partial_exit_lifecycle_projection"
        self.state.conn.execute(f"SAVEPOINT {savepoint}")
        try:
            self.state.upsert_position(**remaining, commit=False)
            if intent_id:
                state, reconciliation, sequence, reason = (
                    self.state._lifecycle_tombstone_fields(evidence)
                )
                self.state.tombstone_pending_intent(
                    intent_id,
                    lifecycle_state=state,
                    terminal_sequence=sequence,
                    reconciliation_status=reconciliation,
                    reason=reason,
                    tombstoned_at=event_time,
                    commit=False,
                )
            self.state.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            self.state.conn.commit()
        except Exception:
            self.state.conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            self.state.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        return inserted

    def rebuild_lifecycle_projections(
        self,
        *,
        authoritative_positions: Iterable[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Rebuild hot positions from hash-verified immutable audit events."""

        rows = self.audit.conn.execute(
            "SELECT event_key, event_type, symbol, intent_id, event_time, "
            "content_hash, payload_json FROM lifecycle_events "
            "ORDER BY event_time, event_key"
        ).fetchall()
        projected: dict[str, dict[str, Any]] = {}
        intent_tombstones: dict[str, Mapping[str, Any]] = {}
        trade_count = 0
        for row in rows:
            payload_json = str(row["payload_json"])
            if hashlib.sha256(payload_json.encode("utf-8")).hexdigest() != str(
                row["content_hash"]
            ):
                raise LifecycleRebuildError(
                    f"lifecycle content hash mismatch: {row['event_key']}"
                )
            try:
                payload = json.loads(payload_json)
            except json.JSONDecodeError as exc:
                raise LifecycleRebuildError(
                    f"invalid lifecycle payload: {row['event_key']}"
                ) from exc
            if not isinstance(payload, dict):
                raise LifecycleRebuildError(
                    f"lifecycle payload is not an object: {row['event_key']}"
                )
            event_type = str(row["event_type"]).upper()
            symbol = str(row["symbol"]).upper()
            if event_type == "ENTRY_FILLED":
                position = payload.get("position")
                if not isinstance(position, dict) or str(
                    position.get("symbol") or ""
                ).upper() != symbol:
                    raise LifecycleRebuildError(
                        f"entry lifecycle lacks a matching position: {row['event_key']}"
                    )
                projected[symbol] = dict(position)
            elif event_type == "PARTIAL_EXIT_FILLED":
                remaining = payload.get("remaining_position")
                if not isinstance(remaining, dict) or str(
                    remaining.get("symbol") or ""
                ).upper() != symbol:
                    raise LifecycleRebuildError(
                        f"partial exit lifecycle lacks a matching residual: {row['event_key']}"
                    )
                if symbol not in projected:
                    raise LifecycleRebuildError(
                        f"partial exit lifecycle lacks prior position: {row['event_key']}"
                    )
                try:
                    before_qty = Decimal(str(projected[symbol].get("qty", "0")))
                    after_qty = Decimal(str(remaining.get("qty", "0")))
                except (InvalidOperation, TypeError, ValueError) as exc:
                    raise LifecycleRebuildError(
                        f"partial exit quantity is not decimal-safe: {row['event_key']}"
                    ) from exc
                if after_qty <= 0 or after_qty >= before_qty:
                    raise LifecycleRebuildError(
                        f"partial exit residual is not strictly smaller: {row['event_key']}"
                    )
                projected[symbol] = dict(remaining)
            elif event_type == "EXIT_FILLED":
                trade = payload.get("trade")
                if not isinstance(trade, dict) or str(
                    trade.get("symbol") or ""
                ).upper() != symbol:
                    raise LifecycleRebuildError(
                        f"exit lifecycle lacks a matching trade: {row['event_key']}"
                    )
                projected.pop(symbol, None)
                trade_count += 1
            else:
                raise LifecycleRebuildError(
                    f"unsupported lifecycle event type: {event_type}"
                )
            if str(row["intent_id"]):
                evidence = payload.get("evidence")
                intent_tombstones[str(row["intent_id"])] = (
                    evidence if isinstance(evidence, dict) else {}
                )

        def identity(position: Mapping[str, Any]) -> tuple[str, str, str, str, str]:
            try:
                qty = str(Decimal(str(position.get("qty", "0"))).normalize())
                hedge = str(
                    Decimal(str(position.get("hedge_ratio", "1"))).normalize()
                )
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise LifecycleRebuildError(
                    "position identity is not decimal-safe"
                ) from exc
            return (
                str(position.get("symbol") or "").upper(),
                str(position.get("side") or "").upper(),
                str(position.get("direction") or "").lower(),
                qty,
                hedge,
            )

        expected = sorted(identity(item) for item in projected.values())
        observed = sorted(identity(item) for item in authoritative_positions)
        if expected != observed:
            raise LifecycleRebuildError(
                "lifecycle replay does not match authoritative exchange positions"
            )
        proof_payload = {
            "events": len(rows),
            "positions": expected,
            "trades": trade_count,
        }
        proof_hash = hashlib.sha256(
            json.dumps(proof_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        savepoint = "split_lifecycle_projection_rebuild"
        self.state.conn.execute(f"SAVEPOINT {savepoint}")
        try:
            self.state.conn.execute("DELETE FROM positions")
            for position in projected.values():
                self.state.upsert_position(**position, commit=False)
            for intent_id, evidence in intent_tombstones.items():
                state, reconciliation, sequence, reason = (
                    self.state._lifecycle_tombstone_fields(evidence)
                )
                self.state.tombstone_pending_intent(
                    intent_id,
                    lifecycle_state=state,
                    terminal_sequence=sequence,
                    reconciliation_status=reconciliation,
                    reason=f"lifecycle_rebuild:{reason}",
                    commit=False,
                )
            self.state.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            self.state.conn.commit()
        except Exception:
            self.state.conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            self.state.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        return {
            "event_count": len(rows),
            "position_count": len(projected),
            "trade_count": trade_count,
            "proof_hash": proof_hash,
            "exchange_positions_matched": True,
        }

    def archive_old_data(
        self,
        *,
        archive_db_path: str | None = None,
        retention_days: int = 90,
        market_retention_days: int = 21,
        health_retention_days: int = 21,
        snapshot_retention_days: int | None = None,
        feature_retention_days: int | None = None,
        batch_size: int = 2_000,
        max_batches_per_table: int = 32,
        archive_max_bytes: int = 1_100_000_000,
    ) -> dict[str, int]:
        """Reject the monolithic archive API at the split-store boundary.

        The inherited implementation assumes every source table and its
        archive manifest share ``self.conn``. A split writer spans three role
        databases, and Tier-A audit evidence is intentionally immutable, so
        attempting to reuse that implementation would either query the wrong
        role or violate the split retention contract.
        """

        raise SplitStoreError(
            "archive_old_data is unsupported for split stores; use "
            "prune_optional_retention for bounded Tier-B/Tier-C retention and "
            "the verified backup/offline archive workflow for Tier-A evidence"
        )

    def maintenance(
        self,
        run_vacuum: bool = False,
        *,
        quiescent: bool = False,
        incremental_pages: int = 1_000,
    ) -> dict[str, Any]:
        return {
            "state": self.state.maintenance(
                run_vacuum,
                quiescent=quiescent,
                incremental_pages=incremental_pages,
            ),
            "audit": self.audit.maintenance(
                run_vacuum,
                quiescent=quiescent,
                incremental_pages=incremental_pages,
            ),
            "research": self.research.maintenance(
                run_vacuum,
                quiescent=quiescent,
                incremental_pages=incremental_pages,
            ),
        }

    def prune_optional_retention(
        self,
        *,
        market_retention_days: int,
        health_retention_days: int,
        snapshot_retention_days: int,
        feature_retention_days: int,
        general_retention_days: int,
        max_rows_per_table: int | None = None,
        market_aggregation_max_hours: int | None = None,
    ) -> dict[str, int]:
        """Bound only Tier-B/Tier-C evidence; Tier-A audit rows are immutable.

        Production callers should set both bounds.  The optional defaults keep
        the offline maintenance API backward compatible, while bounded online
        passes cap DELETE/WAL work per table and aggregate at most a fixed
        complete market-sample time window.
        """

        now = datetime.now(timezone.utc)
        row_limit = (
            None
            if max_rows_per_table is None
            else max(1, int(max_rows_per_table))
        )
        market_window_hours = (
            None
            if market_aggregation_max_hours is None
            else max(1, int(market_aggregation_max_hours))
        )

        def cutoff(days: int) -> str:
            return (now - timedelta(days=max(1, int(days)))).isoformat()

        audit_rules = (
            ("health_samples", "sample_time", health_retention_days),
            ("feed_recovery_events", "event_time", health_retention_days),
        )
        research_rules = (
            ("market_samples", "sample_minute", market_retention_days),
            ("candidate_snapshots", "snapshot_time", snapshot_retention_days),
            ("opportunity_scores", "score_time", snapshot_retention_days),
            ("feature_snapshots", "snapshot_time", feature_retention_days),
            ("execution_quality", "sample_time", feature_retention_days),
            ("model_shadow_decisions", "decision_time", snapshot_retention_days),
        )
        results: dict[str, int] = {}

        def delete_before(
            writer: StateWriter,
            table_name: str,
            time_column: str,
            cutoff_value: str,
            *,
            bounded_rows: bool = True,
        ) -> int:
            if row_limit is None or not bounded_rows:
                cursor = writer.conn.execute(
                    f'DELETE FROM "{table_name}" WHERE "{time_column}" < ?',
                    (cutoff_value,),
                )
            else:
                cursor = writer.conn.execute(
                    f'''DELETE FROM "{table_name}" WHERE rowid IN (
                            SELECT rowid FROM "{table_name}"
                            WHERE "{time_column}" < ?
                            ORDER BY "{time_column}", rowid
                            LIMIT ?
                        )''',
                    (cutoff_value, row_limit),
                )
            return max(0, int(cursor.rowcount))

        for writer, rules in (
            (self.audit, audit_rules),
            (self.research, research_rules),
        ):
            market_delete_before = ""
            writer.conn.execute("BEGIN IMMEDIATE")
            try:
                if writer is self.research:
                    minute_cutoff = cutoff(market_retention_days)
                    hourly_cutoff = cutoff(_RESEARCH_HOURLY_RETENTION_DAYS)
                    market_delete_before = minute_cutoff
                    if market_window_hours is not None:
                        earliest_row = writer.conn.execute(
                            "SELECT MIN(sample_minute) FROM market_samples "
                            "WHERE sample_minute < ?",
                            (minute_cutoff,),
                        ).fetchone()
                        earliest_value = earliest_row[0] if earliest_row else None
                        if earliest_value:
                            try:
                                earliest = datetime.fromisoformat(
                                    str(earliest_value).replace("Z", "+00:00")
                                )
                                if earliest.tzinfo is None:
                                    earliest = earliest.replace(tzinfo=timezone.utc)
                                window_end = earliest.astimezone(timezone.utc).replace(
                                    minute=0,
                                    second=0,
                                    microsecond=0,
                                ) + timedelta(hours=market_window_hours)
                                minute_cutoff_time = datetime.fromisoformat(
                                    minute_cutoff.replace("Z", "+00:00")
                                )
                                market_delete_before = min(
                                    window_end,
                                    minute_cutoff_time,
                                ).isoformat()
                            except ValueError:
                                # Fail closed on malformed evidence time rather
                                # than turning a bounded pass into a full scan.
                                market_delete_before = str(earliest_value)
                    before = writer.conn.total_changes
                    writer.conn.execute(
                        """INSERT INTO market_hourly_aggregates (
                               bucket_hour, symbol, sample_count,
                               ann_funding_avg, ann_funding_min, ann_funding_max,
                               basis_pct_avg, basis_pct_min, basis_pct_max,
                               mark_price_avg, mark_price_min, mark_price_max,
                               notional_volume_sum, source_first_minute,
                               source_last_minute, refreshed_at
                           )
                           SELECT substr(sample_minute, 1, 13) || ':00:00+00:00',
                                  symbol, COUNT(*),
                                  AVG(ann_funding), MIN(ann_funding), MAX(ann_funding),
                                  AVG(basis_pct), MIN(basis_pct), MAX(basis_pct),
                                  AVG(mark_price), MIN(mark_price), MAX(mark_price),
                                  SUM(minute_notional_volume),
                                  MIN(sample_minute), MAX(sample_minute), ?
                           FROM market_samples
                           WHERE sample_minute < ? AND sample_minute >= ?
                           GROUP BY substr(sample_minute, 1, 13), symbol
                           ON CONFLICT(bucket_hour, symbol) DO UPDATE SET
                               sample_count=excluded.sample_count,
                               ann_funding_avg=excluded.ann_funding_avg,
                               ann_funding_min=excluded.ann_funding_min,
                               ann_funding_max=excluded.ann_funding_max,
                               basis_pct_avg=excluded.basis_pct_avg,
                               basis_pct_min=excluded.basis_pct_min,
                               basis_pct_max=excluded.basis_pct_max,
                               mark_price_avg=excluded.mark_price_avg,
                               mark_price_min=excluded.mark_price_min,
                               mark_price_max=excluded.mark_price_max,
                               notional_volume_sum=excluded.notional_volume_sum,
                               source_first_minute=excluded.source_first_minute,
                               source_last_minute=excluded.source_last_minute,
                               refreshed_at=excluded.refreshed_at""",
                        (_now(), market_delete_before, hourly_cutoff),
                    )
                    results["market_hourly_aggregates_upserted"] = (
                        writer.conn.total_changes - before
                    )
                    results["market_hourly_aggregates_deleted"] = delete_before(
                        writer,
                        "market_hourly_aggregates",
                        "bucket_hour",
                        hourly_cutoff,
                    )
                for table_name, time_column, days in rules:
                    table_cutoff = (
                        market_delete_before
                        if writer is self.research and table_name == "market_samples"
                        else cutoff(days)
                    )
                    results[f"{table_name}_deleted"] = delete_before(
                        writer,
                        table_name,
                        time_column,
                        table_cutoff,
                        # The complete aggregation window is itself time
                        # bounded and must be removed atomically so a later
                        # upsert cannot overwrite an aggregate with a partial.
                        bounded_rows=table_name != "market_samples",
                    )
                writer.conn.commit()
            except Exception:
                writer.conn.rollback()
                raise
        return results

    def close(self) -> None:
        self.research.close()
        self.audit.close()
        self.state.close()


class SplitStateReader(StateReader):
    """Route the StateReader API across exact state/audit/research stores."""

    def __init__(self, *, state_path: str, audit_path: str, research_path: str) -> None:
        paths = _prepare_split_store(
            state_path=state_path,
            audit_path=audit_path,
            research_path=research_path,
            allow_initialize=False,
        )
        self.state = _RoleReader(str(paths["state.db"]), role="state.db")
        try:
            self.research = _RoleReader(
                str(paths["research.db"]),
                role="research.db",
                scope_provider=self.state._current_scope,
            )
            try:
                self.audit = _RoleReader(
                    str(paths["audit.db"]),
                    role="audit.db",
                    scope_provider=self.state._current_scope,
                    market_price_provider=self.research._latest_market_price,
                )
            except Exception:
                self.research.close()
                raise
        except Exception:
            self.state.close()
            raise

    @property
    def conn(self) -> sqlite3.Connection:
        return self.state.conn

    def __getattribute__(self, name: str) -> Any:
        if name in _AUDIT_READER_METHODS:
            return getattr(object.__getattribute__(self, "audit"), name)
        if name in _RESEARCH_READER_METHODS:
            return getattr(object.__getattribute__(self, "research"), name)
        return super().__getattribute__(name)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.state, name)

    def get_exchange_statement_cursor(self, **kwargs: Any) -> dict[str, Any] | None:
        return self.state.get_exchange_statement_cursor(**kwargs)

    def get_db_stats(self) -> dict[str, Any]:
        return {
            "state": self.state.get_db_stats(),
            "audit": self.audit.get_db_stats(),
            "research": self.research.get_db_stats(),
        }

    def close(self) -> None:
        self.audit.close()
        self.research.close()
        self.state.close()
