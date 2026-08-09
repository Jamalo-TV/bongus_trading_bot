"""Canonical, deterministic entry-decision engine.

The engine is shared by replay, shadow, paper, testnet and eventual live
adapters.  A surface label is accepted for provenance but is deliberately not
read by the economic kernel: identical snapshots and configuration therefore
produce identical actions, reason codes and configuration hashes.

Funding is evaluated as exact, discrete settlement cash flows over every
prefix ``1..N`` of the supplied causal forecast.  No continuously-prorated
annualized funding value enters the decision.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import json
import math
from typing import Literal, Mapping, Sequence

from bongus.core.config import (
    MAX_CONCURRENT_POSITIONS,
    MAX_GROSS_EXPOSURE_USD,
    MAX_LEVERAGE,
    MAX_NOTIONAL_PER_TRADE,
    PER_SYMBOL_NOTIONAL_CAP_USD,
    TAKER_FEE_PERP,
    TAKER_FEE_SPOT,
)
from bongus.domain.units import (
    AnnualizedReportingRate,
    EconomicUnitSnapshot,
    RawSettlementRate,
)
from bongus.engine.cost_model import (
    PairedActionCostBreakdown,
    paired_exact_book_cost_breakdown,
)
from bongus.market_data.depth_tracker import (
    DepthTracker,
    ExecutablePairCapacity,
    PairDirection,
)
from bongus.market_data.settlement_model import SettlementForecast
from bongus.strategies.opportunity_kernel import (
    OPPORTUNITY_KERNEL_VERSION,
    OpportunityEvaluationInput,
    SettlementExpectation,
    evaluate_opportunity,
)


DECISION_ENGINE_VERSION = "canonical-decision-engine-v1"
HARD_MAX_SLOTS = 4
HARD_MAX_LEG_NOTIONAL_USD = float(MAX_NOTIONAL_PER_TRADE)
HARD_MAX_PAIR_GROSS_USD = 2.0 * float(PER_SYMBOL_NOTIONAL_CAP_USD)
HARD_MAX_PORTFOLIO_PAIR_GROSS_USD = 2.0 * float(MAX_GROSS_EXPOSURE_USD)
HARD_MAX_LEVERAGE = float(MAX_LEVERAGE)

DecisionSurface = Literal["replay", "shadow", "paper", "testnet", "live"]
DecisionAction = Literal["enter", "reject"]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("decision timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _finite(value: float) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def _dedupe(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


@dataclass(frozen=True, slots=True)
class DecisionEngineConfig:
    """Immutable decision policy with hard safety ceilings.

    Values stricter than the repository limits reduce exposure.  Values above
    those limits are clamped by the ``effective_*`` properties, so a runtime
    override can never raise the four-slot, leverage or notional ceilings.
    """

    max_slots: int = MAX_CONCURRENT_POSITIONS
    max_leg_notional_usd: float = MAX_NOTIONAL_PER_TRADE
    max_pair_gross_per_symbol_usd: float = 2.0 * PER_SYMBOL_NOTIONAL_CAP_USD
    max_portfolio_pair_gross_usd: float = 2.0 * MAX_GROSS_EXPOSURE_USD
    max_leverage: float = MAX_LEVERAGE
    max_book_age_seconds: float = 5.0
    max_calendar_age_seconds: float = 3_600.0
    max_filter_age_seconds: float = 3_600.0
    max_funding_age_seconds: float = 30.0
    minimum_forecast_confidence: float = 0.50
    max_settlements: int = 6
    minimum_lower_bound_ev_usd: float = 0.0
    minimum_lower_bound_edge_bps: float = 0.0
    spot_taker_fee_pct: float = TAKER_FEE_SPOT
    perp_taker_fee_pct: float = TAKER_FEE_PERP
    required_rate_limit_weight: int = 4
    default_missed_settlement_probability: float = 0.0
    default_operational_hazard_probability: float = 0.0
    default_reversal_hazard_bps_per_settlement: float = 0.0
    default_operational_hazard_bps_per_settlement: float = 0.0
    default_liquidation_tail_bps_per_settlement: float = 0.0
    default_borrow_cost_bps_per_hour: float = 0.0
    default_collateral_cost_bps_per_hour: float = 0.0
    default_capital_opportunity_cost_bps_per_hour: float = 0.0
    exit_delay_hours: float = 0.0

    def __post_init__(self) -> None:
        positive = {
            "max_slots": self.max_slots,
            "max_leg_notional_usd": self.max_leg_notional_usd,
            "max_pair_gross_per_symbol_usd": self.max_pair_gross_per_symbol_usd,
            "max_portfolio_pair_gross_usd": self.max_portfolio_pair_gross_usd,
            "max_leverage": self.max_leverage,
            "max_book_age_seconds": self.max_book_age_seconds,
            "max_calendar_age_seconds": self.max_calendar_age_seconds,
            "max_filter_age_seconds": self.max_filter_age_seconds,
            "max_funding_age_seconds": self.max_funding_age_seconds,
            "max_settlements": self.max_settlements,
            "required_rate_limit_weight": self.required_rate_limit_weight,
        }
        for name, value in positive.items():
            if not _finite(float(value)) or float(value) <= 0.0:
                raise ValueError(f"{name} must be positive and finite")

        nonnegative = {
            "minimum_lower_bound_ev_usd": self.minimum_lower_bound_ev_usd,
            "minimum_lower_bound_edge_bps": self.minimum_lower_bound_edge_bps,
            "spot_taker_fee_pct": self.spot_taker_fee_pct,
            "perp_taker_fee_pct": self.perp_taker_fee_pct,
            "default_reversal_hazard_bps_per_settlement": (
                self.default_reversal_hazard_bps_per_settlement
            ),
            "default_operational_hazard_bps_per_settlement": (
                self.default_operational_hazard_bps_per_settlement
            ),
            "default_liquidation_tail_bps_per_settlement": (
                self.default_liquidation_tail_bps_per_settlement
            ),
            "default_borrow_cost_bps_per_hour": self.default_borrow_cost_bps_per_hour,
            "default_collateral_cost_bps_per_hour": (
                self.default_collateral_cost_bps_per_hour
            ),
            "default_capital_opportunity_cost_bps_per_hour": (
                self.default_capital_opportunity_cost_bps_per_hour
            ),
            "exit_delay_hours": self.exit_delay_hours,
        }
        for name, value in nonnegative.items():
            if not _finite(value) or float(value) < 0.0:
                raise ValueError(f"{name} must be non-negative and finite")

        for name, value in (
            (
                "minimum_forecast_confidence",
                self.minimum_forecast_confidence,
            ),
            (
                "default_missed_settlement_probability",
                self.default_missed_settlement_probability,
            ),
            (
                "default_operational_hazard_probability",
                self.default_operational_hazard_probability,
            ),
        ):
            if not _finite(value) or not 0.0 <= float(value) <= 1.0:
                raise ValueError(f"{name} must be between zero and one")

    @property
    def effective_max_slots(self) -> int:
        return min(HARD_MAX_SLOTS, int(self.max_slots))

    @property
    def effective_max_leg_notional_usd(self) -> float:
        return min(HARD_MAX_LEG_NOTIONAL_USD, float(self.max_leg_notional_usd))

    @property
    def effective_max_pair_gross_per_symbol_usd(self) -> float:
        return min(
            HARD_MAX_PAIR_GROSS_USD,
            float(self.max_pair_gross_per_symbol_usd),
        )

    @property
    def effective_max_portfolio_pair_gross_usd(self) -> float:
        return min(
            HARD_MAX_PORTFOLIO_PAIR_GROSS_USD,
            float(self.max_portfolio_pair_gross_usd),
        )

    @property
    def effective_max_leverage(self) -> float:
        return min(HARD_MAX_LEVERAGE, float(self.max_leverage))


def canonical_config_payload(config: DecisionEngineConfig) -> bytes:
    """Serialize every policy input in a stable, cross-surface form."""

    payload: Mapping[str, object] = {
        "decision_engine_version": DECISION_ENGINE_VERSION,
        "opportunity_kernel_version": OPPORTUNITY_KERNEL_VERSION,
        "policy": asdict(config),
        "hard_ceilings": {
            "max_slots": HARD_MAX_SLOTS,
            "max_leg_notional_usd": HARD_MAX_LEG_NOTIONAL_USD,
            "max_pair_gross_usd": HARD_MAX_PAIR_GROSS_USD,
            "max_portfolio_pair_gross_usd": HARD_MAX_PORTFOLIO_PAIR_GROSS_USD,
            "max_leverage": HARD_MAX_LEVERAGE,
        },
    }
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_config_hash(config: DecisionEngineConfig) -> str:
    return hashlib.sha256(canonical_config_payload(config)).hexdigest()


@dataclass(frozen=True, slots=True)
class DecisionRequest:
    symbol: str
    decision_time: datetime
    direction: PairDirection
    requested_leg_notional_usd: float
    settlement_forecast: SettlementForecast
    surface: DecisionSurface = "shadow"
    forecast_confidence: float = 0.0
    calendar_authoritative: bool = False
    calendar_observed_at: datetime | None = None
    spot_filters_valid: bool = False
    perp_filters_valid: bool = False
    filters_observed_at: datetime | None = None
    rate_limit_budget: int = 0
    current_open_slots: int = 0
    current_portfolio_pair_gross_usd: float = 0.0
    current_symbol_pair_gross_usd: float = 0.0
    collateral_available_usd: float = 0.0
    margin_available_usd: float = 0.0
    entry_capacity: ExecutablePairCapacity | None = None
    exit_capacity: ExecutablePairCapacity | None = None
    spot_spread_bps: float | None = None
    perp_spread_bps: float | None = None
    missed_settlement_probabilities: tuple[float, ...] = ()
    operational_hazard_probability: float | None = None
    reversal_hazard_bps_per_settlement: float | None = None
    operational_hazard_bps_per_settlement: float | None = None
    liquidation_tail_bps_per_settlement: float | None = None
    borrow_cost_bps_per_hour: float | None = None
    collateral_cost_bps_per_hour: float | None = None
    capital_opportunity_cost_bps_per_hour: float | None = None
    basis_expected_bps_by_settlement: tuple[float, ...] = ()
    basis_lower_bps_by_settlement: tuple[float, ...] = ()
    metadata: Mapping[str, str | int | float | bool] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class HorizonEvaluation:
    settlement_count: int
    holding_hours: float
    last_settlement_time: datetime
    funding_mean_usd: float
    funding_lower_usd: float
    basis_mean_usd: float
    basis_lower_usd: float
    entry_execution_cost_usd: float
    exit_execution_cost_usd: float
    borrow_cost_usd: float
    collateral_cost_usd: float
    capital_opportunity_cost_usd: float
    reversal_hazard_usd: float
    operational_hazard_usd: float
    liquidation_tail_usd: float
    mean_net_ev_usd: float
    lower_bound_net_ev_usd: float
    mean_net_edge_bps: float
    lower_bound_net_edge_bps: float
    lower_bound_pair_gross_bps: float


@dataclass(frozen=True, slots=True)
class Decision:
    engine_version: str
    config_hash: str
    action: DecisionAction
    eligible: bool
    symbol: str
    direction: PairDirection
    reason_codes: tuple[str, ...]
    requested_leg_notional_usd: float
    approved_leg_notional_usd: float
    economic_units: EconomicUnitSnapshot | None
    raw_first_settlement_rate: RawSettlementRate | None
    reporting_annualized_rate: AnnualizedReportingRate | None
    selected_settlement_count: int
    selected_horizon: HorizonEvaluation | None
    horizons: tuple[HorizonEvaluation, ...]
    entry_cost: PairedActionCostBreakdown | None
    exit_cost: PairedActionCostBreakdown | None


@dataclass(frozen=True, slots=True)
class PortfolioSelection:
    """Deterministic top-k selection produced by the canonical engine.

    Economic eligibility and portfolio competition are intentionally kept in
    the same component.  Runtime surfaces may still add safety exclusions,
    but a legacy funding ranker cannot choose which eligible entry is sent.
    """

    engine_version: str
    config_hash: str
    selected: tuple[Decision, ...]
    rejected: Mapping[str, tuple[str, ...]]
    occupied_slots: int
    available_slots: int


class DecisionEngine:
    """Evaluate entry safety and lower-confidence economics deterministically."""

    def __init__(self, config: DecisionEngineConfig | None = None) -> None:
        self.config = config or DecisionEngineConfig()
        self.config_hash = canonical_config_hash(self.config)

    def select_entries(
        self,
        decisions: Sequence[Decision],
        *,
        open_symbols: Sequence[str] = (),
        pending_symbols: Sequence[str] = (),
        excluded_reasons: Mapping[str, Sequence[str]] | None = None,
        current_portfolio_pair_gross_usd: float = 0.0,
        available_collateral_usd: float | None = None,
        available_margin_usd: float | None = None,
    ) -> PortfolioSelection:
        """Select the best lower-confidence entries under the hard slot cap.

        The result is surface-independent.  Callers may supply safety-only
        exclusions (cooldowns, reconciliation latches, storage gates), but
        legacy economic scores are deliberately not an input.
        """

        occupied = {
            str(symbol).strip().upper()
            for symbol in (*open_symbols, *pending_symbols)
            if str(symbol).strip()
        }
        available = max(0, self.config.effective_max_slots - len(occupied))
        try:
            current_pair_gross = float(current_portfolio_pair_gross_usd)
        except (TypeError, ValueError, OverflowError):
            current_pair_gross = math.nan
        valid_current_pair_gross = (
            math.isfinite(current_pair_gross) and current_pair_gross >= 0.0
        )
        remaining_pair_gross = (
            max(
                0.0,
                self.config.effective_max_portfolio_pair_gross_usd
                - current_pair_gross,
            )
            if valid_current_pair_gross
            else 0.0
        )
        remaining_collateral = math.inf
        remaining_margin = math.inf
        capacity_error = ""
        for name, supplied in (
            ("collateral", available_collateral_usd),
            ("margin", available_margin_usd),
        ):
            if supplied is None:
                continue
            try:
                value = float(supplied)
            except (TypeError, ValueError, OverflowError):
                value = math.nan
            if not math.isfinite(value) or value < 0.0:
                capacity_error = f"invalid_available_{name}"
                value = 0.0
            if name == "collateral":
                remaining_collateral = value
            else:
                remaining_margin = value
        exclusions = {
            str(symbol).strip().upper(): _dedupe(tuple(str(reason) for reason in reasons))
            for symbol, reasons in (excluded_reasons or {}).items()
            if str(symbol).strip()
        }
        rejected: dict[str, tuple[str, ...]] = {}
        eligible: list[Decision] = []
        grouped: dict[str, list[Decision]] = {}
        for item in decisions:
            normalized = item.symbol.strip().upper()
            if normalized:
                grouped.setdefault(normalized, []).append(item)
        for symbol, symbol_decisions in grouped.items():
            reasons: list[str] = []
            if len(symbol_decisions) != 1:
                rejected[symbol] = ("duplicate_symbol_decision",)
                continue
            decision = symbol_decisions[0]
            if (
                decision.engine_version != DECISION_ENGINE_VERSION
                or decision.config_hash != self.config_hash
            ):
                reasons.append("foreign_decision_policy")
            if symbol in occupied:
                reasons.append("already_open_or_pending")
            reasons.extend(exclusions.get(symbol, ()))
            if not decision.eligible or decision.action != "enter":
                reasons.extend(decision.reason_codes or ("economically_ineligible",))
            if decision.selected_horizon is None:
                reasons.append("missing_selected_horizon")
            if decision.economic_units is None:
                reasons.append("missing_economic_units")
            if (
                not _finite(decision.approved_leg_notional_usd)
                or decision.approved_leg_notional_usd <= 0.0
            ):
                reasons.append("invalid_approved_leg_notional")
            if not valid_current_pair_gross:
                reasons.append("invalid_current_portfolio_pair_gross")
            if capacity_error:
                reasons.append(capacity_error)
            if reasons:
                rejected[symbol] = _dedupe(reasons)
                continue
            eligible.append(decision)

        eligible.sort(
            key=lambda item: (
                -float(item.selected_horizon.lower_bound_net_ev_usd)  # type: ignore[union-attr]
                / max(float(item.approved_leg_notional_usd), 1e-9),
                -float(item.selected_horizon.lower_bound_net_ev_usd),  # type: ignore[union-attr]
                item.symbol,
            )
        )
        selected_items: list[Decision] = []
        for decision in eligible:
            if len(selected_items) >= available:
                rejected[decision.symbol] = ("portfolio_slot_competition",)
                continue
            assert decision.economic_units is not None
            pair_gross = float(decision.economic_units.pair_gross.value)
            if not math.isfinite(pair_gross) or pair_gross <= 0.0:
                rejected[decision.symbol] = ("invalid_approved_pair_gross",)
                continue
            if pair_gross > remaining_pair_gross + 1e-9:
                rejected[decision.symbol] = (
                    "portfolio_pair_gross_competition",
                )
                continue
            leg_notional = float(decision.approved_leg_notional_usd)
            margin_required = leg_notional / self.config.effective_max_leverage
            if leg_notional > remaining_collateral + 1e-9:
                rejected[decision.symbol] = (
                    "portfolio_collateral_competition",
                )
                continue
            if margin_required > remaining_margin + 1e-9:
                rejected[decision.symbol] = (
                    "portfolio_margin_competition",
                )
                continue
            selected_items.append(decision)
            remaining_pair_gross = max(0.0, remaining_pair_gross - pair_gross)
            remaining_collateral = max(0.0, remaining_collateral - leg_notional)
            remaining_margin = max(0.0, remaining_margin - margin_required)
        selected = tuple(selected_items)
        return PortfolioSelection(
            engine_version=DECISION_ENGINE_VERSION,
            config_hash=self.config_hash,
            selected=selected,
            rejected=rejected,
            occupied_slots=len(occupied),
            available_slots=available,
        )

    def _approved_notional(
        self, request: DecisionRequest, reasons: list[str]
    ) -> float:
        numeric_fields = {
            "requested_leg_notional": request.requested_leg_notional_usd,
            "portfolio_pair_gross": request.current_portfolio_pair_gross_usd,
            "symbol_pair_gross": request.current_symbol_pair_gross_usd,
            "collateral_available": request.collateral_available_usd,
            "margin_available": request.margin_available_usd,
        }
        clean: dict[str, float] = {}
        for name, value in numeric_fields.items():
            if not _finite(value) or float(value) < 0.0:
                reasons.append(f"invalid_{name}")
                clean[name] = 0.0
            else:
                clean[name] = float(value)
        if clean["requested_leg_notional"] <= 0.0:
            reasons.append("non_positive_requested_leg_notional")

        try:
            open_slots = float(request.current_open_slots)
        except (TypeError, ValueError, OverflowError):
            open_slots = math.nan
        if (
            not math.isfinite(open_slots)
            or open_slots < 0.0
            or not open_slots.is_integer()
        ):
            reasons.append("invalid_open_slot_count")
        elif int(open_slots) >= self.config.effective_max_slots:
            reasons.append("slot_capacity_exhausted")

        symbol_pair_remaining = max(
            0.0,
            self.config.effective_max_pair_gross_per_symbol_usd
            - clean["symbol_pair_gross"],
        )
        portfolio_pair_remaining = max(
            0.0,
            self.config.effective_max_portfolio_pair_gross_usd
            - clean["portfolio_pair_gross"],
        )
        margin_notional_capacity = (
            clean["margin_available"] * self.config.effective_max_leverage
        )
        approved = min(
            clean["requested_leg_notional"],
            self.config.effective_max_leg_notional_usd,
            symbol_pair_remaining / 2.0,
            portfolio_pair_remaining / 2.0,
            clean["collateral_available"],
            margin_notional_capacity,
        )
        if approved <= 0.0:
            reasons.append("no_executable_account_capacity")
            return 0.0
        if approved + 1e-9 < clean["requested_leg_notional"]:
            reasons.append("notional_reduced_by_cap")
        return approved

    def _attach_depth(
        self,
        request: DecisionRequest,
        depth_tracker: DepthTracker,
        approved_notional: float,
        *,
        book_check_time: float | None,
    ) -> DecisionRequest:
        if approved_notional <= 0.0:
            return request
        entry = depth_tracker.executable_pair_capacity(
            request.symbol.strip().upper(),
            approved_notional,
            direction=request.direction,
            operation="entry",
            max_age_seconds=self.config.max_book_age_seconds,
            now=book_check_time,
        )
        exit_capacity = depth_tracker.executable_pair_capacity(
            request.symbol.strip().upper(),
            approved_notional,
            direction=request.direction,
            operation="exit",
            max_age_seconds=self.config.max_book_age_seconds,
            now=book_check_time,
        )
        return replace(
            request,
            entry_capacity=entry,
            exit_capacity=exit_capacity,
            spot_spread_bps=depth_tracker.spot_spread_bps(request.symbol),
            perp_spread_bps=depth_tracker.perp_spread_bps(request.symbol),
        )

    @staticmethod
    def _validate_timestamp_age(
        *,
        value: datetime | None,
        decision_time: datetime,
        maximum_age_seconds: float,
        missing_reason: str,
        stale_reason: str,
        future_reason: str,
        reasons: list[str],
    ) -> None:
        if value is None:
            reasons.append(missing_reason)
            return
        try:
            observed = _utc(value)
        except ValueError:
            reasons.append(missing_reason)
            return
        age = (decision_time - observed).total_seconds()
        if age < -1e-6:
            reasons.append(future_reason)
        elif age > maximum_age_seconds:
            reasons.append(stale_reason)

    @staticmethod
    def _request_value(
        supplied: float | None,
        default: float,
        name: str,
        reasons: list[str],
        *,
        probability: bool = False,
    ) -> float:
        try:
            value = float(default if supplied is None else supplied)
        except (TypeError, ValueError, OverflowError):
            reasons.append(f"invalid_{name}")
            return 0.0
        if not _finite(value) or value < 0.0:
            reasons.append(f"invalid_{name}")
            return 0.0
        if probability and value > 1.0:
            reasons.append(f"invalid_{name}")
            return 0.0
        return value

    @staticmethod
    def _series_value(values: tuple[float, ...], index: int) -> float:
        if index >= len(values):
            return 0.0
        try:
            return float(values[index])
        except (TypeError, ValueError, OverflowError):
            return math.nan

    def _reject(
        self,
        request: DecisionRequest,
        reasons: list[str],
        approved_notional: float,
        *,
        units: EconomicUnitSnapshot | None = None,
        raw_rate: RawSettlementRate | None = None,
        horizons: tuple[HorizonEvaluation, ...] = (),
        entry_cost: PairedActionCostBreakdown | None = None,
        exit_cost: PairedActionCostBreakdown | None = None,
    ) -> Decision:
        selected = max(
            horizons,
            key=lambda item: (item.lower_bound_net_ev_usd, -item.settlement_count),
            default=None,
        )
        return Decision(
            engine_version=DECISION_ENGINE_VERSION,
            config_hash=self.config_hash,
            action="reject",
            eligible=False,
            symbol=request.symbol.strip().upper(),
            direction=request.direction,
            reason_codes=_dedupe(reasons),
            requested_leg_notional_usd=(
                float(request.requested_leg_notional_usd)
                if _finite(request.requested_leg_notional_usd)
                else 0.0
            ),
            approved_leg_notional_usd=approved_notional,
            economic_units=units,
            raw_first_settlement_rate=raw_rate,
            reporting_annualized_rate=(
                raw_rate.reporting_annualized if raw_rate is not None else None
            ),
            selected_settlement_count=(selected.settlement_count if selected else 0),
            selected_horizon=selected,
            horizons=horizons,
            entry_cost=entry_cost,
            exit_cost=exit_cost,
        )

    def decide(
        self,
        request: DecisionRequest,
        *,
        depth_tracker: DepthTracker | None = None,
        book_check_time: float | None = None,
    ) -> Decision:
        reasons: list[str] = []
        symbol = request.symbol.strip().upper()
        if not symbol:
            reasons.append("missing_symbol")
        if request.surface not in ("replay", "shadow", "paper", "testnet", "live"):
            reasons.append("unknown_surface")
        if request.direction not in (
            "long_spot_short_perp",
            "short_spot_long_perp",
        ):
            reasons.append("unknown_direction")
        elif request.direction == "short_spot_long_perp":
            # There is intentionally no configuration escape hatch.  Reverse
            # entry can only be added with an authoritative borrow lifecycle.
            reasons.append("reverse_short_spot_disabled")

        try:
            decision_time = _utc(request.decision_time)
        except ValueError:
            decision_time = datetime.min.replace(tzinfo=timezone.utc)
            reasons.append("invalid_decision_time")

        approved = self._approved_notional(request, reasons)
        if depth_tracker is not None and (
            request.entry_capacity is None or request.exit_capacity is None
        ):
            request = self._attach_depth(
                request,
                depth_tracker,
                approved,
                book_check_time=book_check_time,
            )

        if not request.calendar_authoritative:
            reasons.append("missing_authoritative_calendar")
        self._validate_timestamp_age(
            value=request.calendar_observed_at,
            decision_time=decision_time,
            maximum_age_seconds=self.config.max_calendar_age_seconds,
            missing_reason="missing_calendar_timestamp",
            stale_reason="stale_calendar",
            future_reason="future_calendar",
            reasons=reasons,
        )
        if not request.spot_filters_valid:
            reasons.append("spot_filters_unavailable")
        if not request.perp_filters_valid:
            reasons.append("perp_filters_unavailable")
        self._validate_timestamp_age(
            value=request.filters_observed_at,
            decision_time=decision_time,
            maximum_age_seconds=self.config.max_filter_age_seconds,
            missing_reason="missing_filter_timestamp",
            stale_reason="stale_filters",
            future_reason="future_filters",
            reasons=reasons,
        )

        try:
            rate_limit_budget = float(request.rate_limit_budget)
        except (TypeError, ValueError, OverflowError):
            rate_limit_budget = math.nan
        if (
            not math.isfinite(rate_limit_budget)
            or rate_limit_budget < 0.0
            or not rate_limit_budget.is_integer()
        ):
            reasons.append("invalid_rate_limit_budget")
        elif rate_limit_budget < self.config.required_rate_limit_weight:
            reasons.append("insufficient_rate_limit_budget")

        forecast = request.settlement_forecast
        if not forecast.valid:
            reasons.append("invalid_settlement_forecast")
            reasons.extend(f"forecast:{reason}" for reason in forecast.reason_codes)
        if forecast.symbol.strip().upper() != symbol:
            reasons.append("forecast_symbol_mismatch")
        if forecast.direction != request.direction:
            reasons.append("forecast_direction_mismatch")
        try:
            forecast_time = _utc(forecast.decision_time)
        except ValueError:
            forecast_time = datetime.min.replace(tzinfo=timezone.utc)
            reasons.append("invalid_forecast_decision_time")
        if forecast_time != decision_time:
            reasons.append("forecast_decision_time_mismatch")
        self._validate_timestamp_age(
            value=forecast.latest_input_time,
            decision_time=decision_time,
            maximum_age_seconds=self.config.max_funding_age_seconds,
            missing_reason="missing_funding_forecast_input",
            stale_reason="stale_funding_forecast",
            future_reason="future_funding_forecast",
            reasons=reasons,
        )
        try:
            confidence = float(request.forecast_confidence)
        except (TypeError, ValueError, OverflowError):
            confidence = math.nan
        if not _finite(confidence) or not 0.0 <= confidence <= 1.0:
            reasons.append("invalid_forecast_confidence")
        elif confidence < self.config.minimum_forecast_confidence:
            reasons.append("low_forecast_confidence")
        if not forecast.payments:
            reasons.append("no_settlement_in_horizon")

        entry_capacity = request.entry_capacity
        exit_capacity = request.exit_capacity
        for operation, capacity in (
            ("entry", entry_capacity),
            ("exit", exit_capacity),
        ):
            if capacity is None:
                reasons.append(f"missing_{operation}_paired_books")
                continue
            if capacity.symbol.strip().upper() != symbol:
                reasons.append(f"{operation}_book_symbol_mismatch")
            if capacity.direction != request.direction:
                reasons.append(f"{operation}_book_direction_mismatch")
            if capacity.operation != operation:
                reasons.append(f"{operation}_book_operation_mismatch")
            if not math.isclose(
                capacity.requested_notional_usd,
                approved,
                rel_tol=1e-12,
                abs_tol=1e-6,
            ):
                reasons.append(f"{operation}_book_walk_size_mismatch")
            if not capacity.fully_executable:
                reasons.append(f"{operation}_paired_capacity_unavailable")
                reasons.extend(
                    f"{operation}_book:{reason}"
                    for reason in capacity.rejection_reasons
                )
            if capacity.executable_notional_usd + 1e-6 < approved:
                reasons.append(f"{operation}_insufficient_executable_capacity")

        spreads = (request.spot_spread_bps, request.perp_spread_bps)
        if any(
            spread is None
            or not _finite(spread)
            or float(spread) < 0.0
            for spread in spreads
        ):
            reasons.append("invalid_paired_spreads")

        # Cap reduction is observable but is not itself a rejection.  All other
        # reasons are fail-closed gates and prevent economic promotion.
        hard_reasons = [
            reason for reason in _dedupe(reasons) if reason != "notional_reduced_by_cap"
        ]
        if hard_reasons:
            return self._reject(request, reasons, approved)

        assert entry_capacity is not None
        assert exit_capacity is not None
        assert request.spot_spread_bps is not None
        assert request.perp_spread_bps is not None
        entry_cost = paired_exact_book_cost_breakdown(
            entry_capacity,
            spot_spread_bps=request.spot_spread_bps,
            perp_spread_bps=request.perp_spread_bps,
            spot_fee_pct=self.config.spot_taker_fee_pct,
            perp_fee_pct=self.config.perp_taker_fee_pct,
        )
        exit_cost = paired_exact_book_cost_breakdown(
            exit_capacity,
            spot_spread_bps=request.spot_spread_bps,
            perp_spread_bps=request.perp_spread_bps,
            spot_fee_pct=self.config.spot_taker_fee_pct,
            perp_fee_pct=self.config.perp_taker_fee_pct,
        )
        collateral_committed = approved / self.config.effective_max_leverage
        units = EconomicUnitSnapshot.matched(
            leg_notional_usd=approved,
            collateral_usd=collateral_committed,
            margin_exposure_usd=approved,
        )

        operational_probability = self._request_value(
            request.operational_hazard_probability,
            self.config.default_operational_hazard_probability,
            "operational_hazard_probability",
            reasons,
            probability=True,
        )
        reversal_hazard_bps = self._request_value(
            request.reversal_hazard_bps_per_settlement,
            self.config.default_reversal_hazard_bps_per_settlement,
            "reversal_hazard_bps_per_settlement",
            reasons,
        )
        operational_hazard_bps = self._request_value(
            request.operational_hazard_bps_per_settlement,
            self.config.default_operational_hazard_bps_per_settlement,
            "operational_hazard_bps_per_settlement",
            reasons,
        )
        liquidation_tail_bps = self._request_value(
            request.liquidation_tail_bps_per_settlement,
            self.config.default_liquidation_tail_bps_per_settlement,
            "liquidation_tail_bps_per_settlement",
            reasons,
        )
        borrow_bps_hour = self._request_value(
            request.borrow_cost_bps_per_hour,
            self.config.default_borrow_cost_bps_per_hour,
            "borrow_cost_bps_per_hour",
            reasons,
        )
        collateral_bps_hour = self._request_value(
            request.collateral_cost_bps_per_hour,
            self.config.default_collateral_cost_bps_per_hour,
            "collateral_cost_bps_per_hour",
            reasons,
        )
        capital_bps_hour = self._request_value(
            request.capital_opportunity_cost_bps_per_hour,
            self.config.default_capital_opportunity_cost_bps_per_hour,
            "capital_opportunity_cost_bps_per_hour",
            reasons,
        )

        payments = forecast.payments[: self.config.max_settlements]
        raw_rate: RawSettlementRate | None = None
        direction_sign = 1.0 if request.direction == "long_spot_short_perp" else -1.0
        entry_cost_usd = entry_cost.total_pct * approved
        exit_cost_usd = exit_cost.total_pct * approved
        cumulative_basis_mean = 0.0
        cumulative_basis_lower = 0.0
        mean_settlements: list[SettlementExpectation] = []
        lower_settlements: list[SettlementExpectation] = []
        horizons: list[HorizonEvaluation] = []
        previous_settlement: datetime | None = None
        for index, payment in enumerate(payments):
            try:
                settlement_time = _utc(payment.settlement_time)
            except ValueError:
                reasons.append("invalid_settlement_timestamp")
                break
            if settlement_time <= decision_time:
                reasons.append("non_prospective_settlement")
                break
            if previous_settlement is not None and settlement_time <= previous_settlement:
                reasons.append("unordered_or_duplicate_settlement")
                break
            previous_settlement = settlement_time

            missed_probability = (
                self._series_value(request.missed_settlement_probabilities, index)
                if index < len(request.missed_settlement_probabilities)
                else self.config.default_missed_settlement_probability
            )
            if not _finite(missed_probability) or not 0.0 <= missed_probability <= 1.0:
                reasons.append("invalid_missed_settlement_probability")
                break
            survival_probability = (1.0 - operational_probability) ** (index + 1)
            eligibility_probability = (1.0 - missed_probability) * survival_probability

            try:
                mean_rate = float(payment.mean_rate)
                lower_market_rate = (
                    float(payment.lower_rate)
                    if direction_sign > 0.0
                    else float(payment.upper_rate)
                )
            except (TypeError, ValueError, OverflowError):
                mean_rate = math.nan
                lower_market_rate = math.nan
            if not _finite(mean_rate) or not _finite(lower_market_rate):
                reasons.append("invalid_settlement_rate_distribution")
                break
            if index == 0:
                raw_rate = RawSettlementRate(mean_rate)
            mean_settlements.append(
                SettlementExpectation(
                    settlement_time=settlement_time,
                    expected_rate=mean_rate,
                    eligibility_probability=eligibility_probability,
                    source_event_id=f"decision-mean:{index}",
                )
            )
            lower_settlements.append(
                SettlementExpectation(
                    settlement_time=settlement_time,
                    expected_rate=lower_market_rate,
                    eligibility_probability=eligibility_probability,
                    source_event_id=f"decision-lower:{index}",
                )
            )

            basis_mean_bps = self._series_value(
                request.basis_expected_bps_by_settlement, index
            )
            basis_lower_bps = self._series_value(
                request.basis_lower_bps_by_settlement, index
            )
            if not _finite(basis_mean_bps) or not _finite(basis_lower_bps):
                reasons.append("invalid_basis_distribution")
                break
            if basis_lower_bps > basis_mean_bps:
                reasons.append("basis_lower_bound_exceeds_mean")
                break
            cumulative_basis_mean += approved * basis_mean_bps / 10_000.0
            cumulative_basis_lower += approved * basis_lower_bps / 10_000.0

            settlement_count = index + 1
            holding_hours = (
                (settlement_time - decision_time).total_seconds() / 3_600.0
                + self.config.exit_delay_hours
            )
            borrow_cost = approved * borrow_bps_hour * holding_hours / 10_000.0
            collateral_cost = (
                collateral_committed
                * collateral_bps_hour
                * holding_hours
                / 10_000.0
            )
            capital_cost = approved * capital_bps_hour * holding_hours / 10_000.0
            reversal_cost = (
                approved * reversal_hazard_bps * settlement_count / 10_000.0
            )
            operational_cost = (
                approved * operational_hazard_bps * settlement_count / 10_000.0
            )
            liquidation_cost = (
                approved * liquidation_tail_bps * settlement_count / 10_000.0
            )
            shared_kernel_values = {
                "symbol": symbol,
                "direction": request.direction,
                "decision_time": decision_time,
                "horizon_end": settlement_time,
                "pair_gross_notional_usd": units.pair_gross.value,
                "funding_liable_notional_usd": approved,
                "settlement_interval_hours": float(forecast.interval_hours),
                "entry_execution_cost_pct": entry_cost.total_pct,
                "exit_execution_cost_pct": exit_cost.total_pct,
                "minimum_net_edge_bps": -1_000_000_000.0,
                "borrow_and_financing_usd": borrow_cost + collateral_cost,
                "capital_cost_usd": capital_cost,
                "repair_and_failure_allowance_usd": (
                    reversal_cost + operational_cost + liquidation_cost
                ),
                "calendar_authoritative": True,
                "calendar_observed_at": decision_time,
                "funding_rate_observed_at": decision_time,
                "max_calendar_age_seconds": 1.0,
                "max_funding_rate_age_seconds": 1.0,
            }
            mean_evaluation = evaluate_opportunity(
                OpportunityEvaluationInput(
                    **shared_kernel_values,
                    settlements=tuple(mean_settlements),
                    basis_expected_pnl_usd=cumulative_basis_mean,
                )
            )
            lower_evaluation = evaluate_opportunity(
                OpportunityEvaluationInput(
                    **shared_kernel_values,
                    settlements=tuple(lower_settlements),
                    basis_expected_pnl_usd=cumulative_basis_lower,
                )
            )
            if not mean_evaluation.valid or not lower_evaluation.valid:
                reasons.extend(
                    f"opportunity_kernel:{reason}"
                    for reason in _dedupe(
                        mean_evaluation.reason_codes
                        + lower_evaluation.reason_codes
                    )
                )
                break
            mean_ev = mean_evaluation.net_ev_usd
            lower_ev = lower_evaluation.net_ev_usd
            horizons.append(
                HorizonEvaluation(
                    settlement_count=settlement_count,
                    holding_hours=holding_hours,
                    last_settlement_time=settlement_time,
                    funding_mean_usd=mean_evaluation.gross_funding_usd,
                    funding_lower_usd=lower_evaluation.gross_funding_usd,
                    basis_mean_usd=cumulative_basis_mean,
                    basis_lower_usd=cumulative_basis_lower,
                    entry_execution_cost_usd=entry_cost_usd,
                    exit_execution_cost_usd=exit_cost_usd,
                    borrow_cost_usd=borrow_cost,
                    collateral_cost_usd=collateral_cost,
                    capital_opportunity_cost_usd=capital_cost,
                    reversal_hazard_usd=reversal_cost,
                    operational_hazard_usd=operational_cost,
                    liquidation_tail_usd=liquidation_cost,
                    mean_net_ev_usd=mean_ev,
                    lower_bound_net_ev_usd=lower_ev,
                    mean_net_edge_bps=mean_evaluation.net_edge_bps,
                    lower_bound_net_edge_bps=lower_evaluation.net_edge_bps,
                    lower_bound_pair_gross_bps=(
                        lower_evaluation.net_edge_pair_gross_bps
                    ),
                )
            )

        hard_after_economics = [
            reason
            for reason in _dedupe(reasons)
            if reason != "notional_reduced_by_cap"
        ]
        horizon_tuple = tuple(horizons)
        if hard_after_economics or not horizon_tuple:
            if not horizon_tuple and "no_settlement_in_horizon" not in reasons:
                reasons.append("no_valid_settlement_horizon")
            return self._reject(
                request,
                reasons,
                approved,
                units=units,
                raw_rate=raw_rate,
                horizons=horizon_tuple,
                entry_cost=entry_cost,
                exit_cost=exit_cost,
            )

        selected = max(
            horizon_tuple,
            key=lambda item: (item.lower_bound_net_ev_usd, -item.settlement_count),
        )
        if selected.lower_bound_net_ev_usd <= self.config.minimum_lower_bound_ev_usd:
            reasons.append("non_positive_lower_bound_net_ev")
        if (
            selected.lower_bound_net_edge_bps
            <= self.config.minimum_lower_bound_edge_bps
        ):
            reasons.append("lower_bound_edge_below_minimum")
        economic_rejections = {
            "non_positive_lower_bound_net_ev",
            "lower_bound_edge_below_minimum",
        }
        eligible = not economic_rejections.intersection(reasons)
        action: DecisionAction = "enter" if eligible else "reject"
        return Decision(
            engine_version=DECISION_ENGINE_VERSION,
            config_hash=self.config_hash,
            action=action,
            eligible=eligible,
            symbol=symbol,
            direction=request.direction,
            reason_codes=_dedupe(reasons),
            requested_leg_notional_usd=float(request.requested_leg_notional_usd),
            approved_leg_notional_usd=approved,
            economic_units=units,
            raw_first_settlement_rate=raw_rate,
            reporting_annualized_rate=(
                raw_rate.reporting_annualized if raw_rate is not None else None
            ),
            selected_settlement_count=selected.settlement_count,
            selected_horizon=selected,
            horizons=horizon_tuple,
            entry_cost=entry_cost,
            exit_cost=exit_cost,
        )


__all__ = [
    "DECISION_ENGINE_VERSION",
    "Decision",
    "DecisionAction",
    "DecisionEngine",
    "DecisionEngineConfig",
    "DecisionRequest",
    "DecisionSurface",
    "HorizonEvaluation",
    "PortfolioSelection",
    "canonical_config_hash",
    "canonical_config_payload",
]
