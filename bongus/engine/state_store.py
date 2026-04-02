"""SQLite-backed observability, runtime, and governance state store."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from bongus.core.config import STATE_DB_PATH

DB_PATH = STATE_DB_PATH
CURRENT_SCHEMA_VERSION = 2


@dataclass(slots=True)
class Trade:
    symbol: str
    side: str
    entry_time: str
    exit_time: str
    entry_price: float
    exit_price: float
    qty: float
    net_pnl_usd: float
    funding_collected: float = 0.0
    execution_cost_usd: float = 0.0
    basis_pnl_usd: float = 0.0


@dataclass(slots=True)
class CandidateSnapshot:
    cycle_id: str
    symbol: str
    direction: str
    accepted: bool
    status: str
    cluster: str
    rejection_reasons: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    snapshot_time: str | None = None
    rank: int | None = None


@dataclass(slots=True)
class OpportunityScore:
    cycle_id: str
    symbol: str
    total_score: float
    predicted_net_edge_bps: float
    rank: int
    selected: bool
    component_scores: dict[str, float]
    expected_holding_hours: float
    score_time: str | None = None


@dataclass(slots=True)
class FeatureSnapshot:
    trade_id: str
    symbol: str
    features: dict[str, Any]
    snapshot_time: str | None = None
    target_incremental_value_usd: float | None = None
    label: str = ""


@dataclass(slots=True)
class ExecutionQualitySample:
    symbol: str
    client_order_id: str
    side: str
    order_type: str
    urgency: float
    expected_cost_bps: float
    realized_slippage_bps: float
    spread_bps: float
    depth_usd: float
    maker: bool
    quality_score: float
    sample_time: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ShadowDecision:
    trade_id: str
    symbol: str
    action: str
    hold_score: float
    exit_score: float
    incremental_value_usd: float
    recommended: bool
    decision_time: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ParameterPromotion:
    status: str
    params: dict[str, Any]
    source: str = "walk_forward"
    validation_snapshot_time: str | None = None
    promoted_at: str | None = None
    rollback_reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ValidationSnapshot:
    phase: str
    validation_status: str
    go_no_go: str
    observation_days: float
    trade_count: int
    blockers: list[str]
    metrics: dict[str, Any]
    snapshot_time: str | None = None


@dataclass(slots=True)
class PendingIntent:
    intent_id: str
    symbol: str
    intent_type: str
    direction: str
    status: str
    quantity: float
    notional_usd: float
    client_order_id: str | None = None
    retry_count: int = 0
    last_error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str | None = None
    updated_at: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def _connect(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    _apply_migrations(conn)
    return conn


def _apply_migrations(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS positions (
            symbol        TEXT PRIMARY KEY,
            side          TEXT NOT NULL,
            spot_entry    REAL NOT NULL,
            perp_entry    REAL NOT NULL,
            spot_live     REAL DEFAULT 0.0,
            perp_live     REAL DEFAULT 0.0,
            qty           REAL NOT NULL,
            ann_funding   REAL DEFAULT 0.0,
            basis_pct     REAL DEFAULT 0.0,
            net_pnl_usd   REAL DEFAULT 0.0,
            status        TEXT DEFAULT 'OPEN',
            updated_at    TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS portfolio_stats (
            key        TEXT PRIMARY KEY,
            value      REAL NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS trade_history (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol             TEXT NOT NULL,
            side               TEXT NOT NULL,
            entry_time         TEXT NOT NULL,
            exit_time          TEXT NOT NULL,
            entry_price        REAL NOT NULL,
            exit_price         REAL NOT NULL,
            qty                REAL NOT NULL,
            net_pnl_usd        REAL NOT NULL,
            funding_collected  REAL DEFAULT 0.0,
            execution_cost_usd REAL DEFAULT 0.0,
            basis_pnl_usd      REAL DEFAULT 0.0
        );

        CREATE TABLE IF NOT EXISTS risk_state (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS candidate_snapshots (
            cycle_id          TEXT NOT NULL,
            symbol            TEXT NOT NULL,
            snapshot_time     TEXT NOT NULL,
            direction         TEXT NOT NULL,
            accepted          INTEGER NOT NULL,
            status            TEXT NOT NULL,
            cluster           TEXT NOT NULL,
            rank              INTEGER,
            rejection_reasons TEXT NOT NULL,
            metrics_json      TEXT NOT NULL,
            PRIMARY KEY (cycle_id, symbol)
        );

        CREATE TABLE IF NOT EXISTS opportunity_scores (
            cycle_id               TEXT NOT NULL,
            symbol                 TEXT NOT NULL,
            score_time             TEXT NOT NULL,
            total_score            REAL NOT NULL,
            predicted_net_edge_bps REAL NOT NULL,
            rank                   INTEGER NOT NULL,
            selected               INTEGER NOT NULL,
            expected_holding_hours REAL NOT NULL,
            component_scores_json  TEXT NOT NULL,
            PRIMARY KEY (cycle_id, symbol)
        );

        CREATE TABLE IF NOT EXISTS feature_snapshots (
            id                           INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_time                TEXT NOT NULL,
            trade_id                     TEXT NOT NULL,
            symbol                       TEXT NOT NULL,
            label                        TEXT DEFAULT '',
            target_incremental_value_usd REAL,
            features_json                TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS execution_quality (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            sample_time           TEXT NOT NULL,
            symbol                TEXT NOT NULL,
            client_order_id       TEXT NOT NULL,
            side                  TEXT NOT NULL,
            order_type            TEXT NOT NULL,
            urgency               REAL DEFAULT 0.0,
            expected_cost_bps     REAL DEFAULT 0.0,
            realized_slippage_bps REAL DEFAULT 0.0,
            spread_bps            REAL DEFAULT 0.0,
            depth_usd             REAL DEFAULT 0.0,
            maker                 INTEGER DEFAULT 0,
            quality_score         REAL DEFAULT 0.0,
            metadata_json         TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS model_shadow_decisions (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_time         TEXT NOT NULL,
            trade_id              TEXT NOT NULL,
            symbol                TEXT NOT NULL,
            action                TEXT NOT NULL,
            hold_score            REAL DEFAULT 0.0,
            exit_score            REAL DEFAULT 0.0,
            incremental_value_usd REAL DEFAULT 0.0,
            recommended           INTEGER DEFAULT 0,
            metadata_json         TEXT NOT NULL
        );
        """
    )
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS parameter_promotions (
            id                       INTEGER PRIMARY KEY AUTOINCREMENT,
            promoted_at              TEXT NOT NULL,
            status                   TEXT NOT NULL,
            source                   TEXT NOT NULL,
            validation_snapshot_time TEXT,
            rollback_reason          TEXT DEFAULT '',
            params_json              TEXT NOT NULL,
            metadata_json            TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS validation_snapshots (
            snapshot_time        TEXT PRIMARY KEY,
            phase                TEXT NOT NULL,
            validation_status    TEXT NOT NULL,
            go_no_go             TEXT NOT NULL,
            observation_days     REAL DEFAULT 0.0,
            trade_count          INTEGER DEFAULT 0,
            blockers             TEXT NOT NULL,
            metrics_json         TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS execution_events (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol               TEXT NOT NULL,
            client_order_id      TEXT NOT NULL,
            status               TEXT NOT NULL,
            filled_qty           REAL DEFAULT 0.0,
            avg_fill_price       REAL,
            last_fill_price      REAL,
            cumulative_quote_qty REAL,
            commission           REAL,
            commission_asset     TEXT,
            realized_pnl         REAL,
            maker                INTEGER,
            execution_type       TEXT,
            event_time           TEXT NOT NULL,
            raw_payload          TEXT
        );

        CREATE TABLE IF NOT EXISTS health_samples (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            sample_time    TEXT NOT NULL,
            symbol         TEXT,
            metric         TEXT NOT NULL,
            value          REAL DEFAULT 0.0,
            expected_value REAL DEFAULT 0.0,
            zscore         REAL,
            alert_level    TEXT DEFAULT '',
            runtime_mode   TEXT DEFAULT '',
            notes          TEXT
        );

        CREATE TABLE IF NOT EXISTS market_samples (
            id                     INTEGER PRIMARY KEY AUTOINCREMENT,
            sample_minute          TEXT NOT NULL,
            symbol                 TEXT NOT NULL,
            ann_funding            REAL DEFAULT 0.0,
            basis_pct              REAL DEFAULT 0.0,
            mark_price             REAL DEFAULT 0.0,
            minute_notional_volume REAL DEFAULT 0.0,
            UNIQUE(symbol, sample_minute)
        );

        CREATE TABLE IF NOT EXISTS pending_intents (
            intent_id       TEXT PRIMARY KEY,
            symbol          TEXT NOT NULL,
            intent_type     TEXT NOT NULL,
            direction       TEXT NOT NULL DEFAULT '',
            status          TEXT NOT NULL,
            quantity        REAL DEFAULT 0.0,
            notional_usd    REAL DEFAULT 0.0,
            client_order_id TEXT,
            retry_count     INTEGER DEFAULT 0,
            last_error      TEXT,
            metadata        TEXT,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ai_report_proposals (
            proposal_id          TEXT PRIMARY KEY,
            created_at           TEXT NOT NULL,
            report_period_start  TEXT,
            report_period_end    TEXT,
            summary              TEXT NOT NULL,
            proposed_changes     TEXT NOT NULL,
            status               TEXT NOT NULL,
            decision_time        TEXT,
            decision_source      TEXT,
            applied_at           TEXT,
            raw_response         TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_candidate_snapshots_time ON candidate_snapshots(snapshot_time DESC);
        CREATE INDEX IF NOT EXISTS idx_opportunity_scores_time ON opportunity_scores(score_time DESC);
        CREATE INDEX IF NOT EXISTS idx_feature_snapshots_trade ON feature_snapshots(trade_id, snapshot_time DESC);
        CREATE INDEX IF NOT EXISTS idx_execution_quality_symbol ON execution_quality(symbol, sample_time DESC);
        CREATE INDEX IF NOT EXISTS idx_shadow_decisions_symbol ON model_shadow_decisions(symbol, decision_time DESC);
        CREATE INDEX IF NOT EXISTS idx_promotions_time ON parameter_promotions(promoted_at DESC);
        CREATE INDEX IF NOT EXISTS idx_health_samples_time ON health_samples(sample_time DESC);
        CREATE INDEX IF NOT EXISTS idx_market_samples_time ON market_samples(sample_minute DESC);
        """
    )
    conn.execute(
        """
        INSERT INTO schema_meta (key, value)
        VALUES ('schema_version', ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (str(CURRENT_SCHEMA_VERSION),),
    )
    conn.commit()


class StateWriter:
    def __init__(self, db_path: str = DB_PATH) -> None:
        self.conn = _connect(db_path)

    def upsert_position(
        self,
        symbol: str,
        side: str,
        spot_entry: float,
        perp_entry: float,
        qty: float,
        ann_funding: float = 0.0,
        basis_pct: float = 0.0,
        net_pnl_usd: float = 0.0,
        status: str = "OPEN",
        spot_live: float = 0.0,
        perp_live: float = 0.0,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO positions
                (symbol, side, spot_entry, perp_entry, spot_live, perp_live, qty,
                 ann_funding, basis_pct, net_pnl_usd, status, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                side=excluded.side,
                spot_entry=excluded.spot_entry,
                perp_entry=excluded.perp_entry,
                spot_live=excluded.spot_live,
                perp_live=excluded.perp_live,
                qty=excluded.qty,
                ann_funding=excluded.ann_funding,
                basis_pct=excluded.basis_pct,
                net_pnl_usd=excluded.net_pnl_usd,
                status=excluded.status,
                updated_at=excluded.updated_at
            """,
            (
                symbol,
                side,
                spot_entry,
                perp_entry,
                spot_live,
                perp_live,
                qty,
                ann_funding,
                basis_pct,
                net_pnl_usd,
                status,
                _now(),
            ),
        )
        self.conn.commit()

    def remove_position(self, symbol: str) -> None:
        self.conn.execute("DELETE FROM positions WHERE symbol = ?", (symbol,))
        self.conn.commit()

    def record_trade(self, trade: Trade) -> None:
        self.conn.execute(
            """
            INSERT INTO trade_history
                (symbol, side, entry_time, exit_time, entry_price, exit_price, qty,
                 net_pnl_usd, funding_collected, execution_cost_usd, basis_pnl_usd)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trade.symbol,
                trade.side,
                trade.entry_time,
                trade.exit_time,
                trade.entry_price,
                trade.exit_price,
                trade.qty,
                trade.net_pnl_usd,
                trade.funding_collected,
                trade.execution_cost_usd,
                trade.basis_pnl_usd,
            ),
        )
        self.conn.commit()

    def set_stat(self, key: str, value: float) -> None:
        self.conn.execute(
            """
            INSERT INTO portfolio_stats (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, float(value), _now()),
        )
        self.conn.commit()

    def set_stats(self, stats: dict[str, float]) -> None:
        now = _now()
        self.conn.executemany(
            """
            INSERT INTO portfolio_stats (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            [(key, float(value), now) for key, value in stats.items()],
        )
        self.conn.commit()

    def set_risk(self, key: str, value: str) -> None:
        self.conn.execute(
            """
            INSERT INTO risk_state (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, value, _now()),
        )
        self.conn.commit()

    def set_risk_snapshot(self, snapshot: dict[str, Any]) -> None:
        now = _now()
        rows = [
            (key, value if isinstance(value, str) else _json_dump(value), now)
            for key, value in snapshot.items()
        ]
        self.conn.executemany(
            """
            INSERT INTO risk_state (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            rows,
        )
        self.conn.commit()

    def record_candidate_snapshots(self, snapshots: Iterable[CandidateSnapshot]) -> None:
        rows = [
            (
                snapshot.cycle_id,
                snapshot.symbol,
                snapshot.snapshot_time or _now(),
                snapshot.direction,
                int(snapshot.accepted),
                snapshot.status,
                snapshot.cluster,
                snapshot.rank,
                _json_dump(snapshot.rejection_reasons),
                _json_dump(snapshot.metrics),
            )
            for snapshot in snapshots
        ]
        self.conn.executemany(
            """
            INSERT INTO candidate_snapshots
                (cycle_id, symbol, snapshot_time, direction, accepted, status,
                 cluster, rank, rejection_reasons, metrics_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cycle_id, symbol) DO UPDATE SET
                snapshot_time=excluded.snapshot_time,
                direction=excluded.direction,
                accepted=excluded.accepted,
                status=excluded.status,
                cluster=excluded.cluster,
                rank=excluded.rank,
                rejection_reasons=excluded.rejection_reasons,
                metrics_json=excluded.metrics_json
            """,
            rows,
        )
        self.conn.commit()

    def record_opportunity_scores(self, scores: Iterable[OpportunityScore]) -> None:
        rows = [
            (
                score.cycle_id,
                score.symbol,
                score.score_time or _now(),
                score.total_score,
                score.predicted_net_edge_bps,
                score.rank,
                int(score.selected),
                score.expected_holding_hours,
                _json_dump(score.component_scores),
            )
            for score in scores
        ]
        self.conn.executemany(
            """
            INSERT INTO opportunity_scores
                (cycle_id, symbol, score_time, total_score, predicted_net_edge_bps,
                 rank, selected, expected_holding_hours, component_scores_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cycle_id, symbol) DO UPDATE SET
                score_time=excluded.score_time,
                total_score=excluded.total_score,
                predicted_net_edge_bps=excluded.predicted_net_edge_bps,
                rank=excluded.rank,
                selected=excluded.selected,
                expected_holding_hours=excluded.expected_holding_hours,
                component_scores_json=excluded.component_scores_json
            """,
            rows,
        )
        self.conn.commit()

    def record_feature_snapshot(self, snapshot: FeatureSnapshot) -> None:
        self.record_feature_snapshots([snapshot])

    def record_feature_snapshots(self, snapshots: Iterable[FeatureSnapshot]) -> None:
        self.conn.executemany(
            """
            INSERT INTO feature_snapshots
                (snapshot_time, trade_id, symbol, label, target_incremental_value_usd, features_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    snapshot.snapshot_time or _now(),
                    snapshot.trade_id,
                    snapshot.symbol,
                    snapshot.label,
                    snapshot.target_incremental_value_usd,
                    _json_dump(snapshot.features),
                )
                for snapshot in snapshots
            ],
        )
        self.conn.commit()

    def record_execution_quality(self, sample: ExecutionQualitySample) -> None:
        self.conn.execute(
            """
            INSERT INTO execution_quality
                (sample_time, symbol, client_order_id, side, order_type, urgency,
                 expected_cost_bps, realized_slippage_bps, spread_bps, depth_usd,
                 maker, quality_score, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sample.sample_time or _now(),
                sample.symbol,
                sample.client_order_id,
                sample.side,
                sample.order_type,
                sample.urgency,
                sample.expected_cost_bps,
                sample.realized_slippage_bps,
                sample.spread_bps,
                sample.depth_usd,
                int(sample.maker),
                sample.quality_score,
                _json_dump(sample.metadata),
            ),
        )
        self.conn.commit()

    def record_shadow_decision(self, decision: ShadowDecision) -> None:
        self.conn.execute(
            """
            INSERT INTO model_shadow_decisions
                (decision_time, trade_id, symbol, action, hold_score, exit_score,
                 incremental_value_usd, recommended, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision.decision_time or _now(),
                decision.trade_id,
                decision.symbol,
                decision.action,
                decision.hold_score,
                decision.exit_score,
                decision.incremental_value_usd,
                int(decision.recommended),
                _json_dump(decision.metadata),
            ),
        )
        self.conn.commit()

    def record_parameter_promotion(self, promotion: ParameterPromotion) -> None:
        self.conn.execute(
            """
            INSERT INTO parameter_promotions
                (promoted_at, status, source, validation_snapshot_time, rollback_reason,
                 params_json, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                promotion.promoted_at or _now(),
                promotion.status,
                promotion.source,
                promotion.validation_snapshot_time,
                promotion.rollback_reason,
                _json_dump(promotion.params),
                _json_dump(promotion.metadata),
            ),
        )
        self.conn.commit()

    def record_validation_snapshot(self, snapshot: ValidationSnapshot) -> None:
        self.conn.execute(
            """
            INSERT INTO validation_snapshots
                (snapshot_time, phase, validation_status, go_no_go,
                 observation_days, trade_count, blockers, metrics_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(snapshot_time) DO UPDATE SET
                phase=excluded.phase,
                validation_status=excluded.validation_status,
                go_no_go=excluded.go_no_go,
                observation_days=excluded.observation_days,
                trade_count=excluded.trade_count,
                blockers=excluded.blockers,
                metrics_json=excluded.metrics_json
            """,
            (
                snapshot.snapshot_time or _now(),
                snapshot.phase,
                snapshot.validation_status,
                snapshot.go_no_go,
                snapshot.observation_days,
                snapshot.trade_count,
                _json_dump(snapshot.blockers),
                _json_dump(snapshot.metrics),
            ),
        )
        self.conn.commit()

    def record_execution_event(self, payload: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT INTO execution_events
                (symbol, client_order_id, status, filled_qty, avg_fill_price, last_fill_price,
                 cumulative_quote_qty, commission, commission_asset, realized_pnl,
                 maker, execution_type, event_time, raw_payload)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(payload.get("symbol", "")),
                str(payload.get("client_order_id", payload.get("clientOrderId", ""))),
                str(payload.get("status", "")),
                float(payload.get("filled_qty", payload.get("filledQty", 0.0)) or 0.0),
                payload.get("avg_fill_price"),
                payload.get("last_fill_price"),
                payload.get("cumulative_quote_qty"),
                payload.get("commission"),
                payload.get("commission_asset"),
                payload.get("realized_pnl"),
                payload.get("maker"),
                payload.get("execution_type"),
                str(payload.get("event_time", _now())),
                _json_dump(payload),
            ),
        )
        self.conn.commit()

    def record_health_sample(
        self,
        metric: str,
        value: float,
        expected_value: float = 0.0,
        symbol: str | None = None,
        zscore: float | None = None,
        alert_level: str = "",
        runtime_mode: str = "",
        notes: str = "",
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO health_samples
                (sample_time, symbol, metric, value, expected_value, zscore,
                 alert_level, runtime_mode, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (_now(), symbol, metric, value, expected_value, zscore, alert_level, runtime_mode, notes),
        )
        self.conn.commit()

    def upsert_market_sample(
        self,
        sample_minute: str,
        symbol: str,
        ann_funding: float,
        basis_pct: float,
        mark_price: float,
        minute_notional_volume: float = 0.0,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO market_samples
                (sample_minute, symbol, ann_funding, basis_pct, mark_price, minute_notional_volume)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, sample_minute) DO UPDATE SET
                ann_funding=excluded.ann_funding,
                basis_pct=excluded.basis_pct,
                mark_price=excluded.mark_price,
                minute_notional_volume=excluded.minute_notional_volume
            """,
            (sample_minute, symbol, ann_funding, basis_pct, mark_price, minute_notional_volume),
        )
        self.conn.commit()

    def upsert_pending_intent(self, intent: PendingIntent) -> None:
        now = _now()
        self.conn.execute(
            """
            INSERT INTO pending_intents
                (intent_id, symbol, intent_type, direction, status, quantity, notional_usd,
                 client_order_id, retry_count, last_error, metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(intent_id) DO UPDATE SET
                status=excluded.status,
                quantity=excluded.quantity,
                notional_usd=excluded.notional_usd,
                client_order_id=excluded.client_order_id,
                retry_count=excluded.retry_count,
                last_error=excluded.last_error,
                metadata=excluded.metadata,
                updated_at=excluded.updated_at
            """,
            (
                intent.intent_id,
                intent.symbol,
                intent.intent_type,
                intent.direction,
                intent.status,
                intent.quantity,
                intent.notional_usd,
                intent.client_order_id,
                intent.retry_count,
                intent.last_error,
                _json_dump(intent.metadata),
                intent.created_at or now,
                intent.updated_at or now,
            ),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


class StateReader:
    def __init__(self, db_path: str = DB_PATH) -> None:
        self.conn = _connect(db_path)

    def _rows_to_dicts(self, rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
        return [dict(row) for row in rows]

    def get_positions(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM positions WHERE status != 'CLOSED' ORDER BY symbol"
        ).fetchall()
        return self._rows_to_dicts(rows)

    def get_stats(self) -> dict[str, float]:
        rows = self.conn.execute("SELECT key, value FROM portfolio_stats").fetchall()
        return {row["key"]: row["value"] for row in rows}

    def get_trades(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM trade_history ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return self._rows_to_dicts(rows)

    def get_pnl_attribution(self) -> dict[str, Any]:
        row = self.conn.execute(
            """
            SELECT
                COALESCE(SUM(funding_collected), 0.0) AS total_funding,
                COALESCE(SUM(basis_pnl_usd), 0.0) AS total_basis_pnl,
                COALESCE(SUM(execution_cost_usd), 0.0) AS total_execution_cost,
                COALESCE(SUM(net_pnl_usd), 0.0) AS total_net_pnl,
                COUNT(*) AS trade_count
            FROM trade_history
            """
        ).fetchone()
        return dict(row) if row else {}

    def get_risk(self) -> dict[str, Any]:
        rows = self.conn.execute("SELECT key, value FROM risk_state").fetchall()
        result: dict[str, Any] = {}
        float_keys = {
            "drawdown_pct",
            "spread_toxicity",
            "venue_latency",
            "gross_exposure",
            "telemetry_staleness_seconds",
        }
        bool_keys = {
            "kill_switch",
            "allow_new_risk",
            "is_kill_switch",
            "telemetry_connected",
            "runtime_ready",
        }
        json_list_keys = {"reasons", "preflight_checks"}
        for row in rows:
            key = row["key"]
            value = row["value"]
            if key in float_keys:
                try:
                    result[key] = float(value)
                except (TypeError, ValueError):
                    continue
                continue
            if key in bool_keys:
                result[key] = str(value).lower() == "true"
                continue
            if key in json_list_keys:
                try:
                    parsed = json.loads(value)
                except (TypeError, json.JSONDecodeError):
                    parsed = []
                result[key] = [str(item) for item in parsed] if isinstance(parsed, list) else []
                continue
            try:
                result[key] = json.loads(value)
            except (TypeError, json.JSONDecodeError):
                result[key] = value
        return result

    def get_candidate_snapshots(self, cycle_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        if cycle_id is None:
            row = self.conn.execute(
                "SELECT cycle_id FROM candidate_snapshots ORDER BY snapshot_time DESC LIMIT 1"
            ).fetchone()
            cycle_id = row["cycle_id"] if row else None
        if cycle_id is None:
            return []
        rows = self.conn.execute(
            """
            SELECT * FROM candidate_snapshots
            WHERE cycle_id = ?
            ORDER BY accepted DESC, rank ASC, symbol ASC
            LIMIT ?
            """,
            (cycle_id, limit),
        ).fetchall()
        result = []
        for row in rows:
            data = dict(row)
            data["accepted"] = bool(data["accepted"])
            data["rejection_reasons"] = json.loads(data["rejection_reasons"])
            data["metrics"] = json.loads(data["metrics_json"])
            data.pop("metrics_json", None)
            result.append(data)
        return result

    def get_opportunity_scores(self, cycle_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if cycle_id is None:
            row = self.conn.execute(
                "SELECT cycle_id FROM opportunity_scores ORDER BY score_time DESC LIMIT 1"
            ).fetchone()
            cycle_id = row["cycle_id"] if row else None
        if cycle_id is None:
            return []
        rows = self.conn.execute(
            """
            SELECT * FROM opportunity_scores
            WHERE cycle_id = ?
            ORDER BY rank ASC, symbol ASC
            LIMIT ?
            """,
            (cycle_id, limit),
        ).fetchall()
        result = []
        for row in rows:
            data = dict(row)
            data["selected"] = bool(data["selected"])
            data["component_scores"] = json.loads(data["component_scores_json"])
            data.pop("component_scores_json", None)
            result.append(data)
        return result

    def get_feature_snapshots(self, trade_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        if trade_id is None:
            rows = self.conn.execute(
                "SELECT * FROM feature_snapshots ORDER BY snapshot_time DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM feature_snapshots WHERE trade_id = ? ORDER BY snapshot_time DESC LIMIT ?",
                (trade_id, limit),
            ).fetchall()
        result = []
        for row in rows:
            data = dict(row)
            data["features"] = json.loads(data["features_json"])
            data.pop("features_json", None)
            result.append(data)
        return result

    def get_execution_quality(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM execution_quality ORDER BY sample_time DESC LIMIT ?",
            (limit,),
        ).fetchall()
        result = []
        for row in rows:
            data = dict(row)
            data["maker"] = bool(data["maker"])
            data["metadata"] = json.loads(data["metadata_json"])
            data.pop("metadata_json", None)
            result.append(data)
        return result

    def get_shadow_decisions(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM model_shadow_decisions ORDER BY decision_time DESC LIMIT ?",
            (limit,),
        ).fetchall()
        result = []
        for row in rows:
            data = dict(row)
            data["recommended"] = bool(data["recommended"])
            data["metadata"] = json.loads(data["metadata_json"])
            data.pop("metadata_json", None)
            result.append(data)
        return result

    def get_parameter_promotions(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM parameter_promotions ORDER BY promoted_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        result = []
        for row in rows:
            data = dict(row)
            data["params"] = json.loads(data["params_json"])
            data["metadata"] = json.loads(data["metadata_json"])
            data.pop("params_json", None)
            data.pop("metadata_json", None)
            result.append(data)
        return result

    def get_validation_snapshots(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM validation_snapshots ORDER BY snapshot_time DESC LIMIT ?",
            (limit,),
        ).fetchall()
        result = []
        for row in rows:
            data = dict(row)
            data["blockers"] = json.loads(data["blockers"])
            data["metrics"] = json.loads(data["metrics_json"])
            data.pop("metrics_json", None)
            result.append(data)
        return result

    def get_execution_events(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM execution_events ORDER BY event_time DESC LIMIT ?",
            (limit,),
        ).fetchall()
        result = []
        for row in rows:
            data = dict(row)
            try:
                data["raw_payload"] = json.loads(data["raw_payload"]) if data["raw_payload"] else None
            except json.JSONDecodeError:
                pass
            result.append(data)
        return result

    def get_health_samples(self, metric: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if metric is None:
            rows = self.conn.execute(
                "SELECT * FROM health_samples ORDER BY sample_time DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM health_samples WHERE metric = ? ORDER BY sample_time DESC LIMIT ?",
                (metric, limit),
            ).fetchall()
        return self._rows_to_dicts(rows)

    def get_market_samples(self, symbol: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if symbol is None:
            rows = self.conn.execute(
                "SELECT * FROM market_samples ORDER BY sample_minute DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM market_samples WHERE symbol = ? ORDER BY sample_minute DESC LIMIT ?",
                (symbol, limit),
            ).fetchall()
        return self._rows_to_dicts(rows)

    def get_pending_intents(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if status is None:
            rows = self.conn.execute(
                "SELECT * FROM pending_intents ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM pending_intents WHERE status = ? ORDER BY updated_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        result = []
        for row in rows:
            data = dict(row)
            try:
                data["metadata"] = json.loads(data["metadata"]) if data["metadata"] else {}
            except json.JSONDecodeError:
                data["metadata"] = {}
            result.append(data)
        return result

    def close(self) -> None:
        self.conn.close()
