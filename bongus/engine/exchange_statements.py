"""Normalized, durable exchange account-statement evidence.

Exchange account statements are a second source of economic truth alongside
order/fill telemetry.  Some statement rows are the authoritative source of a
cash flow (funding, transfers, and margin interest); other rows repeat
economics already carried by trade fills (commission and realized PnL).  This
module makes that distinction explicit so replay can be idempotent without
double counting.

Numeric values are normalized to canonical decimal text.  Statement identity
is the tuple ``(venue, account, source, type, exchange transaction id)``.  A
replay under that identity is accepted only when its canonical content hash is
unchanged.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from bongus.engine.economic_ledger import (
    BALANCE_ADJUSTMENT,
    BORROW_INTEREST,
    FUNDING,
    EconomicLedgerEvent,
    LedgerIngestionResult,
    LedgerValidationError,
    build_cashflow_event,
    ingest_economic_events,
)

EXCHANGE_STATEMENT_SCHEMA_VERSION = 1

BINANCE_FUTURES_INCOME = "BINANCE_FUTURES_INCOME"
BINANCE_MARGIN_INTEREST = "BINANCE_MARGIN_INTEREST"
MARGIN_INTEREST = "MARGIN_INTEREST"

LEDGERED = "LEDGERED"
MATCH_REQUIRED = "MATCH_REQUIRED"
UNMAPPED = "UNMAPPED"

STATEMENT_RECONCILIATION_STATUSES = frozenset({LEDGERED, MATCH_REQUIRED, UNMAPPED})
FUTURES_INCOME_MATCH_REQUIRED_TYPES = frozenset({"COMMISSION", "REALIZED_PNL"})


class ExchangeStatementValidationError(ValueError):
    """An exchange statement row is incomplete or economically ambiguous."""


class ExchangeStatementIdempotencyConflict(RuntimeError):
    """A statement transaction identity was reused with different content."""


@dataclass(frozen=True, slots=True)
class NormalizedExchangeStatement:
    """One canonical exchange statement row and its optional ledger effect."""

    statement_schema_version: int
    statement_key: str
    content_hash: str
    canonical_content_json: str
    venue: str
    account_id: str
    trading_mode: str
    statement_source: str
    statement_type: str
    exchange_transaction_id: str
    event_time: str
    event_time_ms: int
    symbol: str
    asset: str
    amount: str
    order_id: str
    trade_id: str
    reconciliation_status: str
    raw_payload_json: str
    economic_event: EconomicLedgerEvent | None = None


@dataclass(frozen=True, slots=True)
class ExchangeStatementIngestionResult:
    statement_key: str
    content_hash: str
    inserted: bool
    duplicate: bool
    reconciliation_status: str
    ledger_result: LedgerIngestionResult
    cursor_advanced: bool


def _require_text(
    value: Any,
    field_name: str,
    *,
    upper: bool = False,
    lower: bool = False,
) -> str:
    if value is None:
        raise ExchangeStatementValidationError(f"{field_name} is required")
    text = str(value).strip()
    if not text:
        raise ExchangeStatementValidationError(f"{field_name} is required")
    if len(text) > 256:
        raise ExchangeStatementValidationError(f"{field_name} is too long")
    if upper:
        return text.upper()
    if lower:
        return text.lower()
    return text


def _optional_text(value: Any, *, upper: bool = False) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if len(text) > 256:
        raise ExchangeStatementValidationError("statement text field is too long")
    return text.upper() if upper else text


def _transaction_id(value: Any, field_name: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ExchangeStatementValidationError(
            f"{field_name} must be a string or integer exchange identifier"
        )
    return _require_text(value, field_name)


def _canonical_decimal(value: Any, field_name: str) -> str:
    if value is None or isinstance(value, bool):
        raise ExchangeStatementValidationError(f"{field_name} is required")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ExchangeStatementValidationError(
            f"{field_name} must be a finite decimal"
        ) from exc
    if not parsed.is_finite():
        raise ExchangeStatementValidationError(f"{field_name} must be a finite decimal")
    if parsed == 0:
        return "0"
    return format(parsed.normalize(), "f")


def _timestamp_ms(value: Any, field_name: str) -> int:
    if value is None or isinstance(value, bool):
        raise ExchangeStatementValidationError(f"{field_name} is required")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ExchangeStatementValidationError(
            f"{field_name} must be an integer Unix timestamp in milliseconds"
        ) from exc
    if not parsed.is_finite() or parsed != parsed.to_integral_value() or parsed < 0:
        raise ExchangeStatementValidationError(
            f"{field_name} must be a non-negative integer Unix timestamp in milliseconds"
        )
    milliseconds = int(parsed)
    seconds, remainder_ms = divmod(milliseconds, 1000)
    try:
        _ = datetime.fromtimestamp(seconds, tz=timezone.utc) + timedelta(
            milliseconds=remainder_ms
        )
    except (OverflowError, OSError, ValueError) as exc:
        raise ExchangeStatementValidationError(f"{field_name} is out of range") from exc
    return milliseconds


def _iso_from_timestamp_ms(milliseconds: int) -> str:
    seconds, remainder_ms = divmod(milliseconds, 1000)
    return (
        datetime.fromtimestamp(seconds, tz=timezone.utc)
        + timedelta(milliseconds=remainder_ms)
    ).isoformat()


def _canonical_json(value: Mapping[str, Any], field_name: str) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ExchangeStatementValidationError(
            f"{field_name} must be a JSON-serializable object"
        ) from exc


def _statement_identity_key(
    *,
    venue: str,
    account_id: str,
    statement_source: str,
    statement_type: str,
    exchange_transaction_id: str,
) -> str:
    identity = {
        "venue": venue,
        "account_id": account_id,
        "statement_source": statement_source,
        "statement_type": statement_type,
        "exchange_transaction_id": exchange_transaction_id,
    }
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def statement_cursor_key(*, venue: str, account_id: str, statement_source: str) -> str:
    """Return the stable identity for one independently paginated statement feed."""

    identity = {
        "venue": _require_text(venue, "venue", upper=True),
        "account_id": _require_text(account_id, "account_id"),
        "statement_source": _require_text(
            statement_source,
            "statement_source",
            upper=True,
        ),
    }
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _build_statement(
    *,
    venue: str,
    account_id: str,
    trading_mode: str,
    statement_source: str,
    statement_type: str,
    exchange_transaction_id: str,
    event_time_ms: int,
    symbol: str,
    asset: str,
    amount: str,
    order_id: str,
    trade_id: str,
    reconciliation_status: str,
    raw_payload: Mapping[str, Any],
    canonical_source_extras: Mapping[str, Any],
    economic_event: EconomicLedgerEvent | None,
) -> NormalizedExchangeStatement:
    normalized_venue = _require_text(venue, "venue", upper=True)
    normalized_account_id = _require_text(account_id, "account_id")
    normalized_mode = _require_text(trading_mode, "trading_mode", lower=True)
    normalized_source = _require_text(
        statement_source,
        "statement_source",
        upper=True,
    )
    normalized_type = _require_text(statement_type, "statement_type", upper=True)
    normalized_transaction_id = _transaction_id(
        exchange_transaction_id,
        "exchange_transaction_id",
    )
    normalized_symbol = _optional_text(symbol, upper=True)
    normalized_asset = _require_text(asset, "asset", upper=True)
    normalized_amount = _canonical_decimal(amount, "amount")
    normalized_order_id = _optional_text(order_id)
    normalized_trade_id = _optional_text(trade_id)
    normalized_status = _require_text(
        reconciliation_status,
        "reconciliation_status",
        upper=True,
    )
    if normalized_status not in STATEMENT_RECONCILIATION_STATUSES:
        raise ExchangeStatementValidationError(
            f"unsupported reconciliation_status {normalized_status!r}"
        )
    if (normalized_status == LEDGERED) != (economic_event is not None):
        raise ExchangeStatementValidationError(
            "LEDGERED statements require exactly one economic event; "
            "evidence-only statements must not carry one"
        )

    event_time = _iso_from_timestamp_ms(event_time_ms)
    raw_payload_json = _canonical_json(raw_payload, "raw_payload")
    canonical_extras_json = _canonical_json(
        canonical_source_extras,
        "canonical_source_extras",
    )
    statement_key = _statement_identity_key(
        venue=normalized_venue,
        account_id=normalized_account_id,
        statement_source=normalized_source,
        statement_type=normalized_type,
        exchange_transaction_id=normalized_transaction_id,
    )
    canonical_content = {
        "statement_schema_version": EXCHANGE_STATEMENT_SCHEMA_VERSION,
        "venue": normalized_venue,
        "account_id": normalized_account_id,
        "trading_mode": normalized_mode,
        "statement_source": normalized_source,
        "statement_type": normalized_type,
        "exchange_transaction_id": normalized_transaction_id,
        "event_time": event_time,
        "event_time_ms": event_time_ms,
        "symbol": normalized_symbol,
        "asset": normalized_asset,
        "amount": normalized_amount,
        "order_id": normalized_order_id,
        "trade_id": normalized_trade_id,
        "reconciliation_status": normalized_status,
        # Standard source fields are represented canonically above.  Retain
        # every additional exchange field so an identity reused with changed
        # evidence still fails closed.
        "canonical_source_extras_json": canonical_extras_json,
    }
    canonical_content_json = json.dumps(
        canonical_content,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    content_hash = hashlib.sha256(canonical_content_json.encode("utf-8")).hexdigest()
    return NormalizedExchangeStatement(
        statement_schema_version=EXCHANGE_STATEMENT_SCHEMA_VERSION,
        statement_key=statement_key,
        content_hash=content_hash,
        canonical_content_json=canonical_content_json,
        venue=normalized_venue,
        account_id=normalized_account_id,
        trading_mode=normalized_mode,
        statement_source=normalized_source,
        statement_type=normalized_type,
        exchange_transaction_id=normalized_transaction_id,
        event_time=event_time,
        event_time_ms=event_time_ms,
        symbol=normalized_symbol,
        asset=normalized_asset,
        amount=normalized_amount,
        order_id=normalized_order_id,
        trade_id=normalized_trade_id,
        reconciliation_status=normalized_status,
        raw_payload_json=raw_payload_json,
        economic_event=economic_event,
    )


def normalize_binance_futures_income(
    payload: Mapping[str, Any],
    *,
    account_id: str,
    trading_mode: str,
    strategy_id: str,
    venue: str = "BINANCE",
    runtime_mode: str = "",
    session_id: str = "",
) -> NormalizedExchangeStatement:
    """Normalize one Binance USD-M futures income row.

    ``COMMISSION`` and ``REALIZED_PNL`` are deliberately evidence-only.  The
    user-data trade fill is the canonical ledger source for those incremental
    values, so writing them here would double count the same economics.
    """

    if not isinstance(payload, Mapping):
        raise ExchangeStatementValidationError("payload must be an object")
    raw_payload = dict(payload)
    income_type = _require_text(payload.get("incomeType"), "incomeType", upper=True)
    transaction_id = _transaction_id(payload.get("tranId"), "tranId")
    timestamp = _timestamp_ms(payload.get("time"), "time")
    amount = _canonical_decimal(payload.get("income"), "income")
    asset = _require_text(payload.get("asset"), "asset", upper=True)
    symbol = _optional_text(payload.get("symbol"), upper=True)
    trade_id = (
        ""
        if payload.get("tradeId") in (None, "")
        else _transaction_id(payload.get("tradeId"), "tradeId")
    )
    order_id = (
        ""
        if payload.get("orderId") in (None, "")
        else _transaction_id(payload.get("orderId"), "orderId")
    )
    source_id: str
    economic_event: EconomicLedgerEvent | None
    if income_type == "FUNDING_FEE":
        if not symbol:
            raise ExchangeStatementValidationError("FUNDING_FEE symbol is required")
        source_id = f"binance:{_require_text(account_id, 'account_id')}:funding:{transaction_id}"
        try:
            economic_event = build_cashflow_event(
                event_type=FUNDING,
                account_id=account_id,
                trading_mode=trading_mode,
                venue=venue,
                strategy_id=strategy_id,
                event_time=_iso_from_timestamp_ms(timestamp),
                asset=asset,
                amount=amount,
                exchange_event_id=source_id,
                source_event_id=source_id,
                symbol=symbol,
                instrument_type="PERPETUAL",
                runtime_mode=runtime_mode,
                session_id=session_id,
                metadata={"income_type": "FUNDING_FEE"},
                raw_payload=raw_payload,
            )
        except LedgerValidationError as exc:
            raise ExchangeStatementValidationError(str(exc)) from exc
        status = LEDGERED
    elif income_type == "TRANSFER":
        source_id = (
            f"binance:{_require_text(account_id, 'account_id')}:"
            f"futures_transfer:{transaction_id}"
        )
        try:
            economic_event = build_cashflow_event(
                event_type=BALANCE_ADJUSTMENT,
                account_id=account_id,
                trading_mode=trading_mode,
                venue=venue,
                strategy_id=strategy_id,
                event_time=_iso_from_timestamp_ms(timestamp),
                asset=asset,
                amount=amount,
                exchange_event_id=source_id,
                source_event_id=source_id,
                symbol=symbol,
                runtime_mode=runtime_mode,
                session_id=session_id,
                metadata={"income_type": "TRANSFER"},
                raw_payload=raw_payload,
            )
        except LedgerValidationError as exc:
            raise ExchangeStatementValidationError(str(exc)) from exc
        status = LEDGERED
    elif income_type in FUTURES_INCOME_MATCH_REQUIRED_TYPES:
        if not symbol:
            raise ExchangeStatementValidationError(f"{income_type} symbol is required")
        economic_event = None
        status = MATCH_REQUIRED
    else:
        economic_event = None
        status = UNMAPPED

    standard_fields = {
        "incomeType",
        "tranId",
        "time",
        "income",
        "asset",
        "symbol",
        "tradeId",
        "orderId",
    }
    extras = {str(key): value for key, value in payload.items() if key not in standard_fields}
    return _build_statement(
        venue=venue,
        account_id=account_id,
        trading_mode=trading_mode,
        statement_source=BINANCE_FUTURES_INCOME,
        statement_type=income_type,
        exchange_transaction_id=transaction_id,
        event_time_ms=timestamp,
        symbol=symbol,
        asset=asset,
        amount=amount,
        order_id=order_id,
        trade_id=trade_id,
        reconciliation_status=status,
        raw_payload=raw_payload,
        canonical_source_extras=extras,
        economic_event=economic_event,
    )


def normalize_binance_margin_interest(
    payload: Mapping[str, Any],
    *,
    account_id: str,
    trading_mode: str,
    strategy_id: str,
    venue: str = "BINANCE",
    runtime_mode: str = "",
    session_id: str = "",
) -> NormalizedExchangeStatement:
    """Normalize one Binance cross/isolated-margin interest-history row."""

    if not isinstance(payload, Mapping):
        raise ExchangeStatementValidationError("payload must be an object")
    raw_payload = dict(payload)
    transaction_id = _transaction_id(payload.get("txId"), "txId")
    timestamp_value = payload.get("interestAccuredTime")
    if timestamp_value is None:
        # Accept the correctly-spelled alias defensively; Binance's documented
        # response currently uses ``interestAccuredTime``.
        timestamp_value = payload.get("interestAccruedTime")
    timestamp = _timestamp_ms(timestamp_value, "interestAccuredTime")
    interest = _canonical_decimal(payload.get("interest"), "interest")
    if Decimal(interest) <= 0:
        raise ExchangeStatementValidationError("interest must be a positive charge")
    asset = _require_text(payload.get("asset"), "asset", upper=True)
    symbol = _optional_text(
        payload.get("isolatedSymbol") or payload.get("symbol"),
        upper=True,
    )
    source_id = (
        f"binance:{_require_text(account_id, 'account_id')}:"
        f"margin_interest:{transaction_id}"
    )
    metadata = {
        "interest_type": _optional_text(payload.get("type"), upper=True),
        "isolated_symbol": symbol,
    }
    try:
        economic_event = build_cashflow_event(
            event_type=BORROW_INTEREST,
            account_id=account_id,
            trading_mode=trading_mode,
            venue=venue,
            strategy_id=strategy_id,
            event_time=_iso_from_timestamp_ms(timestamp),
            asset=asset,
            amount=interest,
            exchange_event_id=source_id,
            source_event_id=source_id,
            symbol=symbol,
            instrument_type="MARGIN",
            runtime_mode=runtime_mode,
            session_id=session_id,
            metadata=metadata,
            raw_payload=raw_payload,
        )
    except LedgerValidationError as exc:
        raise ExchangeStatementValidationError(str(exc)) from exc

    standard_fields = {
        "txId",
        "interestAccuredTime",
        "interestAccruedTime",
        "interest",
        "asset",
        "isolatedSymbol",
        "symbol",
    }
    extras = {str(key): value for key, value in payload.items() if key not in standard_fields}
    return _build_statement(
        venue=venue,
        account_id=account_id,
        trading_mode=trading_mode,
        statement_source=BINANCE_MARGIN_INTEREST,
        statement_type=MARGIN_INTEREST,
        exchange_transaction_id=transaction_id,
        event_time_ms=timestamp,
        symbol=symbol,
        asset=asset,
        amount=interest,
        order_id="",
        trade_id="",
        reconciliation_status=LEDGERED,
        raw_payload=raw_payload,
        canonical_source_extras=extras,
        economic_event=economic_event,
    )


def apply_exchange_statement_migration(conn: sqlite3.Connection) -> None:
    """Create the immutable statement journal and independently mutable cursor."""

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS exchange_statement_entries (
            id                          INTEGER PRIMARY KEY AUTOINCREMENT,
            statement_schema_version    INTEGER NOT NULL,
            statement_key               TEXT NOT NULL UNIQUE,
            content_hash                TEXT NOT NULL,
            canonical_content_json       TEXT NOT NULL,
            venue                       TEXT NOT NULL,
            account_id                  TEXT NOT NULL,
            trading_mode                TEXT NOT NULL,
            statement_source            TEXT NOT NULL,
            statement_type              TEXT NOT NULL,
            exchange_transaction_id     TEXT NOT NULL,
            event_time                  TEXT NOT NULL,
            event_time_ms               INTEGER NOT NULL,
            recorded_at                 TEXT NOT NULL,
            symbol                      TEXT NOT NULL DEFAULT '',
            asset                       TEXT NOT NULL,
            amount                      TEXT NOT NULL,
            order_id                    TEXT NOT NULL DEFAULT '',
            trade_id                    TEXT NOT NULL DEFAULT '',
            reconciliation_status       TEXT NOT NULL,
            ledger_event_key            TEXT NOT NULL DEFAULT '',
            raw_payload_json             TEXT NOT NULL,
            UNIQUE(
                venue,
                account_id,
                statement_source,
                statement_type,
                exchange_transaction_id
            )
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS exchange_statement_cursors (
            source_key                  TEXT PRIMARY KEY,
            venue                       TEXT NOT NULL,
            account_id                  TEXT NOT NULL,
            statement_source            TEXT NOT NULL,
            event_time_ms               INTEGER NOT NULL,
            exchange_transaction_id     TEXT NOT NULL,
            updated_at                  TEXT NOT NULL,
            UNIQUE(venue, account_id, statement_source)
        )
        """
    )
    columns: set[str] = set()
    for row in conn.execute("PRAGMA table_info(exchange_statement_entries)").fetchall():
        columns.add(str(row["name"] if isinstance(row, sqlite3.Row) else row[1]))
    if "canonical_content_json" not in columns:
        conn.execute(
            """
            ALTER TABLE exchange_statement_entries
            ADD COLUMN canonical_content_json TEXT NOT NULL DEFAULT '{}'
            """
        )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_exchange_statement_scope_time
        ON exchange_statement_entries(
            venue,
            account_id,
            statement_source,
            event_time_ms,
            exchange_transaction_id
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_exchange_statement_reconciliation
        ON exchange_statement_entries(reconciliation_status, event_time_ms)
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_exchange_statement_no_update
        BEFORE UPDATE ON exchange_statement_entries
        BEGIN
            SELECT RAISE(ABORT, 'exchange statement journal is append-only');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_exchange_statement_no_delete
        BEFORE DELETE ON exchange_statement_entries
        BEGIN
            SELECT RAISE(ABORT, 'exchange statement journal is append-only');
        END
        """
    )


def _cursor_follows(
    candidate_time_ms: int,
    candidate_transaction_id: str,
    current_time_ms: int,
    current_transaction_id: str,
) -> bool:
    if candidate_time_ms != current_time_ms:
        return candidate_time_ms > current_time_ms
    if candidate_transaction_id.isdecimal() and current_transaction_id.isdecimal():
        return int(candidate_transaction_id) > int(current_transaction_id)
    return candidate_transaction_id > current_transaction_id


def _load_canonical_object(value: str, field_name: str) -> dict[str, Any]:
    def reject_constant(constant: str) -> None:
        raise ValueError(f"non-finite JSON constant {constant}")

    try:
        parsed = json.loads(value, parse_constant=reject_constant)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ExchangeStatementValidationError(
            f"{field_name} must contain canonical JSON"
        ) from exc
    if not isinstance(parsed, dict):
        raise ExchangeStatementValidationError(f"{field_name} must contain a JSON object")
    canonical = json.dumps(
        parsed,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    if canonical != value:
        raise ExchangeStatementValidationError(f"{field_name} is not canonical JSON")
    return parsed


def _validate_normalized_statement(statement: NormalizedExchangeStatement) -> None:
    if statement.statement_schema_version != EXCHANGE_STATEMENT_SCHEMA_VERSION:
        raise ExchangeStatementValidationError(
            f"unsupported statement schema version {statement.statement_schema_version}"
        )
    if statement.venue != _require_text(statement.venue, "venue", upper=True):
        raise ExchangeStatementValidationError("venue is not canonical")
    if statement.account_id != _require_text(statement.account_id, "account_id"):
        raise ExchangeStatementValidationError("account_id is not canonical")
    if statement.trading_mode != _require_text(
        statement.trading_mode,
        "trading_mode",
        lower=True,
    ):
        raise ExchangeStatementValidationError("trading_mode is not canonical")
    if statement.statement_source != _require_text(
        statement.statement_source,
        "statement_source",
        upper=True,
    ):
        raise ExchangeStatementValidationError("statement_source is not canonical")
    if statement.statement_type != _require_text(
        statement.statement_type,
        "statement_type",
        upper=True,
    ):
        raise ExchangeStatementValidationError("statement_type is not canonical")
    if statement.exchange_transaction_id != _transaction_id(
        statement.exchange_transaction_id,
        "exchange_transaction_id",
    ):
        raise ExchangeStatementValidationError("exchange_transaction_id is not canonical")
    if statement.event_time != _iso_from_timestamp_ms(statement.event_time_ms):
        raise ExchangeStatementValidationError("event_time does not match event_time_ms")
    if statement.symbol != _optional_text(statement.symbol, upper=True):
        raise ExchangeStatementValidationError("symbol is not canonical")
    if statement.asset != _require_text(statement.asset, "asset", upper=True):
        raise ExchangeStatementValidationError("asset is not canonical")
    if statement.amount != _canonical_decimal(statement.amount, "amount"):
        raise ExchangeStatementValidationError("amount is not canonical")
    if statement.order_id != _optional_text(statement.order_id):
        raise ExchangeStatementValidationError("order_id is not canonical")
    if statement.trade_id != _optional_text(statement.trade_id):
        raise ExchangeStatementValidationError("trade_id is not canonical")
    if statement.reconciliation_status not in STATEMENT_RECONCILIATION_STATUSES:
        raise ExchangeStatementValidationError("reconciliation_status is not supported")
    if (statement.reconciliation_status == LEDGERED) != (
        statement.economic_event is not None
    ):
        raise ExchangeStatementValidationError(
            "statement reconciliation status does not match its ledger event"
        )

    expected_key = _statement_identity_key(
        venue=statement.venue,
        account_id=statement.account_id,
        statement_source=statement.statement_source,
        statement_type=statement.statement_type,
        exchange_transaction_id=statement.exchange_transaction_id,
    )
    if statement.statement_key != expected_key:
        raise ExchangeStatementValidationError("statement_key does not match identity")

    canonical_content = _load_canonical_object(
        statement.canonical_content_json,
        "canonical_content_json",
    )
    extras_json = canonical_content.get("canonical_source_extras_json")
    if not isinstance(extras_json, str):
        raise ExchangeStatementValidationError(
            "canonical content is missing canonical_source_extras_json"
        )
    _load_canonical_object(extras_json, "canonical_source_extras_json")
    expected_content = {
        "statement_schema_version": statement.statement_schema_version,
        "venue": statement.venue,
        "account_id": statement.account_id,
        "trading_mode": statement.trading_mode,
        "statement_source": statement.statement_source,
        "statement_type": statement.statement_type,
        "exchange_transaction_id": statement.exchange_transaction_id,
        "event_time": statement.event_time,
        "event_time_ms": statement.event_time_ms,
        "symbol": statement.symbol,
        "asset": statement.asset,
        "amount": statement.amount,
        "order_id": statement.order_id,
        "trade_id": statement.trade_id,
        "reconciliation_status": statement.reconciliation_status,
        "canonical_source_extras_json": extras_json,
    }
    if canonical_content != expected_content:
        raise ExchangeStatementValidationError(
            "canonical content does not match normalized statement fields"
        )
    expected_hash = hashlib.sha256(
        statement.canonical_content_json.encode("utf-8")
    ).hexdigest()
    if statement.content_hash != expected_hash:
        raise ExchangeStatementValidationError("content_hash does not match canonical content")
    _load_canonical_object(statement.raw_payload_json, "raw_payload_json")


def ingest_exchange_statement(
    conn: sqlite3.Connection,
    statement: NormalizedExchangeStatement,
    *,
    cursor_conn: sqlite3.Connection | None = None,
) -> ExchangeStatementIngestionResult:
    """Append a statement, optional ledger event, and monotonic cursor.

    With one connection, all three records share the original atomic savepoint
    and the owning state-store controls the commit.  A split runtime supplies a
    distinct ``cursor_conn``: immutable statement/ledger evidence is then
    committed first and the hot-state cursor second.  If the process stops
    between those boundaries, replaying the same statement is idempotent and
    repairs the lagging cursor; the reverse (a cursor without evidence) cannot
    occur.
    """

    _validate_normalized_statement(statement)
    savepoint = "exchange_statement_ingest"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        existing = conn.execute(
            """
            SELECT content_hash
            FROM exchange_statement_entries
            WHERE statement_key = ?
            """,
            (statement.statement_key,),
        ).fetchone()
        if existing is not None and str(existing["content_hash"]) != statement.content_hash:
            raise ExchangeStatementIdempotencyConflict(
                f"exchange statement identity collision for key {statement.statement_key}"
            )

        if statement.economic_event is None:
            ledger_result = LedgerIngestionResult(0, 0, 0, ())
            ledger_event_key = ""
        else:
            ledger_result = ingest_economic_events(conn, (statement.economic_event,))
            if len(ledger_result.event_keys) != 1:
                raise ExchangeStatementValidationError(
                    "ledgered statement did not produce exactly one ledger identity"
                )
            ledger_event_key = ledger_result.event_keys[0]

        inserted = False
        if existing is None:
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO exchange_statement_entries (
                        statement_schema_version,
                        statement_key,
                        content_hash,
                        canonical_content_json,
                        venue,
                        account_id,
                        trading_mode,
                        statement_source,
                        statement_type,
                        exchange_transaction_id,
                        event_time,
                        event_time_ms,
                        recorded_at,
                        symbol,
                        asset,
                        amount,
                        order_id,
                        trade_id,
                        reconciliation_status,
                        ledger_event_key,
                        raw_payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        statement.statement_schema_version,
                        statement.statement_key,
                        statement.content_hash,
                        statement.canonical_content_json,
                        statement.venue,
                        statement.account_id,
                        statement.trading_mode,
                        statement.statement_source,
                        statement.statement_type,
                        statement.exchange_transaction_id,
                        statement.event_time,
                        statement.event_time_ms,
                        datetime.now(timezone.utc).isoformat(),
                        statement.symbol,
                        statement.asset,
                        statement.amount,
                        statement.order_id,
                        statement.trade_id,
                        statement.reconciliation_status,
                        ledger_event_key,
                        statement.raw_payload_json,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                composite_collision = conn.execute(
                    """
                    SELECT content_hash
                    FROM exchange_statement_entries
                    WHERE venue = ?
                      AND account_id = ?
                      AND statement_source = ?
                      AND statement_type = ?
                      AND exchange_transaction_id = ?
                    """,
                    (
                        statement.venue,
                        statement.account_id,
                        statement.statement_source,
                        statement.statement_type,
                        statement.exchange_transaction_id,
                    ),
                ).fetchone()
                if composite_collision is not None:
                    raise ExchangeStatementIdempotencyConflict(
                        "exchange statement composite identity collision"
                    ) from exc
                raise
            inserted = cursor.rowcount == 1

        def advance_cursor(connection: sqlite3.Connection) -> bool:
            source_key = statement_cursor_key(
                venue=statement.venue,
                account_id=statement.account_id,
                statement_source=statement.statement_source,
            )
            current_cursor = connection.execute(
            """
            SELECT event_time_ms, exchange_transaction_id
            FROM exchange_statement_cursors
            WHERE source_key = ?
            """,
            (source_key,),
            ).fetchone()
            advanced = current_cursor is None or _cursor_follows(
                statement.event_time_ms,
                statement.exchange_transaction_id,
                int(current_cursor["event_time_ms"])
                if current_cursor is not None
                else -1,
                str(current_cursor["exchange_transaction_id"])
                if current_cursor is not None
                else "",
            )
            if advanced:
                connection.execute(
                """
                INSERT INTO exchange_statement_cursors (
                    source_key,
                    venue,
                    account_id,
                    statement_source,
                    event_time_ms,
                    exchange_transaction_id,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_key) DO UPDATE SET
                    event_time_ms = excluded.event_time_ms,
                    exchange_transaction_id = excluded.exchange_transaction_id,
                    updated_at = excluded.updated_at
                """,
                (
                    source_key,
                    statement.venue,
                    statement.account_id,
                    statement.statement_source,
                    statement.event_time_ms,
                    statement.exchange_transaction_id,
                    datetime.now(timezone.utc).isoformat(),
                ),
                )
            return advanced

        if cursor_conn is None:
            cursor_advanced = advance_cursor(conn)
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        else:
            # The audit boundary must be durable before a state cursor can make
            # the evidence appear consumed.
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            conn.commit()
            cursor_savepoint = "exchange_statement_cursor_advance"
            cursor_conn.execute(f"SAVEPOINT {cursor_savepoint}")
            try:
                cursor_advanced = advance_cursor(cursor_conn)
                cursor_conn.execute(f"RELEASE SAVEPOINT {cursor_savepoint}")
                cursor_conn.commit()
            except Exception:
                cursor_conn.execute(f"ROLLBACK TO SAVEPOINT {cursor_savepoint}")
                cursor_conn.execute(f"RELEASE SAVEPOINT {cursor_savepoint}")
                raise

        return ExchangeStatementIngestionResult(
            statement_key=statement.statement_key,
            content_hash=statement.content_hash,
            inserted=inserted,
            duplicate=not inserted,
            reconciliation_status=statement.reconciliation_status,
            ledger_result=ledger_result,
            cursor_advanced=cursor_advanced,
        )
    except Exception:
        # A split ingest may already have released and committed the evidence
        # savepoint before a cursor failure.  Only roll back while it is active.
        try:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        except sqlite3.OperationalError as rollback_error:
            if "no such savepoint" not in str(rollback_error).lower():
                raise
        raise


def read_exchange_statement_entries(
    conn: sqlite3.Connection,
    *,
    venue: str | None = None,
    account_id: str | None = None,
    statement_source: str | None = None,
    statement_type: str | None = None,
    reconciliation_status: str | None = None,
    limit: int | None = 1000,
    descending: bool = False,
) -> list[dict[str, Any]]:
    conditions: list[str] = []
    params: list[Any] = []
    filters = (
        ("venue", venue, True),
        ("account_id", account_id, False),
        ("statement_source", statement_source, True),
        ("statement_type", statement_type, True),
        ("reconciliation_status", reconciliation_status, True),
    )
    for column, value, upper in filters:
        if value is None:
            continue
        normalized = _require_text(value, column, upper=upper)
        conditions.append(f"{column} = ?")
        params.append(normalized)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    order = "DESC" if descending else "ASC"
    sql = (
        "SELECT * FROM exchange_statement_entries "
        f"{where} ORDER BY event_time_ms {order}, id {order}"
    )
    if limit is not None:
        if isinstance(limit, bool) or limit < 0:
            raise ExchangeStatementValidationError("limit must be non-negative or None")
        sql += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(sql, tuple(params)).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["raw_payload"] = json.loads(str(item.pop("raw_payload_json")))
        item["canonical_content"] = json.loads(
            str(item.pop("canonical_content_json"))
        )
        result.append(item)
    return result


def read_exchange_statement_cursor(
    conn: sqlite3.Connection,
    *,
    venue: str,
    account_id: str,
    statement_source: str,
) -> dict[str, Any] | None:
    source_key = statement_cursor_key(
        venue=venue,
        account_id=account_id,
        statement_source=statement_source,
    )
    row = conn.execute(
        "SELECT * FROM exchange_statement_cursors WHERE source_key = ?",
        (source_key,),
    ).fetchone()
    return None if row is None else dict(row)
