"""Portfolio and validation metrics derived from SQLite state."""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from statistics import fmean, pstdev

from bongus.core.config import (
    VALIDATION_ADJUST_SHARPE_MIN,
    VALIDATION_GO_MAX_DRAWDOWN_PCT,
    VALIDATION_GO_MIN_INTERVENTION_FREE_DAYS,
    VALIDATION_GO_SHARPE_MIN,
    VALIDATION_NO_GO_MAX_DRAWDOWN_PCT,
    VALIDATION_TARGET_COST_MODEL_ERROR_MAX_PCT,
    VALIDATION_TARGET_MONTHLY_RETURN_MAX_PCT,
    VALIDATION_TARGET_MONTHLY_RETURN_MIN_PCT,
    VALIDATION_TARGET_UPTIME_MIN_PCT,
    VALIDATION_TARGET_WIN_RATE_MIN,
)
from bongus.engine.state_store import StateReader


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _annualized_sharpe(daily_returns: list[float]) -> float:
    if len(daily_returns) < 2:
        return 0.0
    sigma = pstdev(daily_returns)
    if sigma <= 1e-12:
        return 0.0
    return (fmean(daily_returns) / sigma) * math.sqrt(365.0)


def _max_drawdown_pct(account_equity: float, cumulative_pnls: list[float]) -> float:
    if account_equity <= 0.0 or not cumulative_pnls:
        return 0.0
    peak = account_equity
    max_drawdown = 0.0
    for pnl in cumulative_pnls:
        equity = account_equity + pnl
        peak = max(peak, equity)
        if peak > 0.0:
            max_drawdown = max(max_drawdown, (peak - equity) / peak)
    return max_drawdown


def _metric_status_min(value: float, target: float) -> str:
    return "pass" if value >= target else "warn"


def _metric_status_max(value: float, target: float) -> str:
    return "pass" if value <= target else "warn"


def _metric_status_band(value: float, target_min: float, target_max: float) -> str:
    return "pass" if target_min <= value <= target_max else "warn"


def calculate_metrics(reader: StateReader, trade_limit: int = 5000) -> dict:
    now = datetime.now(timezone.utc)
    trades = list(reversed(reader.get_trades(limit=trade_limit)))
    risk = reader.get_risk()
    stats = reader.get_stats()
    account_equity = _safe_float(
        risk.get("account_equity", stats.get("account_equity", 10_000.0)),
        10_000.0,
    )

    total_pnl = sum(_safe_float(trade.get("net_pnl_usd")) for trade in trades)
    trade_count = len(trades)
    win_count = sum(1 for trade in trades if _safe_float(trade.get("net_pnl_usd")) > 0.0)
    win_rate = (win_count / trade_count) if trade_count else 0.0

    daily_pnl: dict[str, float] = defaultdict(float)
    cumulative_pnls: list[float] = []
    running_pnl = 0.0
    thirty_days_ago = now - timedelta(days=30)
    monthly_pnl = 0.0
    trade_times: list[datetime] = []

    for trade in trades:
        pnl = _safe_float(trade.get("net_pnl_usd"))
        exit_dt = _parse_iso(trade.get("exit_time"))
        if exit_dt is not None:
            daily_pnl[exit_dt.date().isoformat()] += pnl
            if exit_dt >= thirty_days_ago:
                monthly_pnl += pnl
            trade_times.append(exit_dt)
        running_pnl += pnl
        cumulative_pnls.append(running_pnl)

    ordered_daily_returns = [
        pnl / account_equity
        for _, pnl in sorted(daily_pnl.items())
        if account_equity > 0.0
    ]
    sharpe = _annualized_sharpe(ordered_daily_returns)
    max_drawdown_pct = _max_drawdown_pct(account_equity, cumulative_pnls)
    monthly_return_pct = (monthly_pnl / account_equity) if account_equity > 0.0 else 0.0

    cost_samples = reader.get_health_samples(
        metric="cost_model_error_pct",
        since=(now - timedelta(days=30)).isoformat(),
        limit=10_000,
    )
    cost_model_error_pct = (
        fmean(abs(_safe_float(sample.get("value"))) for sample in cost_samples)
        if cost_samples
        else 0.0
    )

    uptime_samples = reader.get_health_samples(
        metric="loop_alive",
        since=(now - timedelta(days=7)).isoformat(),
        limit=20_000,
    )
    uptime_without_manual_intervention_pct = 0.0
    uptime_timestamps = sorted(
        dt for dt in (_parse_iso(sample.get("sample_time")) for sample in uptime_samples) if dt is not None
    )
    if uptime_timestamps:
        expected_minutes = max(
            1,
            int((uptime_timestamps[-1] - uptime_timestamps[0]).total_seconds() / 60.0) + 1,
        )
        unique_minutes = {dt.replace(second=0, microsecond=0) for dt in uptime_timestamps}
        uptime_without_manual_intervention_pct = min(
            100.0,
            (len(unique_minutes) / expected_minutes) * 100.0,
        )

    observation_start: datetime | None = min(trade_times) if trade_times else None
    if uptime_timestamps and (observation_start is None or uptime_timestamps[0] < observation_start):
        observation_start = uptime_timestamps[0]

    intervention_samples = reader.get_health_samples(
        metric="operator_intervention_required",
        since=(now - timedelta(days=90)).isoformat(),
        limit=5_000,
    )
    intervention_timestamps = sorted(
        dt
        for dt in (_parse_iso(sample.get("sample_time")) for sample in intervention_samples)
        if dt is not None
    )
    if intervention_timestamps and (observation_start is None or intervention_timestamps[0] < observation_start):
        observation_start = intervention_timestamps[0]

    last_intervention = intervention_timestamps[-1] if intervention_timestamps else None
    if last_intervention is None:
        for sample in uptime_samples:
            sample_mode = str(sample.get("runtime_mode") or "").upper()
            sample_alert = str(sample.get("alert_level") or "").lower()
            if sample_mode != "LIVE" or sample_alert in {"critical", "blocked"}:
                dt = _parse_iso(sample.get("sample_time"))
                if dt is not None and (last_intervention is None or dt > last_intervention):
                    last_intervention = dt

    intervention_free_days = 0.0
    if last_intervention is not None:
        intervention_free_days = (now - last_intervention).total_seconds() / 86400.0
    elif observation_start is not None:
        intervention_free_days = (now - observation_start).total_seconds() / 86400.0

    observation_days = (
        max(0.0, (now - observation_start).total_seconds() / 86400.0)
        if observation_start is not None
        else 0.0
    )

    target_checks = {
        "sharpe_ratio_annualized": {
            "value": sharpe,
            "goal_min": VALIDATION_GO_SHARPE_MIN,
            "adjust_floor": VALIDATION_ADJUST_SHARPE_MIN,
            "status": (
                "pass"
                if sharpe >= VALIDATION_GO_SHARPE_MIN
                else ("warn" if sharpe >= VALIDATION_ADJUST_SHARPE_MIN else "fail")
            ),
        },
        "max_drawdown_pct": {
            "value": max_drawdown_pct,
            "goal_max": VALIDATION_GO_MAX_DRAWDOWN_PCT,
            "fail_above": VALIDATION_NO_GO_MAX_DRAWDOWN_PCT,
            "status": (
                "pass"
                if max_drawdown_pct <= VALIDATION_GO_MAX_DRAWDOWN_PCT
                else ("warn" if max_drawdown_pct <= VALIDATION_NO_GO_MAX_DRAWDOWN_PCT else "fail")
            ),
        },
        "monthly_return_pct": {
            "value": monthly_return_pct,
            "goal_min": VALIDATION_TARGET_MONTHLY_RETURN_MIN_PCT,
            "goal_max": VALIDATION_TARGET_MONTHLY_RETURN_MAX_PCT,
            "status": _metric_status_band(
                monthly_return_pct,
                VALIDATION_TARGET_MONTHLY_RETURN_MIN_PCT,
                VALIDATION_TARGET_MONTHLY_RETURN_MAX_PCT,
            ),
        },
        "win_rate": {
            "value": win_rate,
            "goal_min": VALIDATION_TARGET_WIN_RATE_MIN,
            "status": _metric_status_min(win_rate, VALIDATION_TARGET_WIN_RATE_MIN),
        },
        "cost_model_error_pct": {
            "value": cost_model_error_pct,
            "goal_max": VALIDATION_TARGET_COST_MODEL_ERROR_MAX_PCT,
            "status": _metric_status_max(cost_model_error_pct, VALIDATION_TARGET_COST_MODEL_ERROR_MAX_PCT),
        },
        "uptime_without_manual_intervention_pct": {
            "value": uptime_without_manual_intervention_pct,
            "goal_min": VALIDATION_TARGET_UPTIME_MIN_PCT,
            "status": _metric_status_min(
                uptime_without_manual_intervention_pct,
                VALIDATION_TARGET_UPTIME_MIN_PCT,
            ),
        },
        "intervention_free_days": {
            "value": intervention_free_days,
            "goal_min": VALIDATION_GO_MIN_INTERVENTION_FREE_DAYS,
            "status": _metric_status_min(
                intervention_free_days,
                VALIDATION_GO_MIN_INTERVENTION_FREE_DAYS,
            ),
        },
    }

    no_go_reasons: list[str] = []
    target_misses: list[str] = []
    if sharpe < VALIDATION_ADJUST_SHARPE_MIN:
        no_go_reasons.append(
            f"Sharpe {sharpe:.2f} below NO-GO floor {VALIDATION_ADJUST_SHARPE_MIN:.2f}"
        )
    if max_drawdown_pct > VALIDATION_NO_GO_MAX_DRAWDOWN_PCT:
        no_go_reasons.append(
            f"Max drawdown {(max_drawdown_pct * 100):.2f}% above NO-GO limit {(VALIDATION_NO_GO_MAX_DRAWDOWN_PCT * 100):.2f}%"
        )

    if observation_days < VALIDATION_GO_MIN_INTERVENTION_FREE_DAYS:
        target_misses.append(
            f"Observation window only {observation_days:.1f}d; need {VALIDATION_GO_MIN_INTERVENTION_FREE_DAYS:.0f}d"
        )
    if sharpe < VALIDATION_GO_SHARPE_MIN:
        target_misses.append(
            f"Sharpe {sharpe:.2f} below GO target {VALIDATION_GO_SHARPE_MIN:.2f}"
        )
    if max_drawdown_pct > VALIDATION_GO_MAX_DRAWDOWN_PCT:
        target_misses.append(
            f"Max drawdown {(max_drawdown_pct * 100):.2f}% above GO target {(VALIDATION_GO_MAX_DRAWDOWN_PCT * 100):.2f}%"
        )
    if intervention_free_days < VALIDATION_GO_MIN_INTERVENTION_FREE_DAYS:
        target_misses.append(
            f"Clean run only {intervention_free_days:.1f}d; need {VALIDATION_GO_MIN_INTERVENTION_FREE_DAYS:.0f}d"
        )
    if uptime_without_manual_intervention_pct < VALIDATION_TARGET_UPTIME_MIN_PCT:
        target_misses.append(
            f"Uptime {uptime_without_manual_intervention_pct:.2f}% below target {VALIDATION_TARGET_UPTIME_MIN_PCT:.0f}%"
        )
    if cost_model_error_pct > VALIDATION_TARGET_COST_MODEL_ERROR_MAX_PCT:
        target_misses.append(
            f"Cost model error {cost_model_error_pct:.2f}% above target {VALIDATION_TARGET_COST_MODEL_ERROR_MAX_PCT:.0f}%"
        )

    if no_go_reasons:
        go_no_go = "NO_GO"
        validation_status = "FAILING"
        blockers = no_go_reasons
    elif target_misses:
        go_no_go = "ADJUST"
        validation_status = (
            "INSUFFICIENT_DATA"
            if observation_days < VALIDATION_GO_MIN_INTERVENTION_FREE_DAYS
            else "MONITORING"
        )
        blockers = target_misses
    else:
        go_no_go = "GO"
        validation_status = "ON_TRACK"
        blockers = []

    return {
        "trade_count": trade_count,
        "total_pnl": total_pnl,
        "win_rate": win_rate,
        "sharpe_ratio_annualized": sharpe,
        "max_drawdown_pct": max_drawdown_pct,
        "monthly_return_pct": monthly_return_pct,
        "cost_model_error_pct": cost_model_error_pct,
        "uptime_without_manual_intervention_pct": uptime_without_manual_intervention_pct,
        "intervention_free_days": max(0.0, intervention_free_days),
        "manual_intervention_count": len(intervention_timestamps),
        "observation_days": observation_days,
        "validation_status": validation_status,
        "validation_targets": target_checks,
        "validation_blockers": blockers,
        "go_no_go": go_no_go,
    }
