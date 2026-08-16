"""Fixed-protocol purged walk-forward evaluation for cross-venue outcomes."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Final, Literal

from bongus.research.cross_venue.schema import (
    CanonicalAsset,
    ReservedCapital,
    epoch_nanoseconds,
    exact_decimal,
    exact_wire,
    nonnegative_decimal,
    positive_decimal,
)
from bongus.research.cross_venue.storage import canonical_json_bytes

NANOSECONDS_PER_DAY: Final[int] = 86_400_000_000_000
PREDECLARED_UNIVERSE: Final[tuple[CanonicalAsset, ...]] = (
    CanonicalAsset.BTC,
    CanonicalAsset.ETH,
    CanonicalAsset.SOL,
    CanonicalAsset.XRP,
    CanonicalAsset.DOGE,
)
PREREGISTRATION_PATH: Final[Path] = (
    Path(__file__).resolve().parents[3] / "research" / "experiments" / "binance_hyperliquid_v1.json"
)


def _reject_nonfinite_json(value: str) -> object:
    raise ValueError(f"non-finite JSON value: {value}")


@dataclass(frozen=True, slots=True)
class SensitivityCase:
    name: str
    additional_fee_bps: Decimal = Decimal("0")
    slippage_multiplier: Decimal = Decimal("1")
    second_leg_delay_ms: int = 0
    funding_multiplier: Decimal = Decimal("1")
    stablecoin_deviation_fraction: Decimal = Decimal("0")
    venue_outage_hours: Decimal = Decimal("0")
    outage_venue: Literal["none", "binance", "hyperliquid"] = "none"
    explicit_loss: Literal[
        "none",
        "exit_depth_50pct",
        "exit_depth_90pct",
        "underlying_up_30pct",
        "underlying_down_30pct",
        "basis_widening",
        "delisting",
        "open_interest_cap",
        "adl",
        "liquidation",
        "worse_leg_order",
    ] = "none"

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("sensitivity case name is required")
        object.__setattr__(self, "name", self.name.strip())
        for field_name in (
            "additional_fee_bps",
            "stablecoin_deviation_fraction",
            "venue_outage_hours",
        ):
            object.__setattr__(
                self,
                field_name,
                nonnegative_decimal(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "slippage_multiplier",
            positive_decimal(self.slippage_multiplier, "slippage_multiplier"),
        )
        object.__setattr__(
            self,
            "funding_multiplier",
            exact_decimal(self.funding_multiplier, "funding_multiplier"),
        )
        if (
            isinstance(self.second_leg_delay_ms, bool)
            or not isinstance(self.second_leg_delay_ms, int)
            or self.second_leg_delay_ms < 0
        ):
            raise ValueError("second_leg_delay_ms must be a non-negative integer")
        if self.outage_venue not in ("none", "binance", "hyperliquid"):
            raise ValueError("outage_venue is outside the fixed stress contract")
        allowed_losses = {
            "none",
            "exit_depth_50pct",
            "exit_depth_90pct",
            "underlying_up_30pct",
            "underlying_down_30pct",
            "basis_widening",
            "delisting",
            "open_interest_cap",
            "adl",
            "liquidation",
            "worse_leg_order",
        }
        if self.explicit_loss not in allowed_losses:
            raise ValueError("explicit_loss is outside the fixed stress contract")
        if self.venue_outage_hours > 0 and self.outage_venue == "none":
            raise ValueError("venue outage stress must identify the unavailable venue")
        if self.venue_outage_hours == 0 and self.outage_venue != "none":
            raise ValueError("outage venue requires a positive outage duration")


FIXED_SENSITIVITY_GRID: Final[tuple[SensitivityCase, ...]] = (
    SensitivityCase("baseline"),
    SensitivityCase("fees_plus_5bp", additional_fee_bps=Decimal("5")),
    SensitivityCase("fees_plus_10bp", additional_fee_bps=Decimal("10")),
    SensitivityCase("slippage_x1_5", slippage_multiplier=Decimal("1.5")),
    SensitivityCase("slippage_x2", slippage_multiplier=Decimal("2")),
    SensitivityCase("second_leg_delay_300ms", second_leg_delay_ms=300),
    SensitivityCase("second_leg_delay_1s", second_leg_delay_ms=1_000),
    SensitivityCase("second_leg_delay_5s", second_leg_delay_ms=5_000),
    SensitivityCase("funding_haircut_25pct", funding_multiplier=Decimal("0.75")),
    SensitivityCase("funding_haircut_50pct", funding_multiplier=Decimal("0.5")),
    SensitivityCase("funding_sign_reversal", funding_multiplier=Decimal("-1")),
    SensitivityCase("missed_funding", funding_multiplier=Decimal("0")),
    SensitivityCase("exit_depth_reduced_50pct", explicit_loss="exit_depth_50pct"),
    SensitivityCase("exit_depth_reduced_90pct", explicit_loss="exit_depth_90pct"),
    SensitivityCase(
        "usdc_usdt_deviation_0_5pct",
        stablecoin_deviation_fraction=Decimal("0.005"),
    ),
    SensitivityCase(
        "usdc_usdt_deviation_1pct",
        stablecoin_deviation_fraction=Decimal("0.01"),
    ),
    SensitivityCase(
        "usdc_usdt_deviation_5pct",
        stablecoin_deviation_fraction=Decimal("0.05"),
    ),
    SensitivityCase(
        "binance_outage_1h",
        venue_outage_hours=Decimal("1"),
        outage_venue="binance",
    ),
    SensitivityCase(
        "binance_outage_8h",
        venue_outage_hours=Decimal("8"),
        outage_venue="binance",
    ),
    SensitivityCase(
        "binance_outage_24h",
        venue_outage_hours=Decimal("24"),
        outage_venue="binance",
    ),
    SensitivityCase(
        "hyperliquid_outage_1h",
        venue_outage_hours=Decimal("1"),
        outage_venue="hyperliquid",
    ),
    SensitivityCase(
        "hyperliquid_outage_8h",
        venue_outage_hours=Decimal("8"),
        outage_venue="hyperliquid",
    ),
    SensitivityCase(
        "hyperliquid_outage_24h",
        venue_outage_hours=Decimal("24"),
        outage_venue="hyperliquid",
    ),
    SensitivityCase("underlying_move_plus_30pct", explicit_loss="underlying_up_30pct"),
    SensitivityCase("underlying_move_minus_30pct", explicit_loss="underlying_down_30pct"),
    SensitivityCase("cross_venue_basis_widening", explicit_loss="basis_widening"),
    SensitivityCase("delisting", explicit_loss="delisting"),
    SensitivityCase("open_interest_cap", explicit_loss="open_interest_cap"),
    SensitivityCase("adl", explicit_loss="adl"),
    SensitivityCase("liquidation", explicit_loss="liquidation"),
    SensitivityCase("worse_leg_execution_order", explicit_loss="worse_leg_order"),
)


@dataclass(frozen=True, slots=True)
class EvaluationProtocol:
    protocol_id: str = "binance-hyperliquid-v1"
    universe: tuple[CanonicalAsset, ...] = PREDECLARED_UNIVERSE
    sensitivity_grid: tuple[SensitivityCase, ...] = FIXED_SENSITIVITY_GRID
    purge_days: int = 30
    embargo_days: int = 1

    def __post_init__(self) -> None:
        if self.protocol_id != "binance-hyperliquid-v1":
            raise ValueError("protocol changes require a new preregistration version")
        if self.universe != PREDECLARED_UNIVERSE:
            raise ValueError("the v1 universe is immutable")
        if self.sensitivity_grid != FIXED_SENSITIVITY_GRID:
            raise ValueError("the v1 sensitivity grid is immutable")
        if self.purge_days != 30 or self.embargo_days != 1:
            raise ValueError("v1 purge and embargo durations are immutable")
        for name in ("purge_days", "embargo_days"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        self._validate_preregistration()

    def _preregistration_payload(self) -> Mapping[str, object]:
        if not PREREGISTRATION_PATH.is_file():
            raise ValueError("the frozen v1 preregistration artifact is missing")
        payload = json.loads(
            PREREGISTRATION_PATH.read_text(encoding="utf-8"),
            parse_float=Decimal,
            parse_int=int,
            parse_constant=_reject_nonfinite_json,
        )
        if not isinstance(payload, Mapping):
            raise ValueError("the frozen v1 preregistration must be an object")
        return payload

    def _validate_preregistration(self) -> None:
        payload = self._preregistration_payload()
        evaluation = payload.get("evaluation")
        statistics = payload.get("statistics")
        thresholds = payload.get("verdict_thresholds")
        if not all(isinstance(value, Mapping) for value in (evaluation, statistics, thresholds)):
            raise ValueError("preregistration evaluation/statistics/verdict contract is missing")
        assert isinstance(evaluation, Mapping)
        assert isinstance(statistics, Mapping)
        assert isinstance(thresholds, Mapping)
        expected_universe = [asset.value for asset in self.universe]
        expected_grid = [case.name for case in self.sensitivity_grid]
        if (
            payload.get("protocol_id") != self.protocol_id
            or payload.get("universe") != expected_universe
            or payload.get("sensitivity_grid") != expected_grid
            or evaluation.get("purge_days") != self.purge_days
            or evaluation.get("embargo_days") != self.embargo_days
            or evaluation.get("adaptive_parameter_search") is not False
            or evaluation.get("machine_learning") is not False
            or statistics.get("block_units") != ["daily", "weekly"]
            or statistics.get("bootstrap_samples") != 2_000
            or statistics.get("bootstrap_seed") != "binance-hyperliquid-v1-deterministic-block-bootstrap"
            or statistics.get("one_sided_alpha") != "0.05"
            or statistics.get("preserve_cross_asset_and_cross_venue_dependence") is not True
            or thresholds.get("collector_qa_only_before_days") != 14
            or thresholds.get("minimum_forward_oos_days") != 90
            or thresholds.get("inconclusive_extension_days") != 180
            or thresholds.get("sealed_final_days") != 30
            or thresholds.get("minimum_viable_simple_annualized_return") != "0.05"
            or thresholds.get("strong_simple_annualized_return") != "0.12"
            or thresholds.get("maximum_drawdown") != "0.10"
            or thresholds.get("maximum_top_five_profit_contribution") != "0.30"
            or thresholds.get("minimum_depth_multiple") != "5"
            or thresholds.get("minimum_storage_sizing_pilot_hours") != 48
            or thresholds.get("require_positive_leave_one_symbol_out") is not True
            or thresholds.get("require_positive_leave_one_month_out") is not True
            or thresholds.get("require_positive_funding_minus_cost_without_favorable_basis") is not True
            or thresholds.get("require_positive_vs_no_trade_and_binance_only") is not True
            or thresholds.get("require_all_stress_inputs_and_liquidation_survival") is not True
            or thresholds.get("grant_live_authority") is not False
            or not payload.get("null_hypothesis")
            or not payload.get("stop_rules")
        ):
            raise ValueError("code and frozen v1 preregistration do not match")

    @property
    def preregistration_sha256(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self._preregistration_payload())).hexdigest()

    @property
    def protocol_sha256(self) -> str:
        return hashlib.sha256(
            canonical_json_bytes(
                {
                    "protocol_id": self.protocol_id,
                    "universe": self.universe,
                    "sensitivity_grid": self.sensitivity_grid,
                    "purge_days": self.purge_days,
                    "embargo_days": self.embargo_days,
                    "preregistration_sha256": self.preregistration_sha256,
                }
            )
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class OpportunityOutcome:
    event_id: str
    canonical_asset: CanonicalAsset
    decision_time_ns: int
    outcome_end_time_ns: int
    available_time_ns: int
    holding_period_days: Decimal
    funding_pnl_usd: Decimal
    executable_price_pnl_usd: Decimal
    commissions_usd: Decimal
    stablecoin_conversion_cost_usd: Decimal
    collateral_opportunity_cost_usd: Decimal
    repair_failure_cost_usd: Decimal
    executed_pair_notional_usd: Decimal
    measured_slippage_cost_usd: Decimal
    delay_cost_usd_per_second: Decimal
    outage_cost_usd_per_hour: Decimal
    reserved_capital: ReservedCapital
    binance_outage_cost_usd_per_hour: Decimal = Decimal("0")
    hyperliquid_outage_cost_usd_per_hour: Decimal = Decimal("0")
    exit_depth_50pct_loss_usd: Decimal = Decimal("0")
    exit_depth_90pct_loss_usd: Decimal = Decimal("0")
    underlying_up_30pct_loss_usd: Decimal = Decimal("0")
    underlying_down_30pct_loss_usd: Decimal = Decimal("0")
    basis_widening_loss_usd: Decimal = Decimal("0")
    delisting_loss_usd: Decimal = Decimal("0")
    open_interest_cap_loss_usd: Decimal = Decimal("0")
    adl_loss_usd: Decimal = Decimal("0")
    liquidation_loss_usd: Decimal = Decimal("0")
    worse_leg_order_loss_usd: Decimal = Decimal("0")
    binance_only_net_pnl_usd: Decimal = Decimal("0")
    quality_flags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.event_id, str) or not self.event_id.strip():
            raise ValueError("outcome event_id is required")
        if not isinstance(self.canonical_asset, CanonicalAsset):
            raise TypeError("outcome canonical_asset must use the fixed enum")
        decision = epoch_nanoseconds(self.decision_time_ns, "decision_time_ns")
        end = epoch_nanoseconds(self.outcome_end_time_ns, "outcome_end_time_ns")
        available = epoch_nanoseconds(self.available_time_ns, "available_time_ns")
        if not decision < end <= available:
            raise ValueError("outcome timestamps must satisfy decision < end <= availability")
        object.__setattr__(self, "decision_time_ns", decision)
        object.__setattr__(self, "outcome_end_time_ns", end)
        object.__setattr__(self, "available_time_ns", available)
        object.__setattr__(
            self,
            "holding_period_days",
            positive_decimal(self.holding_period_days, "holding_period_days"),
        )
        for name in ("funding_pnl_usd", "executable_price_pnl_usd"):
            object.__setattr__(self, name, exact_decimal(getattr(self, name), name))
        for name in (
            "commissions_usd",
            "stablecoin_conversion_cost_usd",
            "collateral_opportunity_cost_usd",
            "repair_failure_cost_usd",
            "executed_pair_notional_usd",
            "measured_slippage_cost_usd",
            "delay_cost_usd_per_second",
            "outage_cost_usd_per_hour",
            "binance_outage_cost_usd_per_hour",
            "hyperliquid_outage_cost_usd_per_hour",
            "exit_depth_50pct_loss_usd",
            "exit_depth_90pct_loss_usd",
            "underlying_up_30pct_loss_usd",
            "underlying_down_30pct_loss_usd",
            "basis_widening_loss_usd",
            "delisting_loss_usd",
            "open_interest_cap_loss_usd",
            "adl_loss_usd",
            "liquidation_loss_usd",
            "worse_leg_order_loss_usd",
        ):
            object.__setattr__(self, name, nonnegative_decimal(getattr(self, name), name))
        object.__setattr__(
            self,
            "binance_only_net_pnl_usd",
            exact_decimal(self.binance_only_net_pnl_usd, "binance_only_net_pnl_usd"),
        )
        if not isinstance(self.reserved_capital, ReservedCapital):
            raise TypeError("outcome requires the exact ReservedCapital contract")
        if any(not isinstance(flag, str) or not flag.strip() for flag in self.quality_flags):
            raise ValueError("outcome quality flags must be non-empty strings")
        normalized_flags = tuple(sorted(flag.strip() for flag in self.quality_flags))
        if len(normalized_flags) != len(set(normalized_flags)):
            raise ValueError("outcome quality flags must be unique")
        object.__setattr__(self, "quality_flags", normalized_flags)

    @property
    def baseline_net_pnl_usd(self) -> Decimal:
        return (
            self.funding_pnl_usd
            + self.executable_price_pnl_usd
            - self.commissions_usd
            - self.stablecoin_conversion_cost_usd
            - self.collateral_opportunity_cost_usd
            - self.repair_failure_cost_usd
        )


@dataclass(frozen=True, slots=True)
class EvaluationWindow:
    window_id: str
    train_start_ns: int
    train_end_ns: int
    test_start_ns: int
    test_end_ns: int

    def __post_init__(self) -> None:
        if not isinstance(self.window_id, str) or not self.window_id.strip():
            raise ValueError("window_id is required")
        values = tuple(
            epoch_nanoseconds(getattr(self, name), name)
            for name in (
                "train_start_ns",
                "train_end_ns",
                "test_start_ns",
                "test_end_ns",
            )
        )
        if not values[0] < values[1] <= values[2] < values[3]:
            raise ValueError("window timestamps must be ordered and non-overlapping")
        for name, value in zip(
            ("train_start_ns", "train_end_ns", "test_start_ns", "test_end_ns"),
            values,
            strict=True,
        ):
            object.__setattr__(self, name, value)


@dataclass(frozen=True, slots=True)
class ScenarioMetrics:
    scenario_name: str
    outcomes: int
    total_net_pnl_usd: Decimal
    total_reserved_capital_days: Decimal
    net_usd_per_reserved_capital_day: Decimal
    simple_annualized_return: Decimal


@dataclass(frozen=True, slots=True)
class PurgedWindowResult:
    window_id: str
    train_candidates: int
    purged_train_outcomes: int
    retained_train_outcomes: int
    out_of_sample_outcomes: int
    scenario_metrics: tuple[ScenarioMetrics, ...]


@dataclass(frozen=True, slots=True)
class WalkForwardEvaluationReport:
    protocol_id: str
    protocol_sha256: str
    preregistration_sha256: str
    universe: tuple[CanonicalAsset, ...]
    sensitivity_case_names: tuple[str, ...]
    windows: tuple[PurgedWindowResult, ...]
    unique_outcomes: int

    def _payload(self) -> Mapping[str, object]:
        return {
            "protocol_id": self.protocol_id,
            "protocol_sha256": self.protocol_sha256,
            "preregistration_sha256": self.preregistration_sha256,
            "universe": self.universe,
            "sensitivity_case_names": self.sensitivity_case_names,
            "windows": self.windows,
            "unique_outcomes": self.unique_outcomes,
        }

    @property
    def report_sha256(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self._payload())).hexdigest()

    def as_wire(self) -> Mapping[str, object]:
        return {
            **cast_wire_mapping(self._payload()),
            "report_sha256": self.report_sha256,
        }


def cast_wire_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    wire = exact_wire(value)
    if not isinstance(wire, Mapping):
        raise TypeError("evaluation report wire payload must be an object")
    return wire


def _scenario_net(outcome: OpportunityOutcome, case: SensitivityCase) -> Decimal:
    incremental_fee = outcome.executed_pair_notional_usd * case.additional_fee_bps / Decimal("10000")
    incremental_slippage = outcome.measured_slippage_cost_usd * (case.slippage_multiplier - Decimal("1"))
    delay_cost = outcome.delay_cost_usd_per_second * Decimal(case.second_leg_delay_ms) / Decimal("1000")
    stablecoin_stress = outcome.reserved_capital.hyperliquid_collateral_usd * case.stablecoin_deviation_fraction
    outage_rate = outcome.outage_cost_usd_per_hour
    if case.outage_venue == "binance" and outcome.binance_outage_cost_usd_per_hour > 0:
        outage_rate = outcome.binance_outage_cost_usd_per_hour
    elif case.outage_venue == "hyperliquid" and outcome.hyperliquid_outage_cost_usd_per_hour > 0:
        outage_rate = outcome.hyperliquid_outage_cost_usd_per_hour
    outage_cost = outage_rate * case.venue_outage_hours
    explicit_losses = {
        "none": Decimal("0"),
        "exit_depth_50pct": outcome.exit_depth_50pct_loss_usd,
        "exit_depth_90pct": outcome.exit_depth_90pct_loss_usd,
        "underlying_up_30pct": outcome.underlying_up_30pct_loss_usd,
        "underlying_down_30pct": outcome.underlying_down_30pct_loss_usd,
        "basis_widening": outcome.basis_widening_loss_usd,
        "delisting": outcome.delisting_loss_usd,
        "open_interest_cap": outcome.open_interest_cap_loss_usd,
        "adl": outcome.adl_loss_usd,
        "liquidation": outcome.liquidation_loss_usd,
        "worse_leg_order": outcome.worse_leg_order_loss_usd,
    }
    return (
        outcome.funding_pnl_usd * case.funding_multiplier
        + outcome.executable_price_pnl_usd
        - outcome.commissions_usd
        - outcome.stablecoin_conversion_cost_usd
        - outcome.collateral_opportunity_cost_usd
        - outcome.repair_failure_cost_usd
        - incremental_fee
        - incremental_slippage
        - delay_cost
        - stablecoin_stress
        - outage_cost
        - explicit_losses[case.explicit_loss]
    )


def _metrics(outcomes: Sequence[OpportunityOutcome], case: SensitivityCase) -> ScenarioMetrics:
    total_net = sum((_scenario_net(outcome, case) for outcome in outcomes), Decimal("0"))
    capital_days = sum(
        (outcome.reserved_capital.total_usd * outcome.holding_period_days for outcome in outcomes),
        Decimal("0"),
    )
    per_capital_day = total_net / capital_days if capital_days > 0 else Decimal("0")
    return ScenarioMetrics(
        scenario_name=case.name,
        outcomes=len(outcomes),
        total_net_pnl_usd=total_net,
        total_reserved_capital_days=capital_days,
        net_usd_per_reserved_capital_day=per_capital_day,
        simple_annualized_return=per_capital_day * Decimal("365"),
    )


def evaluate_sensitivity_metrics(
    outcomes: Sequence[OpportunityOutcome],
) -> tuple[ScenarioMetrics, ...]:
    """Evaluate every preregistered stress without selecting among scenarios."""

    values = tuple(outcomes)
    if any(outcome.quality_flags for outcome in values):
        raise ValueError("gapped or quality-flagged outcomes cannot be stress evaluated")
    return tuple(_metrics(values, case) for case in FIXED_SENSITIVITY_GRID)


class PurgedWalkForwardEvaluator:
    """Evaluate one frozen policy; no parameter search or adaptive universe."""

    def __init__(self, protocol: EvaluationProtocol | None = None) -> None:
        self.protocol = protocol or EvaluationProtocol()

    def evaluate(
        self,
        outcomes: Sequence[OpportunityOutcome],
        windows: Sequence[EvaluationWindow],
    ) -> WalkForwardEvaluationReport:
        seen_ids: set[str] = set()
        ordered = tuple(sorted(outcomes, key=lambda item: item.decision_time_ns))
        for outcome in ordered:
            if outcome.event_id in seen_ids:
                raise ValueError(f"duplicate outcome event_id: {outcome.event_id}")
            seen_ids.add(outcome.event_id)
            if outcome.canonical_asset not in self.protocol.universe:
                raise ValueError("outcome is outside the predeclared universe")
            if outcome.quality_flags:
                raise ValueError("gapped or quality-flagged outcomes cannot be evaluated")
        purge_ns = self.protocol.purge_days * NANOSECONDS_PER_DAY
        embargo_ns = self.protocol.embargo_days * NANOSECONDS_PER_DAY
        results: list[PurgedWindowResult] = []
        previous_test_end: int | None = None
        for window in windows:
            if window.test_start_ns - window.train_end_ns < embargo_ns:
                raise ValueError("evaluation window violates the preregistered embargo")
            if previous_test_end is not None and window.test_start_ns < previous_test_end:
                raise ValueError("out-of-sample windows must not overlap")
            previous_test_end = window.test_end_ns
            train_candidates = tuple(
                outcome
                for outcome in ordered
                if window.train_start_ns <= outcome.decision_time_ns < window.train_end_ns
                and outcome.available_time_ns <= window.train_end_ns
            )
            purge_boundary = window.test_start_ns - purge_ns
            retained_train = tuple(
                outcome for outcome in train_candidates if outcome.outcome_end_time_ns < purge_boundary
            )
            test = tuple(
                outcome
                for outcome in ordered
                if window.test_start_ns <= outcome.decision_time_ns < window.test_end_ns
                and outcome.outcome_end_time_ns <= window.test_end_ns
                and outcome.available_time_ns <= window.test_end_ns
            )
            results.append(
                PurgedWindowResult(
                    window_id=window.window_id,
                    train_candidates=len(train_candidates),
                    purged_train_outcomes=len(train_candidates) - len(retained_train),
                    retained_train_outcomes=len(retained_train),
                    out_of_sample_outcomes=len(test),
                    scenario_metrics=tuple(_metrics(test, case) for case in self.protocol.sensitivity_grid),
                )
            )
        return WalkForwardEvaluationReport(
            protocol_id=self.protocol.protocol_id,
            protocol_sha256=self.protocol.protocol_sha256,
            preregistration_sha256=self.protocol.preregistration_sha256,
            universe=self.protocol.universe,
            sensitivity_case_names=tuple(case.name for case in self.protocol.sensitivity_grid),
            windows=tuple(results),
            unique_outcomes=len(ordered),
        )


def _mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a JSON object")
    return value


def _sequence(value: object, field_name: str) -> Sequence[object]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ValueError(f"{field_name} must be a JSON array")
    return value


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _decimal(value: object, field_name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, str, int)):
        raise ValueError(f"{field_name} must be an exact decimal")
    return exact_decimal(value, field_name)


def _integer(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{field_name} must be an exact integer")
    result = int(value)
    if not isinstance(value, int) and value.strip() != str(result):
        raise ValueError(f"{field_name} must be an integer string")
    return result


def load_evaluation_fixture(
    path: str | Path,
) -> tuple[tuple[OpportunityOutcome, ...], tuple[EvaluationWindow, ...]]:
    fixture_path = Path(path).resolve()
    root = json.loads(
        fixture_path.read_text(encoding="utf-8"),
        parse_float=Decimal,
        parse_int=int,
        parse_constant=_reject_nonfinite_json,
    )
    payload = _mapping(root, "evaluation fixture")
    outcomes: list[OpportunityOutcome] = []
    for index, value in enumerate(_sequence(payload.get("outcomes"), "outcomes")):
        row = _mapping(value, f"outcomes[{index}]")
        capital = _mapping(row.get("reserved_capital"), "reserved_capital")
        stress = _mapping(row.get("stress_losses", {}), "stress_losses")
        outcomes.append(
            OpportunityOutcome(
                event_id=_text(row.get("event_id"), "event_id"),
                canonical_asset=CanonicalAsset(_text(row.get("canonical_asset"), "canonical_asset")),
                decision_time_ns=_integer(row.get("decision_time_ns"), "decision_time_ns"),
                outcome_end_time_ns=_integer(row.get("outcome_end_time_ns"), "outcome_end_time_ns"),
                available_time_ns=_integer(row.get("available_time_ns"), "available_time_ns"),
                holding_period_days=_decimal(row.get("holding_period_days"), "holding_period_days"),
                funding_pnl_usd=_decimal(row.get("funding_pnl_usd"), "funding_pnl_usd"),
                executable_price_pnl_usd=_decimal(
                    row.get("executable_price_pnl_usd"),
                    "executable_price_pnl_usd",
                ),
                commissions_usd=_decimal(row.get("commissions_usd"), "commissions_usd"),
                stablecoin_conversion_cost_usd=_decimal(
                    row.get("stablecoin_conversion_cost_usd", "0"),
                    "stablecoin_conversion_cost_usd",
                ),
                collateral_opportunity_cost_usd=_decimal(
                    row.get("collateral_opportunity_cost_usd", "0"),
                    "collateral_opportunity_cost_usd",
                ),
                repair_failure_cost_usd=_decimal(
                    row.get("repair_failure_cost_usd", "0"),
                    "repair_failure_cost_usd",
                ),
                executed_pair_notional_usd=_decimal(
                    row.get("executed_pair_notional_usd"),
                    "executed_pair_notional_usd",
                ),
                measured_slippage_cost_usd=_decimal(
                    row.get("measured_slippage_cost_usd", "0"),
                    "measured_slippage_cost_usd",
                ),
                delay_cost_usd_per_second=_decimal(
                    row.get("delay_cost_usd_per_second", "0"),
                    "delay_cost_usd_per_second",
                ),
                outage_cost_usd_per_hour=_decimal(
                    row.get("outage_cost_usd_per_hour", "0"),
                    "outage_cost_usd_per_hour",
                ),
                reserved_capital=ReservedCapital(
                    binance_collateral_usd=_decimal(
                        capital.get("binance_collateral_usd"),
                        "binance_collateral_usd",
                    ),
                    hyperliquid_collateral_usd=_decimal(
                        capital.get("hyperliquid_collateral_usd"),
                        "hyperliquid_collateral_usd",
                    ),
                    liquidation_buffers_usd=_decimal(
                        capital.get("liquidation_buffers_usd"),
                        "liquidation_buffers_usd",
                    ),
                    idle_transfer_buffer_usd=_decimal(
                        capital.get("idle_transfer_buffer_usd"),
                        "idle_transfer_buffer_usd",
                    ),
                ),
                binance_outage_cost_usd_per_hour=_decimal(
                    stress.get("binance_outage_cost_usd_per_hour", "0"),
                    "binance_outage_cost_usd_per_hour",
                ),
                hyperliquid_outage_cost_usd_per_hour=_decimal(
                    stress.get("hyperliquid_outage_cost_usd_per_hour", "0"),
                    "hyperliquid_outage_cost_usd_per_hour",
                ),
                exit_depth_50pct_loss_usd=_decimal(
                    stress.get("exit_depth_50pct_loss_usd", "0"),
                    "exit_depth_50pct_loss_usd",
                ),
                exit_depth_90pct_loss_usd=_decimal(
                    stress.get("exit_depth_90pct_loss_usd", "0"),
                    "exit_depth_90pct_loss_usd",
                ),
                underlying_up_30pct_loss_usd=_decimal(
                    stress.get("underlying_up_30pct_loss_usd", "0"),
                    "underlying_up_30pct_loss_usd",
                ),
                underlying_down_30pct_loss_usd=_decimal(
                    stress.get("underlying_down_30pct_loss_usd", "0"),
                    "underlying_down_30pct_loss_usd",
                ),
                basis_widening_loss_usd=_decimal(
                    stress.get("basis_widening_loss_usd", "0"),
                    "basis_widening_loss_usd",
                ),
                delisting_loss_usd=_decimal(
                    stress.get("delisting_loss_usd", "0"),
                    "delisting_loss_usd",
                ),
                open_interest_cap_loss_usd=_decimal(
                    stress.get("open_interest_cap_loss_usd", "0"),
                    "open_interest_cap_loss_usd",
                ),
                adl_loss_usd=_decimal(stress.get("adl_loss_usd", "0"), "adl_loss_usd"),
                liquidation_loss_usd=_decimal(
                    stress.get("liquidation_loss_usd", "0"),
                    "liquidation_loss_usd",
                ),
                worse_leg_order_loss_usd=_decimal(
                    stress.get("worse_leg_order_loss_usd", "0"),
                    "worse_leg_order_loss_usd",
                ),
                binance_only_net_pnl_usd=_decimal(
                    row.get("binance_only_net_pnl_usd", "0"),
                    "binance_only_net_pnl_usd",
                ),
                quality_flags=tuple(
                    _text(item, "quality flag") for item in _sequence(row.get("quality_flags", []), "quality_flags")
                ),
            )
        )
    windows = tuple(
        EvaluationWindow(
            window_id=_text(row.get("window_id"), "window_id"),
            train_start_ns=_integer(row.get("train_start_ns"), "train_start_ns"),
            train_end_ns=_integer(row.get("train_end_ns"), "train_end_ns"),
            test_start_ns=_integer(row.get("test_start_ns"), "test_start_ns"),
            test_end_ns=_integer(row.get("test_end_ns"), "test_end_ns"),
        )
        for row in (
            _mapping(value, f"windows[{index}]")
            for index, value in enumerate(_sequence(payload.get("windows"), "windows"))
        )
    )
    return tuple(outcomes), windows


def write_evaluation_report(report: WalkForwardEvaluationReport, path: str | Path) -> Path:
    output = Path(path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    encoded = canonical_json_bytes(report.as_wire()) + b"\n"
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=output.parent, prefix=f".{output.name}.", delete=False
        ) as handle:
            temporary_name = handle.name
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, output)
        temporary_name = None
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)
    return output


def verify_evaluation_report(path: str | Path) -> Mapping[str, object]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    root = _mapping(payload, "evaluation report")
    claimed = _text(root.get("report_sha256"), "report_sha256")
    body = {key: value for key, value in root.items() if key != "report_sha256"}
    actual = hashlib.sha256(canonical_json_bytes(body)).hexdigest()
    if claimed != actual:
        raise ValueError("evaluation report hash mismatch")
    return root


__all__ = [
    "EvaluationProtocol",
    "EvaluationWindow",
    "FIXED_SENSITIVITY_GRID",
    "NANOSECONDS_PER_DAY",
    "OpportunityOutcome",
    "PREDECLARED_UNIVERSE",
    "PREREGISTRATION_PATH",
    "PurgedWalkForwardEvaluator",
    "PurgedWindowResult",
    "ScenarioMetrics",
    "SensitivityCase",
    "WalkForwardEvaluationReport",
    "load_evaluation_fixture",
    "evaluate_sensitivity_metrics",
    "verify_evaluation_report",
    "write_evaluation_report",
]
