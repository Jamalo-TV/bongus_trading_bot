"""Point-in-time feature storage, drift checks and a simple funding benchmark."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import sqlite3
from statistics import fmean, pstdev
import threading
from typing import Any, Iterable, Mapping, Sequence

import numpy as np


UTC = timezone.utc


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


@dataclass(frozen=True, slots=True)
class MarketFeatureInput:
    symbol: str
    available_at: datetime
    raw_funding_rate: float
    predicted_funding_rate: float
    premium_index: float
    mark_price: float
    index_price: float
    basis_pct: float
    book_imbalance: float
    realized_volatility: float
    open_interest: float | None
    prior_open_interest: float | None
    minutes_to_settlement: float
    funding_interval_hours: float
    cross_sectional_rates: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FeatureRecord:
    symbol: str
    event_time: datetime
    available_at: datetime
    source_event_id: str
    features: Mapping[str, float | None]
    feature_version: str = "funding-rich-v1"


@dataclass(frozen=True, slots=True)
class DriftReport:
    drifted: bool
    feature_reports: dict[str, dict[str, float | bool]]
    blockers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FundingModelPrediction:
    mean_rate: float
    standard_deviation: float
    lower_rate: float
    upper_rate: float
    model_hash: str


def build_rich_funding_features(values: MarketFeatureInput) -> dict[str, float | None]:
    numeric = {
        "raw_funding_rate": values.raw_funding_rate,
        "predicted_funding_rate": values.predicted_funding_rate,
        "premium_index": values.premium_index,
        "mark_price": values.mark_price,
        "index_price": values.index_price,
        "basis_pct": values.basis_pct,
        "book_imbalance": values.book_imbalance,
        "realized_volatility": values.realized_volatility,
        "minutes_to_settlement": values.minutes_to_settlement,
        "funding_interval_hours": values.funding_interval_hours,
    }
    if any(not math.isfinite(float(value)) for value in numeric.values()):
        raise ValueError("rich funding inputs must be finite")
    if values.mark_price <= 0.0 or values.index_price <= 0.0:
        raise ValueError("mark and index prices must be positive")
    cross_section = [
        float(value)
        for value in values.cross_sectional_rates.values()
        if math.isfinite(float(value))
    ]
    cross_mean = fmean(cross_section) if cross_section else values.raw_funding_rate
    cross_sigma = pstdev(cross_section) if len(cross_section) >= 2 else 0.0
    cross_z = (
        (values.raw_funding_rate - cross_mean) / cross_sigma
        if cross_sigma > 1e-12
        else 0.0
    )
    oi_change = None
    if (
        values.open_interest is not None
        and values.prior_open_interest is not None
        and math.isfinite(values.open_interest)
        and math.isfinite(values.prior_open_interest)
        and values.prior_open_interest > 0.0
    ):
        oi_change = values.open_interest / values.prior_open_interest - 1.0
    elapsed_fraction = 1.0 - min(
        1.0,
        max(
            0.0,
            values.minutes_to_settlement / max(1e-9, values.funding_interval_hours * 60.0),
        ),
    )
    return {
        "raw_funding_rate": values.raw_funding_rate,
        "predicted_funding_rate": values.predicted_funding_rate,
        "funding_prediction_gap": values.predicted_funding_rate - values.raw_funding_rate,
        "premium_index": values.premium_index,
        "mark_index_spread_pct": values.mark_price / values.index_price - 1.0,
        "basis_pct": values.basis_pct,
        "book_imbalance": max(-1.0, min(1.0, values.book_imbalance)),
        "realized_volatility": max(0.0, values.realized_volatility),
        "open_interest_change": oi_change,
        "cross_section_funding_zscore": cross_z,
        "cross_section_funding_mean": cross_mean,
        "settlement_elapsed_fraction": elapsed_fraction,
        "minutes_to_settlement": max(0.0, values.minutes_to_settlement),
        "funding_interval_hours": values.funding_interval_hours,
    }


_SCHEMA = """
CREATE TABLE IF NOT EXISTS point_in_time_features (
    record_key TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    symbol TEXT NOT NULL,
    event_time TEXT NOT NULL,
    available_at TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    feature_version TEXT NOT NULL,
    features_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_point_in_time_features_asof
ON point_in_time_features(symbol, feature_version, available_at DESC, event_time DESC);
"""


class PointInTimeFeatureStore:
    def __init__(self, db_path: str = "state.db") -> None:
        self.conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.executescript(_SCHEMA)
        self._lock = threading.RLock()

    def close(self) -> None:
        self.conn.close()

    @staticmethod
    def _normalize(record: FeatureRecord) -> dict[str, Any]:
        symbol = record.symbol.strip().upper()
        if not symbol or not record.source_event_id.strip() or not record.feature_version.strip():
            raise ValueError("feature symbol, source identity and version are required")
        event_time = _utc(record.event_time)
        available_at = _utc(record.available_at)
        if event_time > available_at:
            raise ValueError("feature event_time cannot be after availability")
        features: dict[str, float | None] = {}
        for name, value in record.features.items():
            normalized_name = str(name).strip()
            if not normalized_name or normalized_name.startswith(("future_", "target_", "label_")):
                raise ValueError("future/target/label values cannot be decision features")
            if value is None:
                features[normalized_name] = None
                continue
            numeric = float(value)
            if not math.isfinite(numeric):
                raise ValueError(f"feature {normalized_name} must be finite")
            features[normalized_name] = numeric
        return {
            "symbol": symbol,
            "event_time": event_time.isoformat(),
            "available_at": available_at.isoformat(),
            "source_event_id": record.source_event_id.strip(),
            "feature_version": record.feature_version.strip(),
            "features": features,
        }

    def append(self, record: FeatureRecord) -> bool:
        payload = self._normalize(record)
        record_key = hashlib.sha256(
            (
                f"{payload['symbol']}|{payload['feature_version']}|"
                f"{payload['source_event_id']}"
            ).encode()
        ).hexdigest()
        content_hash = hashlib.sha256(_canonical_json(payload).encode()).hexdigest()
        with self._lock:
            row = self.conn.execute(
                "SELECT content_hash FROM point_in_time_features WHERE record_key=?",
                (record_key,),
            ).fetchone()
            if row is not None:
                if str(row["content_hash"]) != content_hash:
                    raise ValueError("feature source identity content collision")
                return False
            self.conn.execute(
                """INSERT INTO point_in_time_features
                   (record_key, content_hash, symbol, event_time, available_at,
                    source_event_id, feature_version, features_json, recorded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record_key,
                    content_hash,
                    payload["symbol"],
                    payload["event_time"],
                    payload["available_at"],
                    payload["source_event_id"],
                    payload["feature_version"],
                    _canonical_json(payload["features"]),
                    datetime.now(UTC).isoformat(),
                ),
            )
            self.conn.commit()
        return True

    def as_of(
        self,
        symbol: str,
        decision_time: datetime,
        *,
        feature_version: str = "funding-rich-v1",
        max_age: timedelta | None = None,
    ) -> FeatureRecord | None:
        decision_time = _utc(decision_time)
        row = self.conn.execute(
            """SELECT * FROM point_in_time_features
               WHERE symbol=? AND feature_version=? AND available_at<=?
               ORDER BY available_at DESC, event_time DESC, record_key DESC LIMIT 1""",
            (symbol.upper(), feature_version, decision_time.isoformat()),
        ).fetchone()
        if row is None:
            return None
        available_at = datetime.fromisoformat(str(row["available_at"]))
        if max_age is not None and decision_time - available_at > max_age:
            return None
        return FeatureRecord(
            symbol=str(row["symbol"]),
            event_time=datetime.fromisoformat(str(row["event_time"])),
            available_at=available_at,
            source_event_id=str(row["source_event_id"]),
            features=json.loads(str(row["features_json"])),
            feature_version=str(row["feature_version"]),
        )


class FeatureDriftMonitor:
    def __init__(
        self,
        *,
        standardized_mean_limit: float = 0.50,
        variance_ratio_limit: float = 4.0,
        missing_rate_increase_limit: float = 0.10,
    ) -> None:
        self.standardized_mean_limit = standardized_mean_limit
        self.variance_ratio_limit = variance_ratio_limit
        self.missing_rate_increase_limit = missing_rate_increase_limit

    @staticmethod
    def _finite_values(
        rows: Sequence[Mapping[str, float | None]], name: str
    ) -> list[float]:
        values: list[float] = []
        for row in rows:
            value = row.get(name)
            if value is None:
                continue
            numeric = float(value)
            if math.isfinite(numeric):
                values.append(numeric)
        return values

    def evaluate(
        self,
        reference: Sequence[Mapping[str, float | None]],
        current: Sequence[Mapping[str, float | None]],
    ) -> DriftReport:
        if not reference or not current:
            return DriftReport(True, {}, ("insufficient_drift_samples",))
        feature_names = sorted(
            set().union(*(row.keys() for row in reference), *(row.keys() for row in current))
        )
        reports: dict[str, dict[str, float | bool]] = {}
        blockers: list[str] = []
        for name in feature_names:
            reference_values = self._finite_values(reference, name)
            current_values = self._finite_values(current, name)
            reference_missing = 1.0 - len(reference_values) / len(reference)
            current_missing = 1.0 - len(current_values) / len(current)
            mean_shift = math.inf
            variance_ratio = math.inf
            if reference_values and current_values:
                reference_mean = fmean(reference_values)
                current_mean = fmean(current_values)
                reference_sigma = pstdev(reference_values) if len(reference_values) > 1 else 0.0
                current_sigma = pstdev(current_values) if len(current_values) > 1 else 0.0
                pooled = max(1e-12, math.sqrt((reference_sigma**2 + current_sigma**2) / 2.0))
                mean_shift = abs(current_mean - reference_mean) / pooled
                variance_ratio = (
                    max(reference_sigma**2, current_sigma**2)
                    / max(1e-12, min(reference_sigma**2, current_sigma**2))
                )
            drifted = (
                mean_shift > self.standardized_mean_limit
                or variance_ratio > self.variance_ratio_limit
                or current_missing - reference_missing > self.missing_rate_increase_limit
            )
            reports[name] = {
                "standardized_mean_shift": mean_shift,
                "variance_ratio": variance_ratio,
                "reference_missing_rate": reference_missing,
                "current_missing_rate": current_missing,
                "drifted": drifted,
            }
            if drifted:
                blockers.append(f"feature_drift:{name}")
        return DriftReport(bool(blockers), reports, tuple(blockers))


class CalibratedLinearFundingModel:
    """Ridge baseline whose complexity is bounded and artifact is hashable."""

    def __init__(self, feature_names: Sequence[str], *, ridge: float = 1e-6) -> None:
        if not feature_names or len(set(feature_names)) != len(feature_names):
            raise ValueError("feature_names must be non-empty and unique")
        if ridge < 0.0:
            raise ValueError("ridge must be non-negative")
        self.feature_names = tuple(feature_names)
        self.ridge = float(ridge)
        self.coefficients: np.ndarray | None = None
        self.residual_sigma = math.nan
        self.model_hash = ""

    def _matrix(self, rows: Sequence[Mapping[str, float | None]]) -> np.ndarray:
        matrix: list[list[float]] = []
        for row in rows:
            values: list[float] = [1.0]
            for name in self.feature_names:
                value = row.get(name)
                if value is None or not math.isfinite(float(value)):
                    raise ValueError(f"model feature {name} is missing or non-finite")
                values.append(float(value))
            matrix.append(values)
        return np.asarray(matrix, dtype=float)

    def fit(
        self,
        rows: Sequence[Mapping[str, float | None]],
        labels: Sequence[float],
    ) -> str:
        if len(rows) != len(labels) or len(rows) < len(self.feature_names) + 3:
            raise ValueError("insufficient aligned samples for funding model")
        design = self._matrix(rows)
        targets = np.asarray(labels, dtype=float)
        if not np.all(np.isfinite(targets)):
            raise ValueError("funding labels must be finite")
        penalty = np.eye(design.shape[1]) * self.ridge
        penalty[0, 0] = 0.0
        self.coefficients = np.linalg.solve(design.T @ design + penalty, design.T @ targets)
        residuals = targets - design @ self.coefficients
        self.residual_sigma = max(1e-12, float(np.std(residuals, ddof=1)))
        artifact = {
            "feature_names": self.feature_names,
            "ridge": self.ridge,
            "coefficients": [float(value) for value in self.coefficients],
            "residual_sigma": self.residual_sigma,
        }
        self.model_hash = hashlib.sha256(_canonical_json(artifact).encode()).hexdigest()
        return self.model_hash

    def predict(
        self,
        row: Mapping[str, float | None],
        *,
        z: float = 1.6448536269514722,
    ) -> FundingModelPrediction:
        if self.coefficients is None or not self.model_hash:
            raise ValueError("funding model has not been fitted")
        vector = self._matrix([row])[0]
        mean = float(vector @ self.coefficients)
        return FundingModelPrediction(
            mean_rate=mean,
            standard_deviation=self.residual_sigma,
            lower_rate=mean - z * self.residual_sigma,
            upper_rate=mean + z * self.residual_sigma,
            model_hash=self.model_hash,
        )


def purged_walk_forward_splits(
    decision_times: Sequence[datetime],
    label_end_times: Sequence[datetime],
    *,
    minimum_train_size: int,
    test_size: int,
    embargo: timedelta,
) -> list[tuple[tuple[int, ...], tuple[int, ...]]]:
    if len(decision_times) != len(label_end_times):
        raise ValueError("decision and label-end times must align")
    if minimum_train_size <= 0 or test_size <= 0 or embargo < timedelta(0):
        raise ValueError("split sizes must be positive and embargo non-negative")
    decisions = [_utc(value) for value in decision_times]
    label_ends = [_utc(value) for value in label_end_times]
    if decisions != sorted(decisions):
        raise ValueError("decision times must be chronological")
    splits: list[tuple[tuple[int, ...], tuple[int, ...]]] = []
    test_start_index = minimum_train_size
    while test_start_index < len(decisions):
        test_indices = tuple(
            range(test_start_index, min(len(decisions), test_start_index + test_size))
        )
        cutoff = decisions[test_indices[0]] - embargo
        train_indices = tuple(
            index
            for index in range(test_start_index)
            if label_ends[index] < cutoff
        )
        if len(train_indices) >= minimum_train_size:
            splits.append((train_indices, test_indices))
        test_start_index += test_size
    return splits
