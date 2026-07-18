"""Reservation-aware, proposal-only collateral rebalancing."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Literal

from bongus.portfolio.capital_reservations import (
    CapitalReservationBook,
    CapitalState,
    ReservationPolicy,
)
from bongus.engine.account_reconciliation import AccountReconciliationReport


TransferDirection = Literal["spot_to_futures", "futures_to_spot", "none"]


def _decimal(value: Decimal | str | float, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite decimal") from exc
    if not result.is_finite() or result < 0:
        raise ValueError(f"{name} must be a finite non-negative decimal")
    return result


@dataclass(frozen=True, slots=True)
class TreasuryPolicy:
    target_spot_buffer_usd: Decimal | str | float
    target_futures_buffer_usd: Decimal | str | float
    minimum_transfer_usd: Decimal | str | float = Decimal("25")
    maximum_transfer_usd: Decimal | str | float = Decimal("500")
    proposal_only: bool = True


@dataclass(frozen=True, slots=True)
class TreasuryProposal:
    proposal_id: str
    direction: TransferDirection
    amount_usd: Decimal
    created_at: str
    executable: bool
    reason_codes: tuple[str, ...]
    evidence: dict[str, str]


class ReservationAwareTreasury:
    """Generate auditable proposals; this class never calls an exchange."""

    def __init__(self, reservation_book: CapitalReservationBook) -> None:
        self.reservation_book = reservation_book

    def propose(
        self,
        *,
        capital: CapitalState,
        reservation_policy: ReservationPolicy,
        treasury_policy: TreasuryPolicy,
        reconciliation_matched: bool,
        critical_incident_active: bool,
        now: datetime | None = None,
    ) -> TreasuryProposal:
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        projection = self.reservation_book.projection(capital, reservation_policy)
        target_spot = _decimal(treasury_policy.target_spot_buffer_usd, "target_spot_buffer_usd")
        target_futures = _decimal(
            treasury_policy.target_futures_buffer_usd, "target_futures_buffer_usd"
        )
        minimum = _decimal(treasury_policy.minimum_transfer_usd, "minimum_transfer_usd")
        maximum = _decimal(treasury_policy.maximum_transfer_usd, "maximum_transfer_usd")
        if maximum < minimum:
            raise ValueError("maximum transfer must not be below minimum transfer")

        reasons: list[str] = []
        direction: TransferDirection = "none"
        amount = Decimal("0")
        if not reconciliation_matched:
            reasons.append("account_reconciliation_not_proven")
        if critical_incident_active:
            reasons.append("critical_incident_active")

        spot_free = projection.entry_spot_cash_remaining_usd
        futures_free = projection.entry_futures_margin_remaining_usd
        if not reasons:
            spot_shortfall = max(Decimal("0"), target_spot - spot_free)
            futures_shortfall = max(Decimal("0"), target_futures - futures_free)
            if futures_shortfall > spot_shortfall and spot_free > target_spot:
                direction = "spot_to_futures"
                amount = min(maximum, futures_shortfall, spot_free - target_spot)
            elif spot_shortfall > futures_shortfall and futures_free > target_futures:
                direction = "futures_to_spot"
                amount = min(maximum, spot_shortfall, futures_free - target_futures)
            else:
                reasons.append("buffers_balanced_or_no_unreserved_surplus")
            if amount < minimum:
                amount = Decimal("0")
                direction = "none"
                reasons.append("transfer_below_minimum")

        evidence = {
            "spot_free_after_all_reservations_usd": str(spot_free),
            "futures_free_after_all_reservations_usd": str(futures_free),
            "reserved_spot_cash_usd": str(projection.reserved_spot_cash_usd),
            "reserved_futures_margin_usd": str(projection.reserved_futures_margin_usd),
            "repair_reserve_usd": str(reservation_policy.repair_reserve_usd),
            "exit_reserve_usd": str(reservation_policy.exit_reserve_usd),
            "liquidation_buffer_usd": str(reservation_policy.minimum_liquidation_buffer_usd),
        }
        payload = {
            "direction": direction,
            "amount_usd": str(amount),
            "created_at": now.isoformat(),
            "evidence": evidence,
        }
        proposal_id = "treasury-" + hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:20]
        if treasury_policy.proposal_only:
            reasons.append("proposal_only_policy")
        return TreasuryProposal(
            proposal_id=proposal_id,
            direction=direction,
            amount_usd=amount,
            created_at=now.isoformat(),
            # A valid proposal is still not executable by this component.  A
            # future separately reviewed adapter must consume it.
            executable=False,
            reason_codes=tuple(dict.fromkeys(reasons)),
            evidence=evidence,
        )

    def propose_from_reconciliation(
        self,
        *,
        capital: CapitalState,
        reservation_policy: ReservationPolicy,
        treasury_policy: TreasuryPolicy,
        reconciliation: AccountReconciliationReport,
        critical_incident_active: bool,
        now: datetime | None = None,
    ) -> TreasuryProposal:
        """Bind a proposal to the complete ownership/reconciliation proof.

        Callers must not reduce a rich report to an optimistic boolean. Any
        external order, unknown inventory, endpoint failure, or position
        mismatch makes ``reconciliation.ready`` false and forces a zero-value
        proposal while preserving the report fingerprint as audit evidence.
        """

        proposal = self.propose(
            capital=capital,
            reservation_policy=reservation_policy,
            treasury_policy=treasury_policy,
            reconciliation_matched=(
                reconciliation.ready and reconciliation.snapshot_complete
            ),
            critical_incident_active=critical_incident_active,
            now=now,
        )
        evidence = dict(proposal.evidence)
        evidence.update(
            {
                "reconciliation_fingerprint": reconciliation.fingerprint,
                "reconciliation_ready": str(reconciliation.ready).lower(),
                "reconciliation_snapshot_complete": str(
                    reconciliation.snapshot_complete
                ).lower(),
                "bot_owned_open_order_count": str(
                    len(reconciliation.bot_owned_orders)
                ),
                "unrelated_open_order_count": str(
                    len(reconciliation.unrelated_orders)
                ),
                "blocking_issue_codes": ",".join(
                    sorted({issue.code for issue in reconciliation.blocking_issues})
                ),
            }
        )
        return replace(proposal, evidence=evidence)

    @staticmethod
    def execute(_proposal: TreasuryProposal) -> None:
        raise RuntimeError(
            "treasury execution is intentionally unavailable; proposals require a separately reviewed adapter"
        )
