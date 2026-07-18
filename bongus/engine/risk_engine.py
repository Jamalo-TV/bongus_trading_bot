"""Risk engine with hard limits, de-risking, kill-switch, and consecutive loss tracking."""

from dataclasses import dataclass


import time

@dataclass
class RiskLimits:
    max_gross_exposure_usd: float = 10_000.0
    max_symbol_concentration: float = 0.60
    soft_drawdown_pct: float = 0.04
    max_drawdown_pct: float = 0.1
    max_drawdown_release_pct: float = 0.08
    max_data_staleness_minutes: int = 12
    max_latency_ms: int = 400
    max_consecutive_losses: int = 5
    venue_latency_debounce_s: float = 30.0


@dataclass
class RiskState:
    gross_exposure_usd: float
    symbol_concentration: float
    drawdown_pct: float
    data_staleness_minutes: int
    venue_latency_ms: int
    consecutive_losses: int = 0
    previous_kill_switch: bool = False
    liquidation_buffer_usd: float | None = None
    minimum_liquidation_buffer_usd: float = 0.0


@dataclass
class RiskDecision:
    allow_new_risk: bool
    derisk_required: bool
    kill_switch: bool
    position_scale: float
    reasons: list[str]


class RiskEngine:
    def __init__(self, limits: RiskLimits | None = None) -> None:
        self.limits = limits or RiskLimits()
        self._high_latency_start_monotonic: float = 0.0

    def evaluate(self, state: RiskState, now: float | None = None) -> RiskDecision:
        reasons: list[str] = []
        derisk_required = False
        kill_switch = False
        block_new_risk = False
        position_scale = 1.0

        if state.gross_exposure_usd > self.limits.max_gross_exposure_usd:
            reasons.append("gross exposure limit exceeded")
            derisk_required = True

        if state.symbol_concentration > self.limits.max_symbol_concentration:
            reasons.append("symbol concentration limit exceeded")
            derisk_required = True

        drawdown_trigger_pct = self.limits.max_drawdown_pct
        if state.previous_kill_switch:
            drawdown_trigger_pct = min(
                self.limits.max_drawdown_pct,
                self.limits.max_drawdown_release_pct,
            )
        if state.drawdown_pct > drawdown_trigger_pct:
            reasons.append("max drawdown breached")
            derisk_required = True
            kill_switch = True
        elif state.drawdown_pct >= self.limits.soft_drawdown_pct:
            position_scale = max(0.1, 1.0 - state.drawdown_pct / self.limits.max_drawdown_pct)
            reasons.append(f"soft drawdown active: scaling positions to {position_scale:.2f}")

        if state.data_staleness_minutes > self.limits.max_data_staleness_minutes:
            reasons.append("market data staleness too high")
            derisk_required = True

        if state.venue_latency_ms > self.limits.max_latency_ms:
            current_time = now if now is not None else time.monotonic()
            if self._high_latency_start_monotonic <= 0:
                self._high_latency_start_monotonic = current_time

            duration = current_time - self._high_latency_start_monotonic
            if duration >= self.limits.venue_latency_debounce_s:
                # High latency blocks new entries but does not force exits — closing
                # positions at high latency incurs the same execution cost disadvantage.
                reasons.append(f"venue latency too high ({state.venue_latency_ms}ms > {self.limits.max_latency_ms}ms for {duration:.1f}s)")
                block_new_risk = True
            else:
                reasons.append(f"venue latency elevated ({state.venue_latency_ms}ms); debouncing ({duration:.1f}s/{self.limits.venue_latency_debounce_s}s)")
        else:
            self._high_latency_start_monotonic = 0.0

        if state.consecutive_losses >= self.limits.max_consecutive_losses:
            reasons.append(
                f"consecutive loss limit reached ({state.consecutive_losses}/{self.limits.max_consecutive_losses})"
            )
            derisk_required = True

        if (
            state.liquidation_buffer_usd is not None
            and state.minimum_liquidation_buffer_usd > 0.0
            and state.liquidation_buffer_usd
            < state.minimum_liquidation_buffer_usd
        ):
            reasons.append(
                "liquidation margin buffer compressed "
                f"(${state.liquidation_buffer_usd:.2f} < "
                f"${state.minimum_liquidation_buffer_usd:.2f})"
            )
            derisk_required = True
            kill_switch = True

        allow_new_risk = not derisk_required and not kill_switch and not block_new_risk
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
    # Use min so we never return a target above the hard limit.
    # max() was a bug: when reduced < max_exposure it correctly clamped up,
    # but when reduced > max_exposure it returned the still-overlimit value.
    return min(max_exposure_usd, reduced)
