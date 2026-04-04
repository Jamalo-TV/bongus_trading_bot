from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class RegimeStatus(str, Enum):
    NORMAL = "NORMAL"
    CAUTION = "CAUTION"
    STRESS = "STRESS"
    HALT_NEW_ENTRIES = "HALT_NEW_ENTRIES"


class AlertSeverity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class RecommendationStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    APPLIED = "APPLIED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    ERROR = "ERROR"


class ReportKind(str, Enum):
    MIDWEEK = "midweek"
    WEEKLY = "weekly"
    MANUAL = "manual"


@dataclass(slots=True)
class SupervisorSnapshot:
    generated_at: str
    account_equity_usd: float
    total_pnl_usd: float
    drawdown_pct: float
    gross_exposure_usd: float
    max_gross_exposure_usd: float
    ann_funding: float
    win_rate: float
    trade_count: int
    open_positions: int
    total_funding_usd: float
    total_execution_cost_usd: float
    total_basis_pnl_usd: float
    funding_staleness_status: str = ""
    risk_reasons: list[str] = field(default_factory=list)
    kill_switch: bool = False
    allow_new_risk: bool = True
    pause_new_entries: bool = False
    stale_seconds: float | None = None
    regime: str = RegimeStatus.NORMAL.value
    anomalies: list[str] = field(default_factory=list)
    recommended_actions: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SupervisorRecommendation:
    recommendation_id: str
    created_at: str
    report_kind: str
    kind: str
    target_key: str
    current_value: float | bool | str
    proposed_value: float | bool | str
    rationale: str
    expires_at: str
    status: str = RecommendationStatus.PENDING.value
    metadata: dict[str, str | float | bool] = field(default_factory=dict)


@dataclass(slots=True)
class ReportSchedule:
    kind: str
    weekday: int
    hour: int
    minute: int
    title: str
