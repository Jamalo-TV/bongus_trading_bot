"""Immutable, replayable economic ledger primitives.

The legacy ``execution_events`` table is an operational event log.  It is
useful for diagnostics, but its rows do not have stable exchange identities
and cannot safely be replayed into economic state.  This module provides the
normalized journal used for that purpose without changing the legacy API.

Numeric values are stored as canonical decimal text.  That avoids introducing
binary floating-point drift into reconciliation and preserves the exact value
reported by an exchange.  ``amount`` is always a signed balance change in
``amount_asset``.  ``amount_usd`` is a signed economic effect (income positive,
cost negative), never trade notional.  Fills carry signed quantity separately;
spot quantities affect inventory while perpetual quantities affect positions.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping

ECONOMIC_LEDGER_SCHEMA_VERSION = 1

FILL = "FILL"
COMMISSION = "COMMISSION"
REALIZED_PNL = "REALIZED_PNL"
FUNDING = "FUNDING"
BORROW_INTEREST = "BORROW_INTEREST"
BALANCE_ADJUSTMENT = "BALANCE_ADJUSTMENT"

ECONOMIC_EVENT_TYPES = frozenset(
    {FILL, COMMISSION, REALIZED_PNL, FUNDING, BORROW_INTEREST, BALANCE_ADJUSTMENT}
)
CASHFLOW_EVENT_TYPES = frozenset(
    {COMMISSION, REALIZED_PNL, FUNDING, BORROW_INTEREST, BALANCE_ADJUSTMENT}
)
# Deposits, withdrawals and transfers change reconciled balances, but are not
# strategy income or expense.  Keep them out of the economic PnL projection.
ECONOMIC_EFFECT_TYPES = frozenset(
    {COMMISSION, REALIZED_PNL, FUNDING, BORROW_INTEREST}
)
STABLE_QUOTE_ASSETS = frozenset({"USDT", "USDC", "FDUSD", "BUSD", "USD"})

DecimalInput = Decimal | int | float | str


class LedgerValidationError(ValueError):
    """A normalized event is incomplete or economically ambiguous."""


class LedgerIdempotencyConflict(RuntimeError):
    """A stable source identity was replayed with different economic content."""


@dataclass(frozen=True, slots=True)
class EconomicLedgerEvent:
    """One normalized exchange economic effect.

    At least one of ``exchange_fill_id``, ``exchange_event_id`` or
    ``source_event_id`` must be populated.  Fills and their commissions should
    use the exchange fill/trade ID.  Funding, interest and balance events should
    use the exchange transaction ID.  ``source_event_id`` is the explicit
    fallback for paper/replay sources that do not issue exchange IDs.
    """

    event_type: str
    event_time: str
    account_id: str
    trading_mode: str
    venue: str
    strategy_id: str
    source_event_id: str = ""
    symbol: str = ""
    instrument_type: str = ""
    side: str = ""
    quantity: DecimalInput | None = None
    quantity_asset: str = ""
    price: DecimalInput | None = None
    amount: DecimalInput | None = None
    amount_asset: str = ""
    amount_usd: DecimalInput | None = None
    cycle_id: str = ""
    intent_id: str = ""
    order_id: str = ""
    client_order_id: str = ""
    exchange_event_id: str = ""
    exchange_fill_id: str = ""
    runtime_mode: str = ""
    session_id: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    raw_payload: Mapping[str, Any] | None = None
    schema_version: int = ECONOMIC_LEDGER_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class LedgerIngestionResult:
    requested: int
    inserted: int
    duplicates: int
    event_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EconomicLedgerProjection:
    event_count: int
    fill_count: int
    balance_deltas: dict[str, Decimal]
    spot_inventory_deltas: dict[str, Decimal]
    perpetual_position_deltas: dict[str, Decimal]
    amounts_by_type_and_asset: dict[str, dict[str, Decimal]]
    economic_effect_usd_by_type: dict[str, Decimal]
    total_economic_effect_usd: Decimal
    gross_fill_notional_usd: Decimal
    unvalued_economic_event_count: int


@dataclass(frozen=True, slots=True)
class LedgerReconciliation:
    matched: bool
    expected_balances: dict[str, Decimal]
    exchange_balances: dict[str, Decimal]
    differences: dict[str, Decimal]
    tolerances: dict[str, Decimal]
    unexplained_assets: tuple[str, ...]
    projection: EconomicLedgerProjection


def apply_economic_ledger_migration(conn: sqlite3.Connection) -> None:
    """Create the append-only ledger schema on an existing state database."""

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS economic_ledger_events (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            ledger_schema_version   INTEGER NOT NULL,
            event_key               TEXT NOT NULL UNIQUE,
            content_hash            TEXT NOT NULL,
            account_id              TEXT NOT NULL,
            trading_mode            TEXT NOT NULL,
            runtime_mode            TEXT NOT NULL DEFAULT '',
            session_id              TEXT NOT NULL DEFAULT '',
            venue                   TEXT NOT NULL,
            strategy_id             TEXT NOT NULL,
            cycle_id                TEXT NOT NULL DEFAULT '',
            intent_id               TEXT NOT NULL DEFAULT '',
            order_id                TEXT NOT NULL DEFAULT '',
            client_order_id         TEXT NOT NULL DEFAULT '',
            exchange_event_id       TEXT NOT NULL DEFAULT '',
            exchange_fill_id        TEXT NOT NULL DEFAULT '',
            source_event_id         TEXT NOT NULL DEFAULT '',
            event_type              TEXT NOT NULL,
            event_time              TEXT NOT NULL,
            recorded_at             TEXT NOT NULL,
            symbol                  TEXT NOT NULL DEFAULT '',
            instrument_type         TEXT NOT NULL DEFAULT '',
            side                    TEXT NOT NULL DEFAULT '',
            quantity                TEXT,
            quantity_asset          TEXT NOT NULL DEFAULT '',
            price                   TEXT,
            amount                  TEXT,
            amount_asset            TEXT NOT NULL DEFAULT '',
            amount_usd              TEXT,
            metadata_json           TEXT NOT NULL DEFAULT '{}',
            raw_payload_json        TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_economic_ledger_scope_time
        ON economic_ledger_events(account_id, trading_mode, venue, strategy_id, event_time, id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_economic_ledger_cycle
        ON economic_ledger_events(cycle_id, intent_id, order_id, event_time)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_economic_ledger_exchange_identity
        ON economic_ledger_events(venue, account_id, exchange_event_id, exchange_fill_id)
        """
    )
    # Database-level guards prevent accidental mutation through ad-hoc SQL.
    # A future schema migration can explicitly drop/recreate these triggers.
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_economic_ledger_no_update
        BEFORE UPDATE ON economic_ledger_events
        BEGIN
            SELECT RAISE(ABORT, 'economic ledger is append-only');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_economic_ledger_no_delete
        BEFORE DELETE ON economic_ledger_events
        BEGIN
            SELECT RAISE(ABORT, 'economic ledger is append-only');
        END
        """
    )


def _canonical_decimal(
    value: DecimalInput | None,
    field_name: str,
    *,
    required: bool = False,
) -> str | None:
    if value is None:
        if required:
            raise LedgerValidationError(f"{field_name} is required")
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise LedgerValidationError(f"{field_name} must be a finite decimal") from exc
    if not parsed.is_finite():
        raise LedgerValidationError(f"{field_name} must be a finite decimal")
    if parsed == 0:
        return "0"
    return format(parsed.normalize(), "f")


def _decimal(value: DecimalInput, field_name: str) -> Decimal:
    normalized = _canonical_decimal(value, field_name, required=True)
    assert normalized is not None
    return Decimal(normalized)


def _canonical_json(value: Mapping[str, Any] | None) -> str | None:
    if value is None:
        return None
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
            default=str,
        )
    except (TypeError, ValueError) as exc:
        raise LedgerValidationError("ledger metadata/raw payload must be JSON serializable") from exc


def _canonical_time(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise LedgerValidationError("event_time must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _require_text(value: Any, field_name: str, *, lower: bool = False, upper: bool = False) -> str:
    text = str(value or "").strip()
    if not text:
        raise LedgerValidationError(f"{field_name} is required")
    if lower:
        return text.lower()
    if upper:
        return text.upper()
    return text


def _optional_text(value: Any, *, lower: bool = False, upper: bool = False) -> str:
    text = str(value or "").strip()
    if lower:
        return text.lower()
    if upper:
        return text.upper()
    return text


def _normalize_event(event: EconomicLedgerEvent) -> dict[str, Any]:
    if event.schema_version != ECONOMIC_LEDGER_SCHEMA_VERSION:
        raise LedgerValidationError(
            f"unsupported economic ledger schema version {event.schema_version}"
        )

    event_type = _require_text(event.event_type, "event_type", upper=True)
    if event_type not in ECONOMIC_EVENT_TYPES:
        raise LedgerValidationError(f"unsupported economic event type {event_type!r}")

    normalized: dict[str, Any] = {
        "ledger_schema_version": event.schema_version,
        "account_id": _require_text(event.account_id, "account_id"),
        "trading_mode": _require_text(event.trading_mode, "trading_mode", lower=True),
        "runtime_mode": _optional_text(event.runtime_mode, upper=True),
        "session_id": _optional_text(event.session_id),
        "venue": _require_text(event.venue, "venue", upper=True),
        "strategy_id": _require_text(event.strategy_id, "strategy_id"),
        "cycle_id": _optional_text(event.cycle_id),
        "intent_id": _optional_text(event.intent_id),
        "order_id": _optional_text(event.order_id),
        "client_order_id": _optional_text(event.client_order_id),
        "exchange_event_id": _optional_text(event.exchange_event_id),
        "exchange_fill_id": _optional_text(event.exchange_fill_id),
        "source_event_id": _optional_text(event.source_event_id),
        "event_type": event_type,
        "event_time": _canonical_time(event.event_time),
        "symbol": _optional_text(event.symbol, upper=True),
        "instrument_type": _optional_text(event.instrument_type, upper=True),
        "side": _optional_text(event.side, upper=True),
        "quantity": _canonical_decimal(event.quantity, "quantity"),
        "quantity_asset": _optional_text(event.quantity_asset, upper=True),
        "price": _canonical_decimal(event.price, "price"),
        "amount": _canonical_decimal(event.amount, "amount"),
        "amount_asset": _optional_text(event.amount_asset, upper=True),
        "amount_usd": _canonical_decimal(event.amount_usd, "amount_usd"),
        "metadata_json": _canonical_json(event.metadata) or "{}",
        "raw_payload_json": _canonical_json(event.raw_payload),
    }

    if not (
        normalized["exchange_fill_id"]
        or normalized["exchange_event_id"]
        or normalized["source_event_id"]
    ):
        raise LedgerValidationError(
            "exchange_fill_id, exchange_event_id or source_event_id is required"
        )
    if event_type in {FILL, COMMISSION} and not (
        normalized["exchange_fill_id"] or normalized["source_event_id"]
    ):
        raise LedgerValidationError(
            f"{event_type.lower()} requires exchange_fill_id or source_event_id"
        )
    if event_type == COMMISSION:
        if not normalized["symbol"]:
            raise LedgerValidationError("commission symbol is required")
        if normalized["instrument_type"] not in {"SPOT", "PERPETUAL"}:
            raise LedgerValidationError(
                "commission instrument_type must be SPOT or PERPETUAL"
            )
    if event_type in {FUNDING, REALIZED_PNL}:
        if not normalized["symbol"]:
            raise LedgerValidationError(f"{event_type.lower()} symbol is required")
        if normalized["instrument_type"] != "PERPETUAL":
            raise LedgerValidationError(
                f"{event_type.lower()} instrument_type must be PERPETUAL"
            )
    if event_type == FILL:
        if not normalized["symbol"]:
            raise LedgerValidationError("fill symbol is required")
        if normalized["instrument_type"] not in {"SPOT", "PERPETUAL"}:
            raise LedgerValidationError("fill instrument_type must be SPOT or PERPETUAL")
        if normalized["side"] not in {"BUY", "SELL"}:
            raise LedgerValidationError("fill side must be BUY or SELL")
        if not normalized["quantity_asset"]:
            raise LedgerValidationError("fill quantity_asset is required")
        quantity = Decimal(normalized["quantity"] or "0")
        price = Decimal(normalized["price"] or "0")
        if quantity == 0 or price <= 0:
            raise LedgerValidationError("fill quantity must be non-zero and price must be positive")
        if (quantity > 0) != (normalized["side"] == "BUY"):
            raise LedgerValidationError("fill quantity sign must agree with side")
        if not normalized["amount_asset"]:
            raise LedgerValidationError("fill quote amount_asset is required")
        fill_amount_text = normalized["amount"]
        if normalized["instrument_type"] == "SPOT":
            if fill_amount_text is None or Decimal(fill_amount_text) != -(quantity * price):
                raise LedgerValidationError(
                    "spot fill amount must equal the signed quote balance change"
                )
        elif fill_amount_text is not None and Decimal(fill_amount_text) != 0:
            raise LedgerValidationError(
                "perpetual fill must not embed a wallet balance change"
            )
    elif event_type in CASHFLOW_EVENT_TYPES:
        if not normalized["amount_asset"]:
            raise LedgerValidationError(f"{event_type.lower()} amount_asset is required")
        cashflow_amount = Decimal(normalized["amount"] or "0")
        if cashflow_amount == 0:
            raise LedgerValidationError(f"{event_type.lower()} amount must be non-zero")
        if event_type in {COMMISSION, BORROW_INTEREST} and cashflow_amount >= 0:
            raise LedgerValidationError(f"{event_type.lower()} amount must be a negative charge")
        if normalized["amount_usd"] is not None:
            usd_effect = Decimal(normalized["amount_usd"])
            if event_type in {COMMISSION, BORROW_INTEREST} and usd_effect >= 0:
                raise LedgerValidationError(
                    f"{event_type.lower()} amount_usd must be a negative charge"
                )
            if event_type in {REALIZED_PNL, FUNDING, BALANCE_ADJUSTMENT} and (
                usd_effect == 0 or (usd_effect > 0) != (cashflow_amount > 0)
            ):
                raise LedgerValidationError(
                    f"{event_type.lower()} amount_usd sign must agree with amount"
                )

    identity_kind: str
    identity_value: str
    if normalized["exchange_fill_id"]:
        identity_kind = "exchange_fill"
        identity_value = normalized["exchange_fill_id"]
    elif normalized["exchange_event_id"]:
        identity_kind = "exchange_event"
        identity_value = normalized["exchange_event_id"]
    else:
        identity_kind = "source_event"
        identity_value = normalized["source_event_id"]

    # Fill IDs may be scoped to an exchange order; source IDs may be scoped to
    # an internal cycle.  These linkage fields therefore participate only when
    # the stronger identity needs that scope.  All lineage remains stored and
    # protected by the content hash.
    identity_payload = {
        "schema": normalized["ledger_schema_version"],
        "account_id": normalized["account_id"],
        "trading_mode": normalized["trading_mode"],
        "venue": normalized["venue"],
        "strategy_id": normalized["strategy_id"],
        "event_type": normalized["event_type"],
        "symbol": normalized["symbol"],
        "instrument_type": normalized["instrument_type"],
        "amount_asset": normalized["amount_asset"],
        "identity_kind": identity_kind,
        "identity_value": identity_value,
        "order_id": normalized["order_id"] if identity_kind == "exchange_fill" else "",
        "cycle_id": normalized["cycle_id"] if identity_kind == "source_event" else "",
        "intent_id": normalized["intent_id"] if identity_kind == "source_event" else "",
    }
    event_key = hashlib.sha256(
        json.dumps(identity_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    # Raw payload, observation session and runtime mode are deliberately
    # excluded: replaying the same exchange identity after a process restart or
    # while fail-closed must remain idempotent.  Economic and causal lineage
    # (account/strategy/cycle/intent/order) remains protected.
    economic_content = dict(normalized)
    economic_content.pop("raw_payload_json", None)
    economic_content.pop("runtime_mode", None)
    economic_content.pop("session_id", None)
    content_hash = hashlib.sha256(
        json.dumps(economic_content, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    normalized["event_key"] = event_key
    normalized["content_hash"] = content_hash
    normalized["recorded_at"] = datetime.now(timezone.utc).isoformat()
    return normalized


_INSERT_COLUMNS = (
    "ledger_schema_version",
    "event_key",
    "content_hash",
    "account_id",
    "trading_mode",
    "runtime_mode",
    "session_id",
    "venue",
    "strategy_id",
    "cycle_id",
    "intent_id",
    "order_id",
    "client_order_id",
    "exchange_event_id",
    "exchange_fill_id",
    "source_event_id",
    "event_type",
    "event_time",
    "recorded_at",
    "symbol",
    "instrument_type",
    "side",
    "quantity",
    "quantity_asset",
    "price",
    "amount",
    "amount_asset",
    "amount_usd",
    "metadata_json",
    "raw_payload_json",
)


def ingest_economic_events(
    conn: sqlite3.Connection,
    events: Iterable[EconomicLedgerEvent],
) -> LedgerIngestionResult:
    """Atomically append events, making exact replay a no-op.

    A duplicate stable identity with identical normalized content is counted as
    a duplicate.  The same identity with different economics raises
    :class:`LedgerIdempotencyConflict` and rolls the entire batch back.
    """

    normalized_events = [_normalize_event(event) for event in events]
    if not normalized_events:
        return LedgerIngestionResult(0, 0, 0, ())

    inserted = 0
    duplicates = 0
    keys: list[str] = []
    savepoint = "economic_ledger_ingest"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        placeholders = ", ".join("?" for _ in _INSERT_COLUMNS)
        columns = ", ".join(_INSERT_COLUMNS)
        sql = f"INSERT OR IGNORE INTO economic_ledger_events ({columns}) VALUES ({placeholders})"
        for item in normalized_events:
            cursor = conn.execute(sql, tuple(item[column] for column in _INSERT_COLUMNS))
            event_key = str(item["event_key"])
            keys.append(event_key)
            if cursor.rowcount == 1:
                inserted += 1
                continue
            existing = conn.execute(
                "SELECT content_hash FROM economic_ledger_events WHERE event_key = ?",
                (event_key,),
            ).fetchone()
            if existing is None or str(existing["content_hash"]) != str(item["content_hash"]):
                raise LedgerIdempotencyConflict(
                    f"economic event identity collision for key {event_key}"
                )
            duplicates += 1
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    except Exception:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise

    return LedgerIngestionResult(
        requested=len(normalized_events),
        inserted=inserted,
        duplicates=duplicates,
        event_keys=tuple(keys),
    )


def build_fill_events(
    *,
    account_id: str,
    trading_mode: str,
    venue: str,
    strategy_id: str,
    event_time: str,
    symbol: str,
    instrument_type: str,
    side: str,
    quantity: DecimalInput,
    price: DecimalInput,
    quantity_asset: str,
    quote_asset: str,
    exchange_fill_id: str = "",
    source_event_id: str = "",
    exchange_event_id: str = "",
    cycle_id: str = "",
    intent_id: str = "",
    order_id: str = "",
    client_order_id: str = "",
    commission_amount: DecimalInput | None = None,
    commission_asset: str = "",
    commission_amount_usd: DecimalInput | None = None,
    realized_pnl_amount: DecimalInput | None = None,
    realized_pnl_asset: str = "",
    realized_pnl_amount_usd: DecimalInput | None = None,
    runtime_mode: str = "",
    session_id: str = "",
    metadata: Mapping[str, Any] | None = None,
    raw_payload: Mapping[str, Any] | None = None,
) -> tuple[EconomicLedgerEvent, ...]:
    """Build a normalized fill and optional commission as one atomic batch."""

    side_upper = _require_text(side, "side", upper=True)
    if side_upper not in {"BUY", "SELL"}:
        raise LedgerValidationError("side must be BUY or SELL")
    instrument_upper = _require_text(instrument_type, "instrument_type", upper=True)
    if instrument_upper not in {"SPOT", "PERPETUAL"}:
        raise LedgerValidationError("instrument_type must be SPOT or PERPETUAL")
    quantity_decimal = _decimal(quantity, "quantity")
    price_decimal = _decimal(price, "price")
    if quantity_decimal <= 0 or price_decimal <= 0:
        raise LedgerValidationError("fill quantity and price must be positive inputs")
    signed_quantity = quantity_decimal if side_upper == "BUY" else -quantity_decimal

    # Spot execution changes both inventory and quote balance.  Perpetual fills
    # change contract position; wallet PnL/fees arrive as separate cash events.
    quote_asset_upper = _require_text(quote_asset, "quote_asset", upper=True)
    quote_delta: Decimal | None = None
    if instrument_upper == "SPOT":
        quote_delta = -(signed_quantity * price_decimal)

    common = {
        "event_time": event_time,
        "account_id": account_id,
        "trading_mode": trading_mode,
        "venue": venue,
        "strategy_id": strategy_id,
        "source_event_id": source_event_id,
        "symbol": symbol,
        "instrument_type": instrument_upper,
        "cycle_id": cycle_id,
        "intent_id": intent_id,
        "order_id": order_id,
        "client_order_id": client_order_id,
        "exchange_event_id": exchange_event_id,
        "exchange_fill_id": exchange_fill_id,
        "runtime_mode": runtime_mode,
        "session_id": session_id,
        "metadata": metadata or {},
        "raw_payload": raw_payload,
    }
    result: list[EconomicLedgerEvent] = [
        EconomicLedgerEvent(
            event_type=FILL,
            side=side_upper,
            quantity=signed_quantity,
            quantity_asset=quantity_asset,
            price=price_decimal,
            amount=quote_delta,
            amount_asset=quote_asset_upper,
            **common,
        )
    ]

    if commission_amount is not None:
        commission_decimal = _decimal(commission_amount, "commission_amount")
        if commission_decimal < 0:
            raise LedgerValidationError("commission_amount must be a non-negative charge")
        if commission_decimal > 0:
            commission_asset_upper = _require_text(
                commission_asset, "commission_asset", upper=True
            )
            commission_usd: Decimal | None
            if commission_amount_usd is not None:
                supplied_usd = _decimal(commission_amount_usd, "commission_amount_usd")
                if supplied_usd <= 0:
                    raise LedgerValidationError(
                        "commission_amount_usd must be a positive charge"
                    )
                commission_usd = -supplied_usd
            elif commission_asset_upper in STABLE_QUOTE_ASSETS:
                commission_usd = -commission_decimal
            else:
                commission_usd = None
            result.append(
                EconomicLedgerEvent(
                    event_type=COMMISSION,
                    amount=-commission_decimal,
                    amount_asset=commission_asset_upper,
                    amount_usd=commission_usd,
                    **common,
                )
            )

    # Binance futures reports incremental realized PnL on each TRADE update.
    # It is a wallet cash flow, separate from the position-changing fill and
    # from commission.  Keep all three in the same ingestion transaction so a
    # lifecycle consumer can never observe a fill without its economics.
    if realized_pnl_amount is not None:
        realized_decimal = _decimal(realized_pnl_amount, "realized_pnl_amount")
        if realized_decimal != 0:
            if instrument_upper != "PERPETUAL":
                raise LedgerValidationError(
                    "realized_pnl_amount is only valid for PERPETUAL fills"
                )
            realized_asset_upper = _require_text(
                realized_pnl_asset or quote_asset_upper,
                "realized_pnl_asset",
                upper=True,
            )
            if realized_pnl_amount_usd is not None:
                realized_usd = _decimal(
                    realized_pnl_amount_usd,
                    "realized_pnl_amount_usd",
                )
                if realized_usd == 0 or (realized_usd > 0) != (realized_decimal > 0):
                    raise LedgerValidationError(
                        "realized_pnl_amount_usd sign must agree with realized_pnl_amount"
                    )
            elif realized_asset_upper in STABLE_QUOTE_ASSETS:
                realized_usd = realized_decimal
            else:
                realized_usd = None
            result.append(
                EconomicLedgerEvent(
                    event_type=REALIZED_PNL,
                    amount=realized_decimal,
                    amount_asset=realized_asset_upper,
                    amount_usd=realized_usd,
                    **common,
                )
            )
    return tuple(result)


def build_commission_event(
    *,
    account_id: str,
    trading_mode: str,
    venue: str,
    strategy_id: str,
    event_time: str,
    commission_amount: DecimalInput,
    commission_asset: str,
    exchange_fill_id: str = "",
    source_event_id: str = "",
    exchange_event_id: str = "",
    commission_amount_usd: DecimalInput | None = None,
    symbol: str = "",
    instrument_type: str = "",
    cycle_id: str = "",
    intent_id: str = "",
    order_id: str = "",
    client_order_id: str = "",
    runtime_mode: str = "",
    session_id: str = "",
    metadata: Mapping[str, Any] | None = None,
    raw_payload: Mapping[str, Any] | None = None,
) -> EconomicLedgerEvent:
    """Build a standalone fill commission with negative balance/PnL signs."""

    commission_decimal = _decimal(commission_amount, "commission_amount")
    if commission_decimal <= 0:
        raise LedgerValidationError("commission_amount must be a positive charge")
    asset_upper = _require_text(commission_asset, "commission_asset", upper=True)
    if commission_amount_usd is not None:
        supplied_usd = _decimal(commission_amount_usd, "commission_amount_usd")
        if supplied_usd <= 0:
            raise LedgerValidationError(
                "commission_amount_usd must be a positive charge"
            )
        signed_usd: Decimal | None = -supplied_usd
    elif asset_upper in STABLE_QUOTE_ASSETS:
        signed_usd = -commission_decimal
    else:
        signed_usd = None
    return EconomicLedgerEvent(
        event_type=COMMISSION,
        event_time=event_time,
        account_id=account_id,
        trading_mode=trading_mode,
        venue=venue,
        strategy_id=strategy_id,
        source_event_id=source_event_id,
        symbol=symbol,
        instrument_type=instrument_type,
        amount=-commission_decimal,
        amount_asset=asset_upper,
        amount_usd=signed_usd,
        cycle_id=cycle_id,
        intent_id=intent_id,
        order_id=order_id,
        client_order_id=client_order_id,
        exchange_event_id=exchange_event_id,
        exchange_fill_id=exchange_fill_id,
        runtime_mode=runtime_mode,
        session_id=session_id,
        metadata=metadata or {},
        raw_payload=raw_payload,
    )


def build_cashflow_event(
    *,
    event_type: str,
    account_id: str,
    trading_mode: str,
    venue: str,
    strategy_id: str,
    event_time: str,
    asset: str,
    amount: DecimalInput,
    exchange_event_id: str = "",
    source_event_id: str = "",
    amount_usd: DecimalInput | None = None,
    symbol: str = "",
    instrument_type: str = "",
    cycle_id: str = "",
    intent_id: str = "",
    order_id: str = "",
    client_order_id: str = "",
    runtime_mode: str = "",
    session_id: str = "",
    metadata: Mapping[str, Any] | None = None,
    raw_payload: Mapping[str, Any] | None = None,
) -> EconomicLedgerEvent:
    """Build funding, interest or balance cashflow with signed semantics.

    Funding and balance adjustments accept the exchange-reported signed amount.
    Borrow/interest accepts the positive charge and stores it as a negative
    balance/economic effect.
    """

    normalized_type = _require_text(event_type, "event_type", upper=True)
    if normalized_type not in {
        REALIZED_PNL,
        FUNDING,
        BORROW_INTEREST,
        BALANCE_ADJUSTMENT,
    }:
        raise LedgerValidationError("cashflow event_type is not supported")
    asset_upper = _require_text(asset, "asset", upper=True)
    amount_decimal = _decimal(amount, "amount")
    if amount_decimal == 0:
        raise LedgerValidationError("cashflow amount must be non-zero")
    if normalized_type == BORROW_INTEREST:
        if amount_decimal < 0:
            raise LedgerValidationError("borrow/interest amount must be a positive charge")
        signed_amount = -amount_decimal
    else:
        signed_amount = amount_decimal

    if amount_usd is not None:
        usd_decimal = _decimal(amount_usd, "amount_usd")
        if normalized_type == BORROW_INTEREST:
            if usd_decimal <= 0:
                raise LedgerValidationError(
                    "borrow/interest amount_usd must be a positive charge"
                )
            signed_usd: Decimal | None = -usd_decimal
        else:
            # Realized-PnL/funding/balance USD effects use the same signed convention.
            if usd_decimal == 0 or (usd_decimal > 0) != (signed_amount > 0):
                raise LedgerValidationError(
                    "amount_usd sign must agree with the signed cashflow amount"
                )
            signed_usd = usd_decimal
    elif asset_upper in STABLE_QUOTE_ASSETS:
        signed_usd = signed_amount
    else:
        signed_usd = None

    return EconomicLedgerEvent(
        event_type=normalized_type,
        event_time=event_time,
        account_id=account_id,
        trading_mode=trading_mode,
        venue=venue,
        strategy_id=strategy_id,
        source_event_id=source_event_id,
        symbol=symbol,
        instrument_type=instrument_type,
        amount=signed_amount,
        amount_asset=asset_upper,
        amount_usd=signed_usd,
        cycle_id=cycle_id,
        intent_id=intent_id,
        order_id=order_id,
        client_order_id=client_order_id,
        exchange_event_id=exchange_event_id,
        runtime_mode=runtime_mode,
        session_id=session_id,
        metadata=metadata or {},
        raw_payload=raw_payload,
    )


def _where_clause(
    *,
    account_id: str | None = None,
    trading_mode: str | None = None,
    venue: str | None = None,
    strategy_id: str | None = None,
    cycle_id: str | None = None,
    symbol: str | None = None,
    instrument_type: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
) -> tuple[str, list[Any]]:
    conditions: list[str] = []
    params: list[Any] = []
    filters = (
        ("account_id", account_id, None),
        ("trading_mode", trading_mode, "lower"),
        ("venue", venue, "upper"),
        ("strategy_id", strategy_id, None),
        ("cycle_id", cycle_id, None),
        ("symbol", symbol, "upper"),
        ("instrument_type", instrument_type, "upper"),
    )
    for column, value, transform in filters:
        if value is None:
            continue
        text = str(value).strip()
        if transform == "lower":
            text = text.lower()
        elif transform == "upper":
            text = text.upper()
        conditions.append(f"{column} = ?")
        params.append(text)
    if start_time is not None:
        conditions.append("event_time >= ?")
        params.append(_canonical_time(start_time))
    if end_time is not None:
        conditions.append("event_time <= ?")
        params.append(_canonical_time(end_time))
    return (f"WHERE {' AND '.join(conditions)}" if conditions else "", params)


def read_economic_events(
    conn: sqlite3.Connection,
    *,
    account_id: str | None = None,
    trading_mode: str | None = None,
    venue: str | None = None,
    strategy_id: str | None = None,
    cycle_id: str | None = None,
    symbol: str | None = None,
    instrument_type: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    limit: int | None = 1000,
) -> list[dict[str, Any]]:
    where, params = _where_clause(
        account_id=account_id,
        trading_mode=trading_mode,
        venue=venue,
        strategy_id=strategy_id,
        cycle_id=cycle_id,
        symbol=symbol,
        instrument_type=instrument_type,
        start_time=start_time,
        end_time=end_time,
    )
    sql = f"SELECT * FROM economic_ledger_events {where} ORDER BY event_time ASC, id ASC"
    if limit is not None:
        if limit < 0:
            raise LedgerValidationError("limit must be non-negative or None")
        sql += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(sql, tuple(params)).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        for field_name in ("metadata_json", "raw_payload_json"):
            value = item.get(field_name)
            if value is not None:
                item[field_name.removesuffix("_json")] = json.loads(value)
            item.pop(field_name, None)
        result.append(item)
    return result


def project_economic_ledger(
    conn: sqlite3.Connection,
    *,
    account_id: str | None = None,
    trading_mode: str | None = None,
    venue: str | None = None,
    strategy_id: str | None = None,
    cycle_id: str | None = None,
    symbol: str | None = None,
    instrument_type: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
) -> EconomicLedgerProjection:
    rows = read_economic_events(
        conn,
        account_id=account_id,
        trading_mode=trading_mode,
        venue=venue,
        strategy_id=strategy_id,
        cycle_id=cycle_id,
        symbol=symbol,
        instrument_type=instrument_type,
        start_time=start_time,
        end_time=end_time,
        limit=None,
    )
    balance_deltas: dict[str, Decimal] = {}
    spot_inventory: dict[str, Decimal] = {}
    perpetual_positions: dict[str, Decimal] = {}
    by_type_asset: dict[str, dict[str, Decimal]] = {}
    usd_by_type: dict[str, Decimal] = {}
    gross_fill_notional = Decimal("0")
    unvalued = 0
    fill_count = 0

    def add(target: dict[str, Decimal], key: str, value: Decimal) -> None:
        if key:
            target[key] = target.get(key, Decimal("0")) + value

    for row in rows:
        event_type = str(row["event_type"])
        amount_text = row.get("amount")
        amount_asset = str(row.get("amount_asset") or "")
        amount = Decimal(str(amount_text)) if amount_text is not None else None
        if amount is not None and amount_asset:
            add(balance_deltas, amount_asset, amount)
            event_assets = by_type_asset.setdefault(event_type, {})
            add(event_assets, amount_asset, amount)

        quantity_text = row.get("quantity")
        quantity_asset = str(row.get("quantity_asset") or "")
        quantity = Decimal(str(quantity_text)) if quantity_text is not None else None
        if event_type == FILL and quantity is not None:
            fill_count += 1
            row_instrument_type = str(row.get("instrument_type") or "")
            if row_instrument_type == "SPOT":
                add(spot_inventory, quantity_asset, quantity)
                add(balance_deltas, quantity_asset, quantity)
            elif row_instrument_type == "PERPETUAL":
                add(perpetual_positions, str(row.get("symbol") or ""), quantity)
            price_text = row.get("price")
            if price_text is not None and amount_asset in STABLE_QUOTE_ASSETS:
                gross_fill_notional += abs(quantity * Decimal(str(price_text)))

        usd_text = row.get("amount_usd")
        if usd_text is not None and event_type in ECONOMIC_EFFECT_TYPES:
            add(usd_by_type, event_type, Decimal(str(usd_text)))
        elif event_type in ECONOMIC_EFFECT_TYPES and amount not in {None, Decimal("0")}:
            unvalued += 1

    total_usd = sum(usd_by_type.values(), Decimal("0"))
    return EconomicLedgerProjection(
        event_count=len(rows),
        fill_count=fill_count,
        balance_deltas=balance_deltas,
        spot_inventory_deltas=spot_inventory,
        perpetual_position_deltas=perpetual_positions,
        amounts_by_type_and_asset=by_type_asset,
        economic_effect_usd_by_type=usd_by_type,
        total_economic_effect_usd=total_usd,
        gross_fill_notional_usd=gross_fill_notional,
        unvalued_economic_event_count=unvalued,
    )


def reconcile_economic_ledger(
    conn: sqlite3.Connection,
    *,
    exchange_balances: Mapping[str, DecimalInput],
    opening_balances: Mapping[str, DecimalInput] | None = None,
    tolerances: Mapping[str, DecimalInput] | None = None,
    account_id: str | None = None,
    trading_mode: str | None = None,
    venue: str | None = None,
    strategy_id: str | None = None,
    cycle_id: str | None = None,
    symbol: str | None = None,
    instrument_type: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
) -> LedgerReconciliation:
    """Compare a replayed balance projection with an exchange snapshot."""

    projection = project_economic_ledger(
        conn,
        account_id=account_id,
        trading_mode=trading_mode,
        venue=venue,
        strategy_id=strategy_id,
        cycle_id=cycle_id,
        symbol=symbol,
        instrument_type=instrument_type,
        start_time=start_time,
        end_time=end_time,
    )
    opening = {
        str(asset).upper(): _decimal(value, f"opening balance {asset}")
        for asset, value in (opening_balances or {}).items()
    }
    actual = {
        str(asset).upper(): _decimal(value, f"exchange balance {asset}")
        for asset, value in exchange_balances.items()
    }
    tolerance_map = {
        str(asset).upper(): abs(_decimal(value, f"tolerance {asset}"))
        for asset, value in (tolerances or {}).items()
    }
    assets = sorted(set(opening) | set(actual) | set(projection.balance_deltas))
    expected: dict[str, Decimal] = {}
    differences: dict[str, Decimal] = {}
    effective_tolerances: dict[str, Decimal] = {}
    unexplained: list[str] = []
    for asset in assets:
        expected_value = opening.get(asset, Decimal("0")) + projection.balance_deltas.get(
            asset, Decimal("0")
        )
        actual_value = actual.get(asset, Decimal("0"))
        difference = actual_value - expected_value
        tolerance = tolerance_map.get(asset, Decimal("0"))
        expected[asset] = expected_value
        differences[asset] = difference
        effective_tolerances[asset] = tolerance
        if abs(difference) > tolerance:
            unexplained.append(asset)

    return LedgerReconciliation(
        matched=not unexplained,
        expected_balances=expected,
        exchange_balances=actual,
        differences=differences,
        tolerances=effective_tolerances,
        unexplained_assets=tuple(unexplained),
        projection=projection,
    )
