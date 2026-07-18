"""Immutable experiment registry with deterministic cohorts and hard promotion gates."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from statistics import NormalDist, fmean, variance
import threading
from typing import Any, Iterable, Literal, Mapping


UTC = timezone.utc
ExperimentStatus = Literal[
    "PREREGISTERED",
    "RUNNING",
    "INSUFFICIENT_DATA",
    "SAMPLE_RATIO_MISMATCH",
    "FAILED",
    "PASSED",
    "PROMOTED",
]


class ExperimentRegistryError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MetricDefinition:
    name: str
    higher_is_better: bool
    minimum_effect: float = 0.0
    is_guardrail: bool = False
    maximum_adverse_effect: float = 0.0


@dataclass(frozen=True, slots=True)
class ExperimentManifest:
    experiment_id: str
    hypothesis: str
    primary_metric: str
    metrics: tuple[MetricDefinition, ...]
    data_checksums: Mapping[str, str]
    code_version: str
    config_hash: str
    model_hash: str
    random_seed: int
    minimum_samples_per_arm: int
    treatment_allocation: float = 0.50
    familywise_alpha: float = 0.05
    maximum_sequential_looks: int = 5
    cohort_namespace: str = "eligible-decision"
    rollout_scope: str = "shadow"
    created_at: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ExperimentEvaluation:
    experiment_id: str
    status: ExperimentStatus
    control_count: int
    treatment_count: int
    sample_ratio_p_value: float
    primary_effect: float
    primary_p_value: float
    adjusted_alpha: float
    sequential_look: int
    blockers: tuple[str, ...]
    metric_results: dict[str, dict[str, float | int | bool]]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments (
    experiment_id TEXT PRIMARY KEY,
    manifest_hash TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    status TEXT NOT NULL,
    sequential_looks INTEGER NOT NULL DEFAULT 0,
    registered_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS experiment_assignments (
    experiment_id TEXT NOT NULL,
    unit_id TEXT NOT NULL,
    cohort TEXT NOT NULL,
    assignment_hash TEXT NOT NULL,
    assigned_at TEXT NOT NULL,
    PRIMARY KEY(experiment_id, unit_id),
    FOREIGN KEY(experiment_id) REFERENCES experiments(experiment_id)
);

CREATE TABLE IF NOT EXISTS experiment_observations (
    experiment_id TEXT NOT NULL,
    observation_id TEXT NOT NULL,
    unit_id TEXT NOT NULL,
    cohort TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    metric_value REAL NOT NULL,
    observed_at TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    PRIMARY KEY(experiment_id, observation_id, metric_name),
    FOREIGN KEY(experiment_id) REFERENCES experiments(experiment_id)
);

CREATE TABLE IF NOT EXISTS experiment_promotions (
    promotion_id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL,
    artifact_hash TEXT NOT NULL,
    target_scope TEXT NOT NULL,
    evaluation_json TEXT NOT NULL,
    promoted_at TEXT NOT NULL,
    FOREIGN KEY(experiment_id) REFERENCES experiments(experiment_id)
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def checksum_file(path: str | Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def checksum_files(paths: Iterable[str | Path]) -> dict[str, str]:
    return {
        str(Path(path).resolve()): checksum_file(path)
        for path in sorted((Path(item) for item in paths), key=lambda item: str(item.resolve()))
    }


class ExperimentRegistry:
    def __init__(self, db_path: str = "state.db") -> None:
        self.conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(_SCHEMA)
        self._lock = threading.RLock()

    def close(self) -> None:
        self.conn.close()

    @staticmethod
    def _normalize_manifest(manifest: ExperimentManifest) -> dict[str, Any]:
        payload = asdict(manifest)
        payload["experiment_id"] = manifest.experiment_id.strip()
        payload["hypothesis"] = manifest.hypothesis.strip()
        payload["primary_metric"] = manifest.primary_metric.strip()
        payload["created_at"] = manifest.created_at or _now()
        payload["data_checksums"] = dict(sorted(manifest.data_checksums.items()))
        payload["metadata"] = dict(manifest.metadata)
        return payload

    @staticmethod
    def _validate_manifest(payload: dict[str, Any]) -> None:
        if not payload["experiment_id"] or not payload["hypothesis"]:
            raise ExperimentRegistryError("experiment_id and hypothesis are required")
        metrics = payload["metrics"]
        metric_names = [str(item["name"]).strip() for item in metrics]
        if not metric_names or len(set(metric_names)) != len(metric_names):
            raise ExperimentRegistryError("metric names must be present and unique")
        if payload["primary_metric"] not in metric_names:
            raise ExperimentRegistryError("primary_metric must be preregistered")
        if int(payload["minimum_samples_per_arm"]) < 2:
            raise ExperimentRegistryError("minimum_samples_per_arm must be at least two")
        if not 0.0 < float(payload["treatment_allocation"]) < 1.0:
            raise ExperimentRegistryError("treatment_allocation must be in (0, 1)")
        if not 0.0 < float(payload["familywise_alpha"]) < 0.5:
            raise ExperimentRegistryError("familywise_alpha must be in (0, 0.5)")
        if int(payload["maximum_sequential_looks"]) < 1:
            raise ExperimentRegistryError("maximum_sequential_looks must be positive")
        required_hashes = ("code_version", "config_hash", "model_hash")
        if any(not str(payload[name]).strip() for name in required_hashes):
            raise ExperimentRegistryError("code/config/model versions are required")
        if not payload["data_checksums"]:
            raise ExperimentRegistryError("at least one immutable data checksum is required")
        for name, digest in payload["data_checksums"].items():
            if not str(name).strip() or len(str(digest)) != 64:
                raise ExperimentRegistryError("data checksums must be named SHA-256 values")

    def register(self, manifest: ExperimentManifest) -> str:
        payload = self._normalize_manifest(manifest)
        self._validate_manifest(payload)
        # Registration time is audit metadata, not scientific experiment
        # content.  Excluding it makes an identical retry idempotent while all
        # preregistered assumptions remain immutable.
        hash_payload = dict(payload)
        hash_payload.pop("created_at", None)
        manifest_hash = _sha256_json(hash_payload)
        manifest_json = _canonical_json(payload)
        now = _now()
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                row = self.conn.execute(
                    "SELECT manifest_hash FROM experiments WHERE experiment_id = ?",
                    (payload["experiment_id"],),
                ).fetchone()
                if row is not None:
                    if str(row["manifest_hash"]) != manifest_hash:
                        raise ExperimentRegistryError(
                            "experiment manifest is immutable; use a new experiment_id"
                        )
                    self.conn.commit()
                    return manifest_hash
                self.conn.execute(
                    """INSERT INTO experiments
                       (experiment_id, manifest_hash, manifest_json, status,
                        registered_at, updated_at)
                       VALUES (?, ?, ?, 'PREREGISTERED', ?, ?)""",
                    (
                        payload["experiment_id"],
                        manifest_hash,
                        manifest_json,
                        now,
                        now,
                    ),
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return manifest_hash

    def _manifest(self, experiment_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT manifest_json FROM experiments WHERE experiment_id = ?",
            (experiment_id,),
        ).fetchone()
        if row is None:
            raise ExperimentRegistryError("experiment is not preregistered")
        return json.loads(str(row["manifest_json"]))

    def assign(
        self,
        experiment_id: str,
        unit_id: str,
        *,
        now: datetime | None = None,
    ) -> str:
        if not unit_id.strip():
            raise ExperimentRegistryError("unit_id is required")
        manifest = self._manifest(experiment_id)
        assignment_hash = hashlib.sha256(
            (
                f"{experiment_id}|{manifest['cohort_namespace']}|"
                f"{manifest['random_seed']}|{unit_id.strip()}"
            ).encode()
        ).hexdigest()
        bucket = int(assignment_hash[:16], 16) / float(0xFFFFFFFFFFFFFFFF)
        cohort = (
            "treatment"
            if bucket < float(manifest["treatment_allocation"])
            else "control"
        )
        assigned_at = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                row = self.conn.execute(
                    """SELECT cohort, assignment_hash FROM experiment_assignments
                       WHERE experiment_id = ? AND unit_id = ?""",
                    (experiment_id, unit_id.strip()),
                ).fetchone()
                if row is not None:
                    if str(row["assignment_hash"]) != assignment_hash:
                        raise ExperimentRegistryError("assignment hash collision")
                    self.conn.commit()
                    return str(row["cohort"])
                self.conn.execute(
                    """INSERT INTO experiment_assignments
                       (experiment_id, unit_id, cohort, assignment_hash, assigned_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (experiment_id, unit_id.strip(), cohort, assignment_hash, assigned_at),
                )
                self.conn.execute(
                    "UPDATE experiments SET status='RUNNING', updated_at=? WHERE experiment_id=?",
                    (assigned_at, experiment_id),
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return cohort

    def observe(
        self,
        *,
        experiment_id: str,
        observation_id: str,
        unit_id: str,
        metric_name: str,
        value: float,
        observed_at: datetime,
        metadata: Mapping[str, Any] | None = None,
    ) -> bool:
        manifest = self._manifest(experiment_id)
        allowed_metrics = {str(item["name"]) for item in manifest["metrics"]}
        if metric_name not in allowed_metrics:
            raise ExperimentRegistryError("metric was not preregistered")
        if not observation_id.strip() or not unit_id.strip():
            raise ExperimentRegistryError("observation_id and unit_id are required")
        numeric = float(value)
        if not math.isfinite(numeric):
            raise ExperimentRegistryError("metric value must be finite")
        cohort = self.assign(experiment_id, unit_id)
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=UTC)
        payload = {
            "experiment_id": experiment_id,
            "observation_id": observation_id.strip(),
            "unit_id": unit_id.strip(),
            "cohort": cohort,
            "metric_name": metric_name,
            "metric_value": numeric,
            "observed_at": observed_at.astimezone(UTC).isoformat(),
            "metadata": dict(metadata or {}),
        }
        content_hash = _sha256_json(payload)
        with self._lock:
            row = self.conn.execute(
                """SELECT content_hash FROM experiment_observations
                   WHERE experiment_id=? AND observation_id=? AND metric_name=?""",
                (experiment_id, observation_id.strip(), metric_name),
            ).fetchone()
            if row is not None:
                if str(row["content_hash"]) != content_hash:
                    raise ExperimentRegistryError("observation identity content collision")
                return False
            self.conn.execute(
                """INSERT INTO experiment_observations
                   (experiment_id, observation_id, unit_id, cohort, metric_name,
                    metric_value, observed_at, content_hash, metadata_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    experiment_id,
                    observation_id.strip(),
                    unit_id.strip(),
                    cohort,
                    metric_name,
                    numeric,
                    observed_at.astimezone(UTC).isoformat(),
                    content_hash,
                    _canonical_json(dict(metadata or {})),
                ),
            )
            self.conn.commit()
        return True

    @staticmethod
    def _two_sided_p_value(z_score: float) -> float:
        return max(0.0, min(1.0, 2.0 * (1.0 - NormalDist().cdf(abs(z_score)))))

    @classmethod
    def _sample_ratio_p_value(
        cls,
        treatment_count: int,
        total_count: int,
        expected_allocation: float,
    ) -> float:
        if total_count <= 0:
            return 1.0
        expected = total_count * expected_allocation
        sigma = math.sqrt(total_count * expected_allocation * (1.0 - expected_allocation))
        if sigma <= 0.0:
            return 0.0
        return cls._two_sided_p_value((treatment_count - expected) / sigma)

    @classmethod
    def _metric_test(
        cls,
        control: list[float],
        treatment: list[float],
    ) -> tuple[float, float, float]:
        if len(control) < 2 or len(treatment) < 2:
            return math.nan, math.nan, 1.0
        effect = fmean(treatment) - fmean(control)
        control_var = variance(control)
        treatment_var = variance(treatment)
        standard_error = math.sqrt(
            control_var / len(control) + treatment_var / len(treatment)
        )
        if standard_error <= 0.0:
            p_value = 0.0 if effect != 0.0 else 1.0
        else:
            p_value = cls._two_sided_p_value(effect / standard_error)
        return effect, standard_error, p_value

    def evaluate(
        self,
        experiment_id: str,
        *,
        sample_ratio_alpha: float = 0.001,
        count_look: bool = True,
    ) -> ExperimentEvaluation:
        manifest = self._manifest(experiment_id)
        experiment_row = self.conn.execute(
            "SELECT sequential_looks FROM experiments WHERE experiment_id=?",
            (experiment_id,),
        ).fetchone()
        current_looks = int(experiment_row["sequential_looks"] if experiment_row else 0)
        look = current_looks + 1 if count_look else max(1, current_looks)
        maximum_looks = int(manifest["maximum_sequential_looks"])
        blockers: list[str] = []
        if look > maximum_looks:
            blockers.append("maximum_sequential_looks_exceeded")

        assignments = self.conn.execute(
            "SELECT cohort, COUNT(*) AS n FROM experiment_assignments WHERE experiment_id=? GROUP BY cohort",
            (experiment_id,),
        ).fetchall()
        counts = {str(row["cohort"]): int(row["n"]) for row in assignments}
        control_count = counts.get("control", 0)
        treatment_count = counts.get("treatment", 0)
        total_count = control_count + treatment_count
        srm_p = self._sample_ratio_p_value(
            treatment_count,
            total_count,
            float(manifest["treatment_allocation"]),
        )
        if srm_p < sample_ratio_alpha:
            blockers.append("sample_ratio_mismatch")
        minimum = int(manifest["minimum_samples_per_arm"])
        if control_count < minimum or treatment_count < minimum:
            blockers.append("minimum_sample_not_met")

        rows = self.conn.execute(
            """SELECT cohort, metric_name, metric_value FROM experiment_observations
               WHERE experiment_id=? ORDER BY observed_at, observation_id""",
            (experiment_id,),
        ).fetchall()
        observations: dict[str, dict[str, list[float]]] = {}
        for row in rows:
            observations.setdefault(str(row["metric_name"]), {}).setdefault(
                str(row["cohort"]), []
            ).append(float(row["metric_value"]))

        # Bonferroni controls the preregistered metric family; an equal alpha
        # spending schedule controls repeated sequential looks.
        metric_count = max(1, len(manifest["metrics"]))
        adjusted_alpha = float(manifest["familywise_alpha"]) / metric_count / maximum_looks
        metric_results: dict[str, dict[str, float | int | bool]] = {}
        primary_effect = math.nan
        primary_p = 1.0
        for metric in manifest["metrics"]:
            name = str(metric["name"])
            values = observations.get(name, {})
            control = values.get("control", [])
            treatment = values.get("treatment", [])
            effect, standard_error, p_value = self._metric_test(control, treatment)
            oriented_effect = effect if bool(metric["higher_is_better"]) else -effect
            significant = p_value <= adjusted_alpha
            passes_minimum = oriented_effect >= float(metric["minimum_effect"])
            metric_results[name] = {
                "control_count": len(control),
                "treatment_count": len(treatment),
                "effect": effect,
                "oriented_effect": oriented_effect,
                "standard_error": standard_error,
                "p_value": p_value,
                "significant": significant,
                "passes_minimum_effect": passes_minimum,
            }
            if name == manifest["primary_metric"]:
                primary_effect = effect
                primary_p = p_value
                if not significant:
                    blockers.append("primary_metric_not_significant")
                if not passes_minimum:
                    blockers.append("primary_minimum_effect_not_met")
            if bool(metric["is_guardrail"]):
                adverse = -oriented_effect
                if adverse > float(metric["maximum_adverse_effect"]):
                    blockers.append(f"guardrail_failed:{name}")

        if "sample_ratio_mismatch" in blockers:
            status: ExperimentStatus = "SAMPLE_RATIO_MISMATCH"
        elif "minimum_sample_not_met" in blockers:
            status = "INSUFFICIENT_DATA"
        elif blockers:
            status = "FAILED"
        else:
            status = "PASSED"
        evaluation = ExperimentEvaluation(
            experiment_id=experiment_id,
            status=status,
            control_count=control_count,
            treatment_count=treatment_count,
            sample_ratio_p_value=srm_p,
            primary_effect=primary_effect,
            primary_p_value=primary_p,
            adjusted_alpha=adjusted_alpha,
            sequential_look=look,
            blockers=tuple(dict.fromkeys(blockers)),
            metric_results=metric_results,
        )
        if count_look:
            self.conn.execute(
                "UPDATE experiments SET status=?, sequential_looks=?, updated_at=? WHERE experiment_id=?",
                (status, look, _now(), experiment_id),
            )
            self.conn.commit()
        return evaluation

    def promote(
        self,
        *,
        experiment_id: str,
        artifact_hash: str,
        target_scope: str,
        evaluation: ExperimentEvaluation | None = None,
    ) -> str:
        if len(artifact_hash.strip()) != 64:
            raise ExperimentRegistryError("promotion artifact must have a SHA-256 hash")
        evaluation = evaluation or self.evaluate(experiment_id, count_look=False)
        if evaluation.experiment_id != experiment_id or evaluation.status != "PASSED":
            raise ExperimentRegistryError("only a preregistered PASSED experiment can promote")
        manifest = self._manifest(experiment_id)
        rollout_scope = str(manifest["rollout_scope"]).lower()
        if target_scope.lower() not in {"shadow", "paper", "testnet"} and rollout_scope != "live-canary-approved":
            raise ExperimentRegistryError("manifest does not authorize a live promotion scope")
        payload = {
            "experiment_id": experiment_id,
            "artifact_hash": artifact_hash.strip(),
            "target_scope": target_scope.strip().lower(),
            "manifest_hash": _sha256_json(manifest),
        }
        promotion_id = "promotion-" + _sha256_json(payload)[:24]
        self.conn.execute(
            """INSERT OR IGNORE INTO experiment_promotions
               (promotion_id, experiment_id, artifact_hash, target_scope,
                evaluation_json, promoted_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                promotion_id,
                experiment_id,
                artifact_hash.strip(),
                target_scope.strip().lower(),
                _canonical_json(asdict(evaluation)),
                _now(),
            ),
        )
        self.conn.execute(
            "UPDATE experiments SET status='PROMOTED', updated_at=? WHERE experiment_id=?",
            (_now(), experiment_id),
        )
        self.conn.commit()
        return promotion_id
