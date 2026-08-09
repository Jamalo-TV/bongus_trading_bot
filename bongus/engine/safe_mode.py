"""Structured safe-mode descriptors shared by runtime and operators."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable, Mapping


@dataclass(frozen=True, slots=True)
class SafeModeCode:
    code: str
    scope: str
    recoverable: bool
    next_action: str
    description: str

    def to_dict(self) -> dict[str, str | bool]:
        return asdict(self)


_CATALOG: dict[str, SafeModeCode] = {
    "account_reconciliation": SafeModeCode(
        code="account_reconciliation",
        scope="global",
        recoverable=False,
        next_action="classify_or_repair_account_state",
        description="An account order, position, hedge, liability, or required endpoint is unexplained.",
    ),
    "audit_unavailable": SafeModeCode(
        code="audit_unavailable",
        scope="global",
        recoverable=True,
        next_action="retry_exchange_audit",
        description="Exchange reconciliation failed too many times to trust new risk.",
    ),
    "capital_reservation": SafeModeCode(
        code="capital_reservation",
        scope="global",
        recoverable=True,
        next_action="reconcile_capital_reservations",
        description="Capital reservation state could not be proven safe for new exposure.",
    ),
    "divergence_exit_blocked": SafeModeCode(
        code="divergence_exit_blocked",
        scope="global",
        recoverable=False,
        next_action="operator_review",
        description="A symbol with position divergence refused automated exit dispatch.",
    ),
    "execution_bridge": SafeModeCode(
        code="execution_bridge",
        scope="global",
        recoverable=True,
        next_action="restore_rust_ipc",
        description="Python cannot safely send intents to the Rust execution bridge.",
    ),
    "economic_ledger_lineage": SafeModeCode(
        code="economic_ledger_lineage",
        scope="global",
        recoverable=False,
        next_action="reconcile_execution_lineage",
        description="An execution effect cannot be linked to its durable economic lineage.",
    ),
    "economic_ledger_reconciliation": SafeModeCode(
        code="economic_ledger_reconciliation",
        scope="global",
        recoverable=False,
        next_action="reconcile_economic_ledger",
        description="Economic ledger totals do not reconcile to authoritative exchange statements.",
    ),
    "exchange_statement_ingestion": SafeModeCode(
        code="exchange_statement_ingestion",
        scope="global",
        recoverable=True,
        next_action="replay_exchange_statements",
        description="Authoritative exchange statement ingestion is incomplete or contains unmapped rows.",
    ),
    "execution_reconciliation": SafeModeCode(
        code="execution_reconciliation",
        scope="global",
        recoverable=True,
        next_action="reconcile_execution_state",
        description="Execution state has an ambiguous or unverified exchange effect.",
    ),
    "heartbeat_bridge": SafeModeCode(
        code="heartbeat_bridge",
        scope="global",
        recoverable=True,
        next_action="restore_heartbeat_ack",
        description="Rust heartbeat acknowledgements are missing or stale.",
    ),
    "funding_stale": SafeModeCode(
        code="funding_stale",
        scope="global",
        recoverable=True,
        next_action="refresh_funding_data",
        description="Funding observations required for entry decisions are stale.",
    ),
    "health_monitor": SafeModeCode(
        code="health_monitor",
        scope="global",
        recoverable=True,
        next_action="restore_health_monitor",
        description="The runtime health monitor cannot prove that entry dependencies are healthy.",
    ),
    "private_stream_recovery": SafeModeCode(
        code="private_stream_recovery",
        scope="global",
        recoverable=True,
        next_action="replay_private_order_and_trade_history",
        description="Spot or futures private-stream history has not been backfilled through a proven cursor.",
    ),
    "late_entry_fill": SafeModeCode(
        code="late_entry_fill",
        scope="global",
        recoverable=False,
        next_action="operator_review",
        description="An entry filled after Python considered the intent stale.",
    ),
    "risk_limits": SafeModeCode(
        code="risk_limits",
        scope="global",
        recoverable=True,
        next_action="wait_or_derisk",
        description="Portfolio risk limits require blocking new risk or derisking.",
    ),
    "rust_execution_readiness": SafeModeCode(
        code="rust_execution_readiness",
        scope="global",
        recoverable=True,
        next_action="reconcile_spot_and_futures_execution_state",
        description="Rust has not completed authoritative two-venue execution reconciliation.",
    ),
    "rust_subscriber": SafeModeCode(
        code="rust_subscriber",
        scope="global",
        recoverable=True,
        next_action="restore_rust_telemetry",
        description="The Rust telemetry subscriber is unavailable or has not recovered continuity.",
    ),
    "spot_universe_unavailable": SafeModeCode(
        code="spot_universe_unavailable",
        scope="global",
        recoverable=True,
        next_action="retry_symbol_universe",
        description="The spot hedge universe is unavailable, so new entries cannot be validated.",
    ),
    "startup_mismatch": SafeModeCode(
        code="startup_mismatch",
        scope="global",
        recoverable=False,
        next_action="operator_review",
        description="Startup exchange state does not match local managed state.",
    ),
    "startup_reconciliation_failed": SafeModeCode(
        code="startup_reconciliation_failed",
        scope="global",
        recoverable=True,
        next_action="retry_startup_reconciliation",
        description="Startup reconciliation failed before a safe runtime state was reached.",
    ),
    "state_store_write": SafeModeCode(
        code="state_store_write",
        scope="global",
        recoverable=True,
        next_action="restore_state_store",
        description="SQLite state writes failed, so the bot cannot maintain an audit trail.",
    ),
    "exit_failure": SafeModeCode(
        code="exit_failure",
        scope="symbol",
        recoverable=True,
        next_action="retry_or_operator_review",
        description="A symbol exit failed and needs retry tracking or review.",
    ),
    "hedge_gap": SafeModeCode(
        code="hedge_gap",
        scope="symbol",
        recoverable=True,
        next_action="reconcile_or_exit_symbol",
        description="Spot and perp legs are not sufficiently hedged for one or more symbols.",
    ),
    "naked_leg_unwind_stuck": SafeModeCode(
        code="naked_leg_unwind_stuck",
        scope="symbol",
        recoverable=False,
        next_action="operator_review",
        description="A startup single-leg unwind could not complete automatically.",
    ),
    "partial_rotation_reconciliation": SafeModeCode(
        code="partial_rotation_reconciliation",
        scope="symbol",
        recoverable=True,
        next_action="reconcile_intent_terminal_state",
        description=(
            "A partial rotation has an unresolved terminal quantity; new risk stays "
            "blocked until the durable Rust result and exchange residual agree."
        ),
    ),
    "stale_pending_intent": SafeModeCode(
        code="stale_pending_intent",
        scope="symbol",
        recoverable=True,
        next_action="reconcile_intent_terminal_state",
        description="A pending enter or exit intent exceeded its allowed age.",
    ),
    "startup_exit_candidate": SafeModeCode(
        code="startup_exit_candidate",
        scope="symbol",
        recoverable=True,
        next_action="allow_recovery_exit",
        description="Startup found a position eligible for controlled recovery exit.",
    ),
    "startup_manual_review": SafeModeCode(
        code="startup_manual_review",
        scope="symbol",
        recoverable=False,
        next_action="operator_acknowledge_or_flatten",
        description="Startup found an orphan or mismatch that requires explicit operator review.",
    ),
}


def describe_safe_mode_flags(flags: Iterable[str]) -> list[dict[str, str | bool]]:
    """Return stable, machine-readable descriptors for active safe-mode flags."""

    descriptors: list[dict[str, str | bool]] = []
    for raw_flag in sorted({str(flag).strip() for flag in flags if str(flag).strip()}):
        descriptor = _CATALOG.get(raw_flag)
        if descriptor is None:
            descriptor = SafeModeCode(
                code=raw_flag,
                scope="global",
                recoverable=False,
                next_action="operator_review",
                description="Uncatalogued safe-mode flag.",
            )
        descriptors.append(descriptor.to_dict())
    return descriptors


def safe_mode_catalog() -> list[dict[str, str | bool]]:
    return [descriptor.to_dict() for descriptor in sorted(_CATALOG.values(), key=lambda item: item.code)]


def restore_safe_mode_flags(snapshot: Mapping[str, object]) -> set[str]:
    """Restore the last durable entry block without trusting display text alone.

    Structured codes are authoritative when present.  The comma-separated
    reason remains a backward-compatible fallback for databases created before
    ``safe_mode_codes`` was persisted.  Unknown codes are deliberately kept so
    they retain the catalog's fail-to-operator-review behavior after restart.
    """

    restored: set[str] = set()
    raw_codes = snapshot.get("safe_mode_codes")
    if isinstance(raw_codes, list):
        for raw in raw_codes:
            code = raw.get("code") if isinstance(raw, Mapping) else raw
            normalized = str(code or "").strip()
            if normalized:
                restored.add(normalized)
    if restored:
        return restored

    raw_reason = str(snapshot.get("safe_mode_reason") or "")
    return {
        item.strip()
        for item in raw_reason.split(",")
        if item.strip()
    }
