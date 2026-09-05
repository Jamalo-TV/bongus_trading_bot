"""Offline all-cost carry accounting and paired research comparisons.

No exchange client, runtime configuration, or activation route is imported here.
An input digest identifies a reproducible calculation; it is NOT an attestation
that its inputs are true. Missing costs stay unknown. Funding is counted at
explicit settlement times, never by multiplying a displayed annualized rate.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from bongus.engine.economic_ledger import (
    BORROW_INTEREST,
    COMMISSION,
    DEPOSIT,
    FUNDING,
    INTERNAL_TRANSFER,
    WITHDRAWAL,
    EconomicLedgerProjection,
    project_economic_ledger,
    read_economic_events,
)
from bongus.supervisor.daily_report import FINALIZED, DailyNavClose, calculate_daily_nav_close

ZERO = Decimal("0")
OPEX_CATEGORIES = frozenset({"server", "data", "transfer", "other_operations"})
SCHEMA_VERSION = 1


def decimal(value: Any, name: str, *, nonnegative: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite decimal")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite decimal") from exc
    if not result.is_finite() or (nonnegative and result < ZERO):
        raise ValueError(f"{name} must be finite" + (" and non-negative" if nonnegative else ""))
    return result


def utc(value: datetime | str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return result.astimezone(timezone.utc)


def json_value(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return json_value(asdict(value))
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return utc(value).isoformat()
    if isinstance(value, Mapping):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    return value


def evidence_digest(value: Any) -> str:
    encoded = json.dumps(json_value(value), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class OperatingCost:
    """An identified incremental cost, or an explicit already-booked cost.

    The coverage must include all four OPEX categories, including evidenced
    zero amounts. Trade fees/shortfall/markout are not OPEX categories.
    ``included_in_exchange_pnl`` prevents deducting a booked transfer charge
    twice; the source ID must be an actual ledger event key in that case.
    """

    source_id: str
    category: str
    amount_usd: Decimal | None
    basis: str = "unknown"  # actual, accrued, assumption, unknown
    included_in_exchange_pnl: bool = False


def _operating_costs(
    costs: tuple[OperatingCost, ...],
    *,
    booked_event_keys: frozenset[str] = frozenset(),
) -> tuple[Decimal | None, tuple[str, ...], bool]:
    covered: set[str] = set()
    identifiers: set[str] = set()
    blockers: list[str] = []
    total = ZERO
    measured = True
    for cost in costs:
        if type(cost.included_in_exchange_pnl) is not bool:
            raise ValueError("included_in_exchange_pnl must be a JSON boolean")
        if not cost.source_id.strip() or cost.source_id in identifiers:
            raise ValueError("operating cost source IDs must be non-empty and unique")
        identifiers.add(cost.source_id)
        if cost.category not in OPEX_CATEGORIES:
            raise ValueError(f"unsupported operating cost category: {cost.category}")
        if cost.basis not in {"actual", "accrued", "assumption", "unknown"}:
            raise ValueError("invalid operating cost basis")
        covered.add(cost.category)
        measured = measured and cost.basis == "actual"
        if cost.amount_usd is None or cost.basis == "unknown":
            blockers.append(f"unknown_operating_cost:{cost.category}")
            continue
        amount = decimal(cost.amount_usd, "operating cost", nonnegative=True)
        if cost.included_in_exchange_pnl:
            if cost.source_id not in booked_event_keys:
                raise ValueError("already-booked operating cost requires an economic ledger event key")
        else:
            if cost.source_id in booked_event_keys:
                raise ValueError("operating cost is already booked; refusing a duplicate deduction")
            total += amount
    blockers.extend(f"unknown_operating_cost:{category}" for category in sorted(OPEX_CATEGORIES - covered))
    return (None if blockers else total), tuple(blockers), measured


def actual_cost_report(
    *,
    nav: DailyNavClose,
    projection: EconomicLedgerProjection,
    ledger_reconciled: bool,
    ledger_digest: str,
    average_reserved_capital_usd: Decimal,
    operating_costs: tuple[OperatingCost, ...],
    booked_event_keys: frozenset[str] = frozenset(),
    adverse_markout_diagnostic_usd: Decimal | None = None,
) -> dict[str, Any]:
    """Separate realized cash earnings from MTM and model/accrued cost views.

    Realized price PnL must include BOTH spot cost-basis accounting and futures
    PnL. The NAV close refuses missing components. Spread, slippage, legging,
    and basis effects already contained in executed prices are not charged
    again; markout is a diagnostic, never a second expense.
    """
    reserved = decimal(average_reserved_capital_usd, "average reserved capital", nonnegative=True)
    if reserved == ZERO:
        raise ValueError("average reserved capital must be positive")
    opex, cost_blockers, actual_costs = _operating_costs(operating_costs, booked_event_keys=booked_event_keys)
    blockers = list(cost_blockers)
    if not ledger_reconciled:
        blockers.append("ledger_not_reconciled")
    if nav.status != FINALIZED:
        blockers.append("nav_not_finalized")
    if projection.unvalued_cashflow_event_count or projection.unvalued_economic_event_count:
        blockers.append("unvalued_ledger_events")
    if projection.incomplete_envelope_event_count:
        blockers.append("incomplete_ledger_lineage")
    if not ledger_digest:
        blockers.append("missing_ledger_digest")
    if adverse_markout_diagnostic_usd is not None:
        decimal(adverse_markout_diagnostic_usd, "markout diagnostic")
    cash_values = (
        nav.realized_price_pnl_usd, nav.actual_funding_usd,
        nav.commission_cost_usd, nav.borrow_interest_cost_usd,
    )
    cash = None
    if all(value is not None for value in cash_values):
        realized, funding, commission, borrow = (decimal(value, "cash component") for value in cash_values)
        cash = realized + funding - commission - borrow
    net_view = cash - opex if cash is not None and opex is not None else None
    mtm = None
    if (net_view is not None and nav.unrealized_pnl_change_usd is not None
            and nav.stablecoin_fx_movement_usd is not None):
        mtm = net_view + nav.unrealized_pnl_change_usd + nav.stablecoin_fx_movement_usd
    verified = not blockers and actual_costs
    inputs = {
        "nav": nav, "projection": projection, "ledger_digest": ledger_digest,
        "ledger_reconciled": ledger_reconciled, "average_reserved_capital_usd": reserved,
        "operating_costs": operating_costs, "booked_event_keys": sorted(booked_event_keys),
        "adverse_markout_diagnostic_usd": adverse_markout_diagnostic_usd,
    }
    return json_value({
        "schema_version": SCHEMA_VERSION,
        "status": "MEASURED" if verified else "MEASUREMENT_INCOMPLETE" if blockers else "COST_ASSUMPTION_VIEW",
        "input_digest": evidence_digest(inputs), "ledger_digest": ledger_digest,
        "exchange_realized_cash_pnl_usd": cash,
        "additional_operating_cost_usd": opex,
        "net_cost_view_usd": net_view,
        "verified_realized_net_profit_usd": net_view if verified else None,
        "net_return_on_reserved_capital": net_view / reserved if verified and net_view is not None else None,
        "mtm_cost_view_usd": mtm,
        "external_deposits_usd": nav.external_deposits_usd,
        "external_withdrawals_usd": nav.external_withdrawals_usd,
        "internal_transfers_usd": nav.internal_transfers_usd,
        "average_reserved_capital_usd": reserved,
        "adverse_markout_diagnostic_usd": adverse_markout_diagnostic_usd,
        "blockers": blockers, "operating_costs": operating_costs,
        "live_activation_authorized": False, "profitability_established": False,
        "confidence": "accounting_only; future_return_and_tail_risk_not_estimated",
    })


def ledger_cost_report(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    trading_mode: str,
    start_time: str,
    end_time: str,
    nav_inputs: Mapping[str, Any],
    ledger_reconciled: bool,
    average_reserved_capital_usd: Decimal,
    operating_costs: tuple[OperatingCost, ...],
) -> dict[str, Any]:
    """Read existing ledger in a caller-owned snapshot; never migrate/write it.

    Report periods are [start, end), so adjacent periods cannot count the same
    boundary event twice. Missing event categories do NOT imply zero. Explicit zero confirmations
    may be supplied in nav_inputs. Supplied realized PnL replaces, rather than
    adds to, the ledger subtotal to support consolidated spot cost-basis PnL.
    """
    if not account_id.strip() or not trading_mode.strip():
        raise ValueError("explicit account and trading mode are required")
    start, end = utc(start_time), utc(end_time)
    if end <= start:
        raise ValueError("report end must follow start")
    conn.row_factory = sqlite3.Row
    scope = dict(account_id=account_id, trading_mode=trading_mode,
                 start_time=start.isoformat(), end_time=(end - timedelta(microseconds=1)).isoformat())
    rows = read_economic_events(conn, **scope, limit=None)
    projection = project_economic_ledger(conn, **scope)
    inputs = dict(nav_inputs)
    for field_name, event_type, sign in (
        ("external_deposits_usd", DEPOSIT, 1), ("external_withdrawals_usd", WITHDRAWAL, -1),
        ("actual_funding_usd", FUNDING, 1),
        ("commission_cost_usd", COMMISSION, -1), ("borrow_interest_cost_usd", BORROW_INTEREST, -1),
        ("internal_transfers_usd", INTERNAL_TRANSFER, 1),
    ):
        amount = projection.cashflow_usd_by_type.get(event_type)
        if field_name not in inputs:
            inputs[field_name] = amount * sign if amount is not None else None
        elif amount is not None and inputs[field_name] is not None:
            if decimal(inputs[field_name], field_name) != amount * sign:
                raise ValueError(f"{field_name} conflicts with the scoped economic ledger")
    # REALIZED_PNL exchange cash events commonly cover only the perpetual leg.
    # A spot sale's cash proceeds are not its cost-basis PnL. Never relabel that
    # futures subtotal as consolidated hedge earnings.
    for field_name in ("opening_nav_usd", "closing_nav_usd", "realized_price_pnl_usd",
                       "unrealized_pnl_change_usd", "stablecoin_fx_movement_usd"):
        inputs.setdefault(field_name, None)
    report = actual_cost_report(
        nav=calculate_daily_nav_close(**inputs), projection=projection,
        ledger_reconciled=ledger_reconciled, ledger_digest=evidence_digest(rows),
        average_reserved_capital_usd=average_reserved_capital_usd, operating_costs=operating_costs,
        booked_event_keys=frozenset(str(row["event_key"]) for row in rows
                                   if row["event_type"] in {COMMISSION, BORROW_INTEREST}),
    )
    report["scope"] = {
        "account_id": account_id, "trading_mode": trading_mode,
        "start_inclusive": start.isoformat(), "end_exclusive": end.isoformat(),
    }
    report["input_attestation"] = "caller_supplied; digest_and_NAV_identity_do_not_prove_exchange_completeness"
    return report


@dataclass(frozen=True, slots=True)
class FundingSettlement:
    source_id: str
    settlement_time: datetime
    available_at: datetime
    rate: Decimal
    mark_price_usd: Decimal


@dataclass(frozen=True, slots=True)
class CarryWindow:
    """A fixed-quantity hedge on (start, end], using supplied USD prices.

    Positive spot_quantity means long spot/short perp, negative means inverse.
    Historical prices use executions OR reference mid prices, explicitly.
    ``execution_shortfall_usd`` is the total non-overlapping shortfall against
    those mids. With fill prices it MUST be zero, since PnL already includes it.
    These hypothetical policy results never become actual ledger profit.
    """

    label: str
    start: datetime
    end: datetime
    policy_frozen_at: datetime
    data_cutoff: datetime
    reserved_capital_usd: Decimal
    spot_quantity: Decimal
    spot_entry_usd: Decimal
    spot_exit_usd: Decimal
    perp_entry_usd: Decimal
    perp_exit_usd: Decimal
    prices_are_fills: bool
    commissions_usd: Decimal | None
    borrow_cost_usd: Decimal | None
    execution_shortfall_usd: Decimal | None
    operating_costs: tuple[OperatingCost, ...]
    settlements: tuple[FundingSettlement, ...]
    funding_history_complete: bool = False
    evidence_kind: str = "assumption"  # assumption, historical, shadow, live


def evaluate_carry_window(window: CarryWindow) -> dict[str, Any]:
    if type(window.prices_are_fills) is not bool or type(window.funding_history_complete) is not bool:
        raise ValueError("prices_are_fills and funding_history_complete must be JSON booleans")
    start, end, cutoff = utc(window.start), utc(window.end), utc(window.data_cutoff)
    if end <= start or cutoff < end:
        raise ValueError("carry window requires start < end <= data cutoff")
    if window.evidence_kind not in {"assumption", "historical", "shadow", "live"}:
        raise ValueError("unknown carry evidence kind")
    capital = decimal(window.reserved_capital_usd, "reserved capital", nonnegative=True)
    quantity = decimal(window.spot_quantity, "spot quantity")
    if capital == ZERO:
        raise ValueError("reserved capital must be positive")
    prices = tuple(decimal(value, "price", nonnegative=True) for value in (
        window.spot_entry_usd, window.spot_exit_usd, window.perp_entry_usd, window.perp_exit_usd,
    ))
    if any(price == ZERO for price in prices):
        raise ValueError("prices must be positive")
    spot_in, spot_out, perp_in, perp_out = prices
    price_pnl = quantity * ((spot_out - spot_in) - (perp_out - perp_in))
    ids: set[str] = set()
    funding = ZERO
    funding_exposure = ZERO
    for event in window.settlements:
        if not event.source_id.strip() or event.source_id in ids:
            raise ValueError("funding source IDs must be non-empty and unique within each window")
        ids.add(event.source_id)
        settled, available = utc(event.settlement_time), utc(event.available_at)
        if not start < settled <= end or not settled <= available <= cutoff:
            raise ValueError("settlement eligibility or point-in-time availability violated")
        rate = decimal(event.rate, "funding rate")
        mark = decimal(event.mark_price_usd, "funding mark price", nonnegative=True)
        if mark == ZERO:
            raise ValueError("funding mark price must be positive")
        funding += quantity * mark * rate
        funding_exposure += abs(quantity) * mark
    opex, cost_blockers, _ = _operating_costs(window.operating_costs)
    blockers = list(cost_blockers)
    costs: dict[str, Decimal | None] = {"opex": opex}
    for name in ("commissions_usd", "borrow_cost_usd", "execution_shortfall_usd"):
        raw = getattr(window, name)
        costs[name] = None if raw is None else decimal(raw, name, nonnegative=True)
        if raw is None:
            blockers.append(f"unknown_cost:{name}")
    if window.prices_are_fills and costs["execution_shortfall_usd"] != ZERO:
        raise ValueError("fill-price PnL already contains execution shortfall; supply zero, not a second deduction")
    if not window.funding_history_complete:
        blockers.append("funding_history_not_confirmed_complete")
    causal = utc(window.policy_frozen_at) <= start
    if not causal:
        blockers.append("policy_not_frozen_before_window")
    total_cost = sum((cost for cost in costs.values() if cost is not None), ZERO) if not blockers else None
    net = price_pnl + funding - total_cost if total_cost is not None else None
    breakeven = (total_cost - price_pnl) / funding_exposure if total_cost is not None and funding_exposure else None
    return json_value({
        "schema_version": SCHEMA_VERSION, "label": window.label,
        "status": "MODEL_ONLY" if window.evidence_kind == "assumption" else "HISTORICAL_ACCOUNTING",
        "input_digest": evidence_digest(window), "start": start, "end": end,
        "horizon_days": Decimal(str((end - start).total_seconds())) / Decimal("86400"),
        "evidence_kind": window.evidence_kind, "reserved_capital_usd": capital,
        "price_pnl_usd": price_pnl, "signed_funding_usd": funding,
        "settlement_count": len(ids), "funding_source_ids": sorted(ids),
        "costs_usd": costs, "net_cost_view_usd": net,
        "net_return_on_reserved_capital": net / capital if net is not None else None,
        "break_even_signed_received_rate_per_settlement": breakeven,
        "policy_frozen_before_window": causal, "blockers": blockers,
        "loss_month_probability": None, "expected_max_drawdown": None, "ruin_probability": None,
        "live_activation_authorized": False, "profitability_established": False,
    })


@dataclass(frozen=True, slots=True)
class CarryPortfolioWindow:
    """A fixed research budget with explicit hedge cycles, including rotations.

    Cycle reserve requirements may not exceed the common budget at any time.
    All period OPEX belongs here; cycle OPEX must explicitly be zero. The
    history-complete flag attests to the full cycle list, including losing
    cycles and unsuccessful executions represented by their actual costs.
    """

    label: str
    start: datetime
    end: datetime
    policy_frozen_at: datetime
    data_cutoff: datetime
    reserved_capital_usd: Decimal
    cycles: tuple[CarryWindow, ...]
    operating_costs: tuple[OperatingCost, ...]
    cycle_history_complete: bool = False
    evidence_kind: str = "assumption"


def evaluate_carry_portfolio(window: CarryPortfolioWindow) -> dict[str, Any]:
    start, end, cutoff = utc(window.start), utc(window.end), utc(window.data_cutoff)
    capital = decimal(window.reserved_capital_usd, "portfolio reserved capital", nonnegative=True)
    if end <= start or cutoff < end or capital == ZERO:
        raise ValueError("portfolio requires positive capital and start < end <= data cutoff")
    if window.evidence_kind not in {"assumption", "historical", "shadow", "live"}:
        raise ValueError("unknown portfolio evidence kind")
    if type(window.cycle_history_complete) is not bool:
        raise ValueError("cycle_history_complete must be a JSON boolean")
    opex, cost_blockers, _ = _operating_costs(window.operating_costs)
    blockers = list(cost_blockers)
    if not window.cycle_history_complete:
        blockers.append("cycle_history_not_confirmed_complete")
    if utc(window.policy_frozen_at) > start:
        blockers.append("policy_not_frozen_before_window")
    views = []
    labels: set[str] = set()
    funding_ids: set[str] = set()
    reserve_changes: list[tuple[datetime, int, Decimal]] = []
    evidence_kind = window.evidence_kind
    for cycle in window.cycles:
        if not cycle.label.strip() or cycle.label in labels:
            raise ValueError("cycle labels must be non-empty and unique")
        labels.add(cycle.label)
        cycle_start, cycle_end = utc(cycle.start), utc(cycle.end)
        if not start <= cycle_start < cycle_end <= end or utc(cycle.data_cutoff) > cutoff:
            raise ValueError("cycles must stay inside the portfolio period and data cutoff")
        if utc(cycle.policy_frozen_at) > start:
            blockers.append("cycle_policy_not_frozen_before_portfolio_window")
        cycle_opex, cycle_cost_blockers, _ = _operating_costs(cycle.operating_costs)
        if cycle_cost_blockers or cycle_opex != ZERO:
            raise ValueError("portfolio OPEX must be charged once at period level; cycle OPEX must be explicit zero")
        view = evaluate_carry_window(cycle)
        if funding_ids.intersection(view["funding_source_ids"]):
            raise ValueError("funding source IDs must not be reused across portfolio cycles")
        funding_ids.update(view["funding_source_ids"])
        blockers.extend(view["blockers"])
        if cycle.evidence_kind == "assumption":
            evidence_kind = "assumption"
        views.append(view)
        reserve = decimal(cycle.reserved_capital_usd, "cycle reserved capital")
        # Release ending cycles before allocating another cycle at the same timestamp.
        reserve_changes.extend(((cycle_start, 1, reserve), (cycle_end, 0, -reserve)))
    active_reserve = ZERO
    for _, _, change in sorted(reserve_changes):
        active_reserve += change
        if active_reserve > capital:
            raise ValueError("concurrent cycle reserves exceed the portfolio capital budget")
    net = None
    if not blockers and opex is not None:
        net = sum((decimal(view["net_cost_view_usd"], "cycle net") for view in views), ZERO) - opex
    return json_value({
        "schema_version": SCHEMA_VERSION, "label": window.label,
        "status": "MODEL_ONLY" if evidence_kind == "assumption" else "HISTORICAL_ACCOUNTING",
        "input_digest": evidence_digest(window), "start": start, "end": end,
        "horizon_days": Decimal(str((end - start).total_seconds())) / Decimal("86400"),
        "evidence_kind": evidence_kind, "reserved_capital_usd": capital,
        "net_cost_view_usd": net, "net_return_on_reserved_capital": net / capital if net is not None else None,
        "price_pnl_usd": sum((decimal(view["price_pnl_usd"], "price PnL") for view in views), ZERO),
        "signed_funding_usd": sum((decimal(view["signed_funding_usd"], "funding") for view in views), ZERO),
        "settlement_count": len(funding_ids), "funding_source_ids": sorted(funding_ids),
        "period_operating_cost_usd": opex, "cycles": views, "blockers": sorted(set(blockers)),
        "loss_month_probability": None, "expected_max_drawdown": None, "ruin_probability": None,
        "live_activation_authorized": False, "profitability_established": False,
    })


def compare_carry_to_baseline(
    candidate: CarryWindow | CarryPortfolioWindow, baseline: CarryWindow | CarryPortfolioWindow,
) -> dict[str, Any]:
    if (utc(candidate.start), utc(candidate.end), decimal(candidate.reserved_capital_usd, "capital")) != (
        utc(baseline.start), utc(baseline.end), decimal(baseline.reserved_capital_usd, "capital")
    ):
        raise ValueError("paired comparisons require identical windows and reserved capital")
    days = (utc(candidate.end) - utc(candidate.start)).total_seconds() / 86400
    if days not in (7, 30):
        raise ValueError("predeclared carry comparisons support 7- or 30-day windows")
    result = (evaluate_carry_portfolio(candidate) if isinstance(candidate, CarryPortfolioWindow)
              else evaluate_carry_window(candidate))
    reference = (evaluate_carry_portfolio(baseline) if isinstance(baseline, CarryPortfolioWindow)
                 else evaluate_carry_window(baseline))
    net, base_net = result["net_cost_view_usd"], reference["net_cost_view_usd"]
    return json_value({
        "candidate": result, "baseline": reference,
        "incremental_net_usd": decimal(net, "candidate net") - decimal(base_net, "baseline net")
        if net is not None and base_net is not None else None,
        "pair_digest": evidence_digest({"candidate": candidate, "baseline": baseline}),
        "live_activation_authorized": False,
    })


def research_evidence_gate(comparisons: tuple[dict[str, Any], ...], *, expected_digest: str) -> dict[str, Any]:
    """Integrity/coverage gate for review, never a strategy activation gate.

    Require both planned horizons and complete causal cost views. No sample
    size alone proves profit; uncertainty/tails and live promotion remain out
    of scope. Pin the expected digest in an independently reviewed, immutable
    evidence manifest; a self-computed hash alone is not evidence approval.
    """
    digest = evidence_digest(comparisons)
    blockers: list[str] = []
    if not expected_digest or expected_digest != digest:
        blockers.append("comparison_digest_mismatch")
    horizons: set[Decimal] = set()
    pair_ids: set[str] = set()
    for pair in comparisons:
        pair_id = str(pair["pair_digest"])
        if pair_id in pair_ids:
            blockers.append("duplicate_comparison")
        pair_ids.add(pair_id)
        for name in ("candidate", "baseline"):
            result = pair[name]
            horizons.add(decimal(result["horizon_days"], "horizon"))
            if result["blockers"] or result["net_cost_view_usd"] is None:
                blockers.append("incomplete_cost_or_causality_evidence")
            if result["evidence_kind"] == "assumption":
                blockers.append("assumption_only_evidence")
    if not {Decimal(7), Decimal(30)}.issubset(horizons):
        blockers.append("missing_7_or_30_day_baseline_comparison")
    return {
        "status": "READY_FOR_RESEARCH_REVIEW" if not blockers else "INSUFFICIENT_EVIDENCE",
        "comparison_digest": digest, "blockers": sorted(set(blockers)),
        "live_activation_authorized": False, "capital_increase_authorized": False,
        "profitability_established": False,
    }


def capital_scenarios(
    *,
    capitals_usd: tuple[Decimal, ...],
    reserved_fraction: Decimal,
    annual_net_edge_on_reserved_before_opex: Decimal,
    monthly_opex_usd: Decimal,
    assumption_label: str,
) -> dict[str, Any]:
    """Explicit sensitivities, not forecasts; fees/shortfall are inside edge.

    ``reserved_fraction`` is average reserved equity / account equity. It is
    NOT gross notional leverage. Exchange-filter feasibility must be checked
    separately. Annual/monthly flat-capital PnL includes all twelve months of
    stated OPEX even when it exceeds starting capital (external cost budget).
    Reinvestment is shown separately and stops when the account cannot fund
    another month; it never earns returns on negative capital.
    """
    utilization = decimal(reserved_fraction, "reserved fraction", nonnegative=True)
    edge = decimal(annual_net_edge_on_reserved_before_opex, "annual net edge")
    monthly_cost = decimal(monthly_opex_usd, "monthly operating cost", nonnegative=True)
    if not ZERO < utilization <= 1 or not assumption_label.strip():
        raise ValueError("reserved fraction must be in (0,1] and assumption label is required")
    rows = []
    for raw_capital in capitals_usd:
        capital = decimal(raw_capital, "capital", nonnegative=True)
        if capital == ZERO:
            raise ValueError("capital must be positive")
        reserved = capital * utilization
        annual_profit = reserved * edge - 12 * monthly_cost
        cash = capital
        exhausted = None
        for month in range(1, 13):
            cash = cash * (1 + utilization * edge / 12) - monthly_cost
            if cash <= ZERO:
                exhausted = month
                break
        rows.append({
            "capital_usd": capital, "average_reserved_capital_usd": reserved,
            "month_net_profit_usd": annual_profit / 12, "year_net_profit_no_reinvestment_usd": annual_profit,
            "month_net_return": annual_profit / 12 / capital,
            "year_net_return_no_reinvestment": annual_profit / capital,
            "year_profit_with_monthly_reinvestment_usd": cash - capital if exhausted is None else None,
            "cash_budget_exhausted_month_in_reinvestment_scenario": exhausted,
            "break_even_annual_net_edge_on_reserved_before_opex": 12 * monthly_cost / reserved,
            "exchange_filter_feasibility": "NOT_EVALUATED",
        })
    return json_value({
        "schema_version": SCHEMA_VERSION, "status": "ASSUMPTION_ONLY", "assumption_label": assumption_label,
        "annual_net_edge_on_reserved_before_opex": edge, "reserved_fraction": utilization,
        "monthly_opex_usd": monthly_cost,
        "break_even_capital_usd": 12 * monthly_cost / (utilization * edge) if edge > ZERO else None,
        "rows": rows, "expected_max_drawdown": None, "loss_month_probability": None, "ruin_probability": None,
        "live_activation_authorized": False, "profitability_established": False,
    })
