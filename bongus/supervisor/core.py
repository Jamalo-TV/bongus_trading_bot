from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from bongus.engine.state_store import StateReader
from bongus.supervisor.models import RegimeStatus, SupervisorRecommendation, SupervisorSnapshot
from bongus.supervisor.store import SupervisorStore


def collect_snapshot(reader: StateReader, store: SupervisorStore) -> SupervisorSnapshot:
    stats = reader.get_stats()
    risk = reader.get_risk()
    positions = reader.get_positions()
    pnl = reader.get_pnl_attribution()
    timestamps = store.get_state_table_timestamps()

    stale_seconds: float | None = None
    if timestamps:
        latest = max(datetime.fromisoformat(ts) for ts in timestamps.values())
        stale_seconds = max(0.0, (datetime.now(timezone.utc) - latest).total_seconds())

    snapshot = SupervisorSnapshot(
        generated_at=datetime.now(timezone.utc).isoformat(),
        account_equity_usd=float(stats.get("account_equity", 0.0) or 0.0),
        total_pnl_usd=float(stats.get("total_pnl", 0.0) or 0.0),
        drawdown_pct=float(risk.get("drawdown_pct", 0.0) or 0.0),
        gross_exposure_usd=float(stats.get("gross_exposure", 0.0) or 0.0),
        max_gross_exposure_usd=float(stats.get("max_gross_exposure", 0.0) or 0.0),
        ann_funding=float(stats.get("ann_funding", 0.0) or 0.0),
        win_rate=float(stats.get("win_rate", 0.0) or 0.0),
        trade_count=int(stats.get("trade_count", 0) or 0),
        open_positions=len(positions),
        total_funding_usd=float(pnl.get("total_funding", 0.0) or 0.0),
        total_execution_cost_usd=float(pnl.get("total_execution_cost", 0.0) or 0.0),
        total_basis_pnl_usd=float(pnl.get("total_basis_pnl", 0.0) or 0.0),
        risk_reasons=[str(item) for item in risk.get("reasons", [])],
        kill_switch=bool(risk.get("kill_switch", False)),
        allow_new_risk=bool(risk.get("allow_new_risk", True)),
        pause_new_entries=bool(risk.get("pause_new_entries", False)),
        stale_seconds=stale_seconds,
    )
    return annotate_snapshot(snapshot)


def annotate_snapshot(snapshot: SupervisorSnapshot) -> SupervisorSnapshot:
    anomalies: list[str] = []
    actions: list[str] = []
    regime = RegimeStatus.NORMAL

    utilization = 0.0
    if snapshot.max_gross_exposure_usd > 0:
        utilization = snapshot.gross_exposure_usd / snapshot.max_gross_exposure_usd

    if snapshot.kill_switch:
        anomalies.append("Kill switch is active.")
        actions.append("Keep new entries paused until operator review.")
        regime = RegimeStatus.HALT_NEW_ENTRIES

    if snapshot.stale_seconds is not None and snapshot.stale_seconds >= 180:
        anomalies.append(f"State telemetry is stale ({int(snapshot.stale_seconds)}s).")
        if regime != RegimeStatus.HALT_NEW_ENTRIES:
            regime = RegimeStatus.STRESS
        actions.append("Check data feeds and Rust telemetry before resuming entries.")

    if snapshot.drawdown_pct >= 0.08:
        anomalies.append(f"Drawdown is elevated at {snapshot.drawdown_pct:.1%}.")
        regime = max_regime(regime, RegimeStatus.STRESS)
    elif snapshot.drawdown_pct >= 0.04:
        anomalies.append(f"Soft drawdown is active at {snapshot.drawdown_pct:.1%}.")
        regime = max_regime(regime, RegimeStatus.CAUTION)

    if utilization >= 0.9:
        anomalies.append(f"Gross exposure is at {utilization:.0%} of configured capacity.")
        regime = max_regime(regime, RegimeStatus.CAUTION)

    if snapshot.trade_count >= 5 and snapshot.win_rate <= 0.40:
        anomalies.append(f"Win rate is weak at {snapshot.win_rate:.1%} across {snapshot.trade_count} trades.")
        regime = max_regime(regime, RegimeStatus.CAUTION)

    if snapshot.total_funding_usd > 0 and snapshot.total_execution_cost_usd > snapshot.total_funding_usd * 0.8:
        anomalies.append("Execution costs are consuming most of realized funding.")
        regime = max_regime(regime, RegimeStatus.CAUTION)

    if snapshot.total_funding_usd <= 0 and snapshot.total_execution_cost_usd > 0 and snapshot.trade_count >= 3:
        anomalies.append("Recent realized funding is non-positive while execution costs remain positive.")
        regime = max_regime(regime, RegimeStatus.STRESS)

    if snapshot.open_positions > 0 and snapshot.ann_funding <= 0:
        anomalies.append("Open position exists while annualized funding is non-positive.")
        regime = max_regime(regime, RegimeStatus.CAUTION)

    if snapshot.risk_reasons:
        actions.extend(snapshot.risk_reasons)
        if any("staleness" in reason or "latency" in reason for reason in snapshot.risk_reasons):
            regime = max_regime(regime, RegimeStatus.STRESS)

    if snapshot.pause_new_entries:
        actions.append("New entries are currently paused.")

    snapshot.regime = regime.value
    snapshot.anomalies = dedupe(anomalies)
    snapshot.recommended_actions = dedupe(actions)
    return snapshot


def max_regime(current: RegimeStatus, candidate: RegimeStatus) -> RegimeStatus:
    order = {
        RegimeStatus.NORMAL: 0,
        RegimeStatus.CAUTION: 1,
        RegimeStatus.STRESS: 2,
        RegimeStatus.HALT_NEW_ENTRIES: 3,
    }
    return candidate if order[candidate] > order[current] else current


def dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


def build_recommendations(
    snapshot: SupervisorSnapshot,
    config_snapshot: dict[str, object],
    report_kind: str,
) -> list[SupervisorRecommendation]:
    recommendations: list[SupervisorRecommendation] = []

    entry_threshold = _coerce_float(config_snapshot.get("entry_ann_funding_threshold"), 0.15)
    exit_threshold = _coerce_float(config_snapshot.get("exit_ann_funding_threshold"), 0.01)
    notional_per_trade = _coerce_float(config_snapshot.get("notional_per_trade"), 20_000.0)

    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(days=7)).isoformat()

    if snapshot.trade_count >= 4 and (
        snapshot.total_execution_cost_usd > max(25.0, snapshot.total_funding_usd * 0.8)
        or snapshot.total_pnl_usd < 0
    ):
        proposed = min(0.50, round(entry_threshold + max(0.01, entry_threshold * 0.10), 4))
        if proposed > entry_threshold:
            recommendations.append(
                SupervisorRecommendation(
                    recommendation_id=uuid4().hex[:10],
                    created_at=now.isoformat(),
                    report_kind=report_kind,
                    kind="tighten_entry_threshold",
                    target_key="entry_ann_funding_threshold",
                    current_value=entry_threshold,
                    proposed_value=proposed,
                    rationale="Execution friction is eating too much of the funding edge; tighten entries until conditions improve.",
                    expires_at=expires_at,
                    metadata={"direction": "raise", "max_change_pct": 0.15},
                )
            )

    if snapshot.drawdown_pct >= 0.04 or (snapshot.trade_count >= 6 and snapshot.win_rate < 0.45):
        proposed_notional = max(1_000.0, round(notional_per_trade * 0.75, 2))
        if proposed_notional < notional_per_trade:
            recommendations.append(
                SupervisorRecommendation(
                    recommendation_id=uuid4().hex[:10],
                    created_at=now.isoformat(),
                    report_kind=report_kind,
                    kind="reduce_notional",
                    target_key="notional_per_trade",
                    current_value=notional_per_trade,
                    proposed_value=proposed_notional,
                    rationale="Recent drawdown and hit rate warrant de-risking until the edge stabilizes again.",
                    expires_at=expires_at,
                    metadata={"direction": "lower", "max_change_pct": 0.25},
                )
            )

    if snapshot.open_positions > 0 and snapshot.ann_funding < exit_threshold and exit_threshold < 0.08:
        proposed_exit = round(min(0.08, exit_threshold + 0.01), 4)
        if proposed_exit > exit_threshold:
            recommendations.append(
                SupervisorRecommendation(
                    recommendation_id=uuid4().hex[:10],
                    created_at=now.isoformat(),
                    report_kind=report_kind,
                    kind="tighten_exit_threshold",
                    target_key="exit_ann_funding_threshold",
                    current_value=exit_threshold,
                    proposed_value=proposed_exit,
                    rationale="Funding has decayed below the current exit bar while exposure remains open; a tighter exit threshold would reduce carry bleed.",
                    expires_at=expires_at,
                    metadata={"direction": "raise", "max_change_pct": 0.01},
                )
            )

    return recommendations[:3]


def snapshot_to_dict(snapshot: SupervisorSnapshot) -> dict[str, object]:
    return asdict(snapshot)


def _coerce_float(value: object, default: float) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    return default
