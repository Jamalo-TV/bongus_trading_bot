"""Risk engine with hard limits, de-risking, and kill-switch support."""

from dataclasses import dataclass


@dataclass
class RiskLimits:
    max_gross_exposure_usd: float = 200_000.0
    max_symbol_concentration: float = 0.5
    soft_drawdown_pct: float = 0.05
    max_drawdown_pct: float = 0.1
    max_data_staleness_minutes: int = 12
    max_latency_ms: int = 400


@dataclass
class RiskState:
    gross_exposure_usd: float
    symbol_concentration: float
    drawdown_pct: float
    data_staleness_minutes: int
    venue_latency_ms: int


@dataclass
class RiskDecision:
    allow_new_risk: bool
    derisk_required: bool
    kill_switch: bool
    position_scale: float  # Add position scale for dynamic scaling
    reasons: list[str]


class RiskEngine:
    def __init__(self, limits: RiskLimits | None = None) -> None:
        self.limits = limits or RiskLimits()

    def evaluate(self, state: RiskState) -> RiskDecision:
        reasons: list[str] = []
        derisk_required = False
        kill_switch = False
        position_scale = 1.0

        if state.gross_exposure_usd > self.limits.max_gross_exposure_usd:
            reasons.append("gross exposure limit exceeded")
            derisk_required = True

        if state.symbol_concentration > self.limits.max_symbol_concentration:
            reasons.append("symbol concentration limit exceeded")
            derisk_required = True

        if state.drawdown_pct > self.limits.max_drawdown_pct:
            reasons.append("max drawdown breached")
            derisk_required = True
            kill_switch = True
        elif state.drawdown_pct >= self.limits.soft_drawdown_pct:
            reasons.append("soft drawdown active: halving leverage")
            position_scale = 0.5

        if state.data_staleness_minutes > self.limits.max_data_staleness_minutes:
            reasons.append("market data staleness too high")
            derisk_required = True

        if state.venue_latency_ms > self.limits.max_latency_ms:
            reasons.append("venue latency too high")
            derisk_required = True

        allow_new_risk = not derisk_required and not kill_switch
        return RiskDecision(
            allow_new_risk=allow_new_risk,
            derisk_required=derisk_required,
            kill_switch=kill_switch,
            position_scale=position_scale,
            reasons=reasons,
        )


def target_exposure_after_derisk(
    current_exposure_usd: float,
    max_exposure_usd: float,
    reduction_fraction: float = 0.25,
) -> float:
    if current_exposure_usd <= max_exposure_usd:
        return current_exposure_usd

    reduced = current_exposure_usd * (1.0 - reduction_fraction)
    return max(max_exposure_usd, reduced)
