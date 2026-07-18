"""Purged walk-forward validation over causal, net-of-cost trade outcomes."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import polars as pl

from bongus.core.config import WF_MAX_DRAWDOWN_PCT, WF_MIN_UTILIZATION
from bongus.engine.analytics import compute_trade_summary
from bongus.engine.state_store import ParameterPromotion, StateWriter, ValidationSnapshot
from bongus.strategies.strategy import StrategyParameters, run_strategy


@dataclass(frozen=True, slots=True)
class AcceptanceGates:
    min_avg_oos_edge: float = 0.0
    min_windows_passing: int = 2
    min_trades_per_window: int = 10
    min_signal_to_noise: float = 0.1


@dataclass(frozen=True, slots=True)
class WindowResult:
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    embargo_rows: int
    trades: int
    avg_realized_edge: float
    avg_signal_to_noise: float
    utilization: float
    max_drawdown_pct: float
    selected_entry_ann_funding: float
    selected_entry_premium: float
    train_trades: int
    train_avg_realized_edge: float
    passed: bool
    net_pnl_path: tuple[float, ...] = ()


@dataclass(frozen=True, slots=True)
class _Outcome:
    trades: int
    avg_net_edge: float
    signal_to_noise: float
    utilization: float
    max_drawdown_pct: float
    net_pnl_path: tuple[float, ...]

    @property
    def total_net_edge(self) -> float:
        return sum(self.net_pnl_path)


def _normalize_param_keys(parameters: dict[str, Any]) -> dict[str, Any]:
    mapping = {
        "ENTRY_ANN_FUNDING_THRESHOLD": "entry_ann_funding_threshold",
        "ENTRY_PREMIUM_THRESHOLD": "entry_premium_threshold",
        "EXIT_ANN_FUNDING_THRESHOLD": "exit_ann_funding_threshold",
        "EXIT_DISCOUNT_THRESHOLD": "exit_discount_threshold",
        "NOTIONAL_PER_TRADE": "notional_per_trade",
        "MAX_NOTIONAL_PER_TRADE": "max_notional_per_trade",
        "MIN_TOP_N": "min_top_n",
        "MAX_TOP_N": "max_top_n",
        "SCANNER_MIN_DEPTH_MULTIPLIER": "scanner_min_depth_multiplier",
        "ROTATION_MAX_PAYBACK_DAYS": "rotation_max_payback_days",
    }
    return {
        mapping.get(key, key.lower()): value for key, value in parameters.items()
    }


def _window_slices(
    df: pl.DataFrame,
    train_rows: int,
    test_rows: int,
    step_rows: int,
    embargo_rows: int,
) -> list[tuple[int, int, int, int]]:
    if min(train_rows, test_rows, step_rows) <= 0 or embargo_rows < 0:
        raise ValueError("window sizes must be positive and embargo_rows non-negative")

    windows: list[tuple[int, int, int, int]] = []
    start = 0
    while start + train_rows + embargo_rows + test_rows <= df.height:
        train_end = start + train_rows
        test_start = train_end + embargo_rows
        test_end = test_start + test_rows
        windows.append((start, train_end, test_start, test_end))
        start += step_rows
    return windows


def _max_drawdown(path: Sequence[float]) -> float:
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for realized_return in path:
        equity += realized_return
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    return max_drawdown


def _signal_to_noise(path: Sequence[float]) -> float:
    if len(path) < 2:
        return 0.0
    mean = sum(path) / len(path)
    variance = sum((value - mean) ** 2 for value in path) / (len(path) - 1)
    standard_deviation = math.sqrt(max(variance, 0.0))
    if standard_deviation <= 1e-15:
        return 1_000_000.0 if mean > 0 else (-1_000_000.0 if mean < 0 else 0.0)
    return mean / standard_deviation


def _run_parameters(df: pl.DataFrame, parameters: StrategyParameters) -> _Outcome:
    annotated = run_strategy(
        df,
        parameters=parameters,
        force_close_at_end=True,
    )
    summary = compute_trade_summary(annotated)
    path = tuple(float(value) for value in summary["net_pnl_pct"].to_list())
    utilization_raw = annotated.select(pl.col("in_position").mean()).item()
    utilization = (
        float(utilization_raw)
        if isinstance(utilization_raw, (int, float))
        else 0.0
    )
    return _Outcome(
        trades=len(path),
        avg_net_edge=(sum(path) / len(path)) if path else 0.0,
        signal_to_noise=_signal_to_noise(path),
        utilization=utilization,
        max_drawdown_pct=_max_drawdown(path),
        net_pnl_path=path,
    )


def _default_candidate_grid() -> tuple[StrategyParameters, ...]:
    baseline = StrategyParameters()
    return (
        replace(
            baseline,
            entry_ann_funding_threshold=baseline.entry_ann_funding_threshold * 0.75,
            entry_premium_threshold=baseline.entry_premium_threshold * 0.75,
        ),
        baseline,
        replace(
            baseline,
            entry_ann_funding_threshold=baseline.entry_ann_funding_threshold * 1.50,
            entry_premium_threshold=baseline.entry_premium_threshold * 1.50,
        ),
    )


def _select_on_train(
    train: pl.DataFrame,
    candidates: Sequence[StrategyParameters],
) -> tuple[StrategyParameters, _Outcome]:
    if not candidates:
        raise ValueError("at least one candidate parameter set is required")

    evaluated = [(candidate, _run_parameters(train, candidate)) for candidate in candidates]

    # A small lower-confidence penalty discourages choosing a one-trade fluke.
    # Candidate generation is fixed before the window; only training outcomes
    # participate in this selection.
    def selection_score(item: tuple[StrategyParameters, _Outcome]) -> tuple[float, float, int]:
        _, outcome = item
        if outcome.trades == 0:
            return (-math.inf, -math.inf, 0)
        uncertainty_penalty = (
            abs(outcome.avg_net_edge)
            / math.sqrt(outcome.trades)
            if outcome.trades < 4
            else 0.0
        )
        return (
            outcome.avg_net_edge - uncertainty_penalty,
            outcome.total_net_edge,
            outcome.trades,
        )

    return max(evaluated, key=selection_score)


def _evaluate_window(
    train: pl.DataFrame,
    test: pl.DataFrame,
    gates: AcceptanceGates,
    *,
    embargo_rows: int,
    candidates: Sequence[StrategyParameters],
) -> WindowResult:
    selected, train_outcome = _select_on_train(train, candidates)
    test_outcome = _run_parameters(test, selected)
    passed = (
        test_outcome.trades >= gates.min_trades_per_window
        and test_outcome.avg_net_edge >= gates.min_avg_oos_edge
        and test_outcome.signal_to_noise >= gates.min_signal_to_noise
    )
    return WindowResult(
        train_start=str(train["timestamp"].min()),
        train_end=str(train["timestamp"].max()),
        test_start=str(test["timestamp"].min()),
        test_end=str(test["timestamp"].max()),
        embargo_rows=embargo_rows,
        trades=test_outcome.trades,
        avg_realized_edge=test_outcome.avg_net_edge,
        avg_signal_to_noise=test_outcome.signal_to_noise,
        utilization=test_outcome.utilization,
        max_drawdown_pct=test_outcome.max_drawdown_pct,
        selected_entry_ann_funding=selected.entry_ann_funding_threshold,
        selected_entry_premium=selected.entry_premium_threshold,
        train_trades=train_outcome.trades,
        train_avg_realized_edge=train_outcome.avg_net_edge,
        passed=passed,
        net_pnl_path=test_outcome.net_pnl_path,
    )


def run_walk_forward_validation(
    df: pl.DataFrame,
    gates: AcceptanceGates | None = None,
    train_rows: int = 30 * 24 * 60,
    test_rows: int = 7 * 24 * 60,
    step_rows: int = 7 * 24 * 60,
    *,
    embargo_rows: int = 60,
    candidates: Sequence[StrategyParameters] | None = None,
) -> dict[str, Any]:
    """Select on each training window and score frozen parameters out of sample."""
    required = {
        "timestamp",
        "spot_close",
        "perp_close",
        "funding_rate",
        "funding_snapshot",
    }
    missing = required.difference(df.columns)
    if missing:
        raise ValueError(f"walk-forward data missing columns: {sorted(missing)}")

    gates = gates or AcceptanceGates()
    data = df.sort("timestamp")
    candidate_grid = tuple(candidates or _default_candidate_grid())
    windows = _window_slices(
        data,
        train_rows=train_rows,
        test_rows=test_rows,
        step_rows=step_rows,
        embargo_rows=embargo_rows,
    )
    results: list[WindowResult] = []
    for train_start, train_end, test_start, test_end in windows:
        train = data.slice(train_start, train_end - train_start)
        test = data.slice(test_start, test_end - test_start)
        results.append(
            _evaluate_window(
                train,
                test,
                gates,
                embargo_rows=embargo_rows,
                candidates=candidate_grid,
            )
        )

    passing = sum(result.passed for result in results)
    total_trades = sum(result.trades for result in results)
    out_of_sample_path = tuple(
        value for result in results for value in result.net_pnl_path
    )
    return {
        "windows": len(results),
        "windows_passing": passing,
        "accepted": bool(results) and passing >= gates.min_windows_passing,
        "results": results,
        "avg_utilization": (
            sum(result.utilization for result in results) / len(results)
            if results
            else 0.0
        ),
        "max_drawdown_pct": _max_drawdown(out_of_sample_path),
        "total_trades": total_trades,
        "avg_oos_net_edge": (
            sum(out_of_sample_path) / len(out_of_sample_path)
            if out_of_sample_path
            else 0.0
        ),
        "embargo_rows": embargo_rows,
    }


def govern_walk_forward_result(
    parameters: dict[str, Any],
    summary: dict[str, Any],
    *,
    writer: StateWriter | None = None,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    """Record a promotion proposal without mutating live configuration.

    Passing an offline validation gate is evidence for human review, not
    authority to increase live risk.  Deployment is intentionally a separate,
    audited workflow.
    """
    writer = writer or StateWriter()
    normalized_params = _normalize_param_keys(parameters)
    accepted = bool(summary.get("accepted"))
    utilization_ok = (
        float(summary.get("avg_utilization", 0.0)) >= WF_MIN_UTILIZATION
    )
    drawdown_ok = (
        float(summary.get("max_drawdown_pct", 0.0)) <= WF_MAX_DRAWDOWN_PCT
    )
    go_no_go = "GO" if accepted and utilization_ok and drawdown_ok else "NO_GO"
    blockers: list[str] = []
    if not accepted:
        blockers.append("insufficient_windows")
    if not utilization_ok:
        blockers.append("low_utilization")
    if not drawdown_ok:
        blockers.append("drawdown_limit")

    metrics = {
        "windows": summary.get("windows", 0),
        "windows_passing": summary.get("windows_passing", 0),
        "avg_utilization": summary.get("avg_utilization", 0.0),
        "max_drawdown_pct": summary.get("max_drawdown_pct", 0.0),
        "total_trades": summary.get("total_trades", 0),
        "avg_oos_net_edge": summary.get("avg_oos_net_edge", 0.0),
        "parameters": normalized_params,
    }
    snapshot_time = datetime.now(timezone.utc).isoformat()
    snapshot = ValidationSnapshot(
        phase="walk_forward",
        validation_status="candidate" if go_no_go == "GO" else "rejected",
        go_no_go=go_no_go,
        observation_days=float(summary.get("windows", 0) or 0.0) * 7.0,
        trade_count=int(summary.get("total_trades", 0) or 0),
        blockers=blockers,
        metrics=metrics,
        snapshot_time=snapshot_time,
    )
    writer.record_validation_snapshot(snapshot)

    promotion_status = "proposed" if go_no_go == "GO" else "rejected"
    writer.record_parameter_promotion(
        ParameterPromotion(
            status=promotion_status,
            params=normalized_params,
            validation_snapshot_time=snapshot.snapshot_time,
            rollback_reason="" if go_no_go == "GO" else ",".join(blockers),
            metadata={
                "target_config_path": str(config_path) if config_path else None,
                "requires_operator_approval": True,
                "live_config_mutated": False,
            },
        )
    )
    return {
        "go_no_go": go_no_go,
        "promotion_status": promotion_status,
        "blockers": blockers,
        "parameters": normalized_params,
        "live_config_mutated": False,
    }
