"""Portfolio and validation metrics derived from SQLite state."""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from statistics import fmean, pstdev

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


def calculate_metrics(reader: StateReader, trade_limit: int = 5000) -> dict:
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
    thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)
    monthly_pnl = 0.0

    for trade in trades:
        pnl = _safe_float(trade.get("net_pnl_usd"))
        exit_dt = _parse_iso(trade.get("exit_time"))
        if exit_dt is not None:
            daily_pnl[exit_dt.date().isoformat()] += pnl
            if exit_dt >= thirty_days_ago:
                monthly_pnl += pnl
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
        since=(datetime.now(timezone.utc) - timedelta(days=30)).isoformat(),
        limit=10_000,
    )
    cost_model_error_pct = (
        fmean(abs(_safe_float(sample.get("value"))) for sample in cost_samples)
        if cost_samples
        else 0.0
    )

    uptime_samples = reader.get_health_samples(
        metric="loop_alive",
        since=(datetime.now(timezone.utc) - timedelta(days=7)).isoformat(),
        limit=20_000,
    )
    uptime_without_manual_intervention_pct = 0.0
    intervention_free_days = 0.0
    if uptime_samples:
        timestamps = sorted(
            dt for dt in (_parse_iso(sample.get("sample_time")) for sample in uptime_samples) if dt is not None
        )
        if timestamps:
            expected_minutes = max(1, int((timestamps[-1] - timestamps[0]).total_seconds() / 60.0) + 1)
            unique_minutes = {dt.replace(second=0, microsecond=0) for dt in timestamps}
            uptime_without_manual_intervention_pct = min(100.0, (len(unique_minutes) / expected_minutes) * 100.0)

        last_intervention = None
        for sample in uptime_samples:
            if str(sample.get("runtime_mode") or "").upper() != "LIVE" or str(sample.get("alert_level") or "").lower() in {"critical", "blocked"}:
                dt = _parse_iso(sample.get("sample_time"))
                if dt is not None and (last_intervention is None or dt > last_intervention):
                    last_intervention = dt
        if last_intervention is None and timestamps:
            intervention_free_days = (datetime.now(timezone.utc) - timestamps[-1]).total_seconds() / 86400.0
        elif last_intervention is not None:
            intervention_free_days = (datetime.now(timezone.utc) - last_intervention).total_seconds() / 86400.0

    go_no_go = "GO"
    if sharpe < 1.0 or max_drawdown_pct > 0.15:
        go_no_go = "NO_GO"
    elif sharpe < 2.0 or max_drawdown_pct > 0.10:
        go_no_go = "ADJUST"

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
        "go_no_go": go_no_go,
    }
