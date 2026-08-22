import sqlite3
from dataclasses import replace
from decimal import Decimal

import pytest

from bongus.engine.economic_ledger import (
    BALANCE_ADJUSTMENT,
    BORROW_INTEREST,
    COMMISSION,
    FUNDING,
    REALIZED_PNL,
)
from bongus.engine.exchange_statements import (
    BINANCE_FUTURES_INCOME,
    BINANCE_MARGIN_INTEREST,
    LEDGERED,
    MATCH_REQUIRED,
    UNMAPPED,
    ExchangeStatementIdempotencyConflict,
    ExchangeStatementValidationError,
    normalize_binance_futures_income,
    normalize_binance_margin_interest,
)
from bongus.engine.state_store import CURRENT_SCHEMA_VERSION, StateReader, StateWriter


ACCOUNT_ID = "binance-testnet-main"
TRADING_MODE = "testnet"
STRATEGY_ID = "funding-arbitrage-v2"
VENUE = "BINANCE"


@pytest.fixture
def statement_store(tmp_path):
    db_path = str(tmp_path / "statements.db")
    writer = StateWriter(db_path=db_path)
    reader = StateReader(db_path=db_path)
    try:
        yield writer, reader
    finally:
        reader.close()
        writer.close()


def _context() -> dict[str, str]:
    return {
        "account_id": ACCOUNT_ID,
        "trading_mode": TRADING_MODE,
        "strategy_id": STRATEGY_ID,
        "venue": VENUE,
        "runtime_mode": "SAFE_MODE",
        "session_id": "statement-test-session",
    }


def _funding_row(**overrides):
    row = {
        "symbol": "BTCUSDT",
        "incomeType": "FUNDING_FEE",
        "income": "1.2500",
        "asset": "USDT",
        "info": "funding fee",
        "time": 1_767_225_600_123,
        "tranId": 12345,
        "tradeId": "",
    }
    row.update(overrides)
    return row


def _margin_interest_row(**overrides):
    row = {
        "txId": 777,
        "interestAccuredTime": 1_767_225_601_456,
        "asset": "USDT",
        "rawAsset": "USDT",
        "principal": "100.00000000",
        "interest": "0.0012300",
        "interestRate": "0.0000123",
        "type": "PERIODIC",
        "isolatedSymbol": "BTCUSDT",
    }
    row.update(overrides)
    return row


def test_funding_statement_is_durable_decimal_exact_and_restart_idempotent(
    statement_store,
):
    writer, reader = statement_store
    first = writer.record_binance_futures_income_statement(
        _funding_row(),
        **_context(),
    )
    replay_payload = dict(reversed(list(_funding_row(income="1.25").items())))
    replay = writer.record_binance_futures_income_statement(
        replay_payload,
        **_context(),
    )

    assert first.inserted is True
    assert first.duplicate is False
    assert first.reconciliation_status == LEDGERED
    assert first.ledger_result.inserted == 1
    assert replay.inserted is False
    assert replay.duplicate is True
    assert replay.ledger_result.duplicates == 1
    assert first.statement_key == replay.statement_key
    assert first.content_hash == replay.content_hash

    statements = reader.get_exchange_statement_entries(account_id=ACCOUNT_ID)
    assert len(statements) == 1
    assert statements[0]["amount"] == "1.25"
    assert statements[0]["statement_type"] == "FUNDING_FEE"
    assert statements[0]["raw_payload"]["income"] == "1.2500"

    events = reader.get_economic_ledger_events(
        account_id=ACCOUNT_ID,
        trading_mode=TRADING_MODE,
        venue=VENUE,
        strategy_id=STRATEGY_ID,
    )
    assert len(events) == 1
    assert events[0]["event_type"] == FUNDING
    assert events[0]["amount"] == "1.25"
    assert events[0]["exchange_event_id"] == (
        "binance:binance-testnet-main:funding:12345"
    )
    assert (
        writer.conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()[0]
        == str(CURRENT_SCHEMA_VERSION)
    )


def test_funding_statement_preserves_existing_strategy_cycle_lineage(statement_store):
    writer, reader = statement_store

    result = writer.record_binance_futures_income_statement(
        _funding_row(),
        **_context(),
        cycle_id="cycle-42",
        intent_id="entry-42",
    )

    assert result.ledger_result.inserted == 1
    event = reader.get_economic_ledger_events(account_id=ACCOUNT_ID)[0]
    assert event["event_type"] == FUNDING
    assert event["cycle_id"] == "cycle-42"
    assert event["intent_id"] == "entry-42"

    replay = writer.record_binance_futures_income_statement(
        _funding_row(),
        **_context(),
    )
    assert replay.duplicate is True
    assert replay.ledger_result.duplicates == 1
    replayed_event = reader.get_economic_ledger_events(account_id=ACCOUNT_ID)[0]
    assert replayed_event["cycle_id"] == "cycle-42"
    assert replayed_event["intent_id"] == "entry-42"


def test_historical_blank_funding_lineage_is_not_relabelled_on_replay(statement_store):
    writer, reader = statement_store
    writer.record_binance_futures_income_statement(_funding_row(), **_context())

    replay = writer.record_binance_futures_income_statement(
        _funding_row(),
        **_context(),
        cycle_id="later-cycle",
        intent_id="later-entry",
    )

    assert replay.duplicate is True
    event = reader.get_economic_ledger_events(account_id=ACCOUNT_ID)[0]
    assert event["cycle_id"] == ""
    assert event["intent_id"] == ""


def test_funding_statement_rejects_partial_or_nonfunding_cycle_lineage(statement_store):
    writer, reader = statement_store

    with pytest.raises(ValueError, match="both cycle_id and intent_id"):
        writer.record_binance_futures_income_statement(
            _funding_row(),
            **_context(),
            cycle_id="cycle-42",
        )
    with pytest.raises(ValueError, match="valid only for FUNDING_FEE"):
        writer.record_binance_futures_income_statement(
            _funding_row(incomeType="TRANSFER", symbol=""),
            **_context(),
            cycle_id="cycle-42",
            intent_id="entry-42",
        )
    with pytest.raises(ValueError, match="valid only for FUNDING_FEE"):
        writer.record_binance_futures_income_statement(
            _funding_row(
                incomeType="COMMISSION",
                income="-0.20",
                tradeId=5001,
            ),
            **_context(),
            cycle_id="cycle-42",
            intent_id="entry-42",
        )
    assert reader.get_exchange_statement_entries(account_id=ACCOUNT_ID) == []
    assert reader.get_economic_ledger_events(account_id=ACCOUNT_ID) == []


def test_statement_content_collision_rolls_back_ledger_and_cursor(statement_store):
    writer, reader = statement_store
    writer.record_binance_futures_income_statement(_funding_row(), **_context())
    original_cursor = reader.get_exchange_statement_cursor(
        venue=VENUE,
        account_id=ACCOUNT_ID,
        statement_source=BINANCE_FUTURES_INCOME,
    )

    with pytest.raises(ExchangeStatementIdempotencyConflict):
        writer.record_binance_futures_income_statement(
            _funding_row(income="2.50", time=1_767_225_999_999),
            **_context(),
        )

    statements = reader.get_exchange_statement_entries(account_id=ACCOUNT_ID)
    events = reader.get_economic_ledger_events(account_id=ACCOUNT_ID)
    assert len(statements) == 1
    assert statements[0]["amount"] == "1.25"
    assert len(events) == 1
    assert events[0]["amount"] == "1.25"
    assert reader.get_exchange_statement_cursor(
        venue=VENUE,
        account_id=ACCOUNT_ID,
        statement_source=BINANCE_FUTURES_INCOME,
    ) == original_cursor


def test_statement_funding_identity_matches_existing_funding_ingestion(statement_store):
    writer, reader = statement_store
    row = _funding_row()
    normalized = normalize_binance_futures_income(row, **_context())
    source_id = "binance:binance-testnet-main:funding:12345"
    writer.record_economic_funding(
        account_id=ACCOUNT_ID,
        trading_mode=TRADING_MODE,
        venue=VENUE,
        strategy_id=STRATEGY_ID,
        event_time=normalized.event_time,
        asset="USDT",
        amount="1.25",
        exchange_event_id=source_id,
        source_event_id=source_id,
        symbol="BTCUSDT",
        instrument_type="PERPETUAL",
        runtime_mode="LIVE",
        session_id="earlier-session",
        metadata={"income_type": "FUNDING_FEE"},
        raw_payload={"first_delivery": True},
    )
    writer.flush()

    result = writer.record_exchange_statement(normalized)

    assert result.inserted is True
    assert result.ledger_result.inserted == 0
    assert result.ledger_result.duplicates == 1
    assert len(reader.get_economic_ledger_events(account_id=ACCOUNT_ID)) == 1


def test_forged_normalized_content_is_rejected_before_database_write(statement_store):
    writer, reader = statement_store
    normalized = normalize_binance_futures_income(_funding_row(), **_context())

    with pytest.raises(ExchangeStatementValidationError, match="content_hash"):
        writer.record_exchange_statement(
            replace(normalized, content_hash="0" * 64)
        )

    assert reader.get_exchange_statement_entries(account_id=ACCOUNT_ID) == []
    assert reader.get_economic_ledger_events(account_id=ACCOUNT_ID) == []


def test_statement_insert_failure_rolls_back_optional_economic_event(statement_store):
    writer, reader = statement_store
    writer._statement_conn.execute(
        """
        CREATE TRIGGER reject_blocked_statement
        BEFORE INSERT ON exchange_statement_entries
        WHEN NEW.exchange_transaction_id = 'blocked-transaction'
        BEGIN
            SELECT RAISE(ABORT, 'injected statement failure');
        END
        """
    )
    writer._statement_conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="injected statement failure"):
        writer.record_binance_futures_income_statement(
            _funding_row(tranId="blocked-transaction"),
            **_context(),
        )

    assert reader.get_exchange_statement_entries(account_id=ACCOUNT_ID) == []
    assert reader.get_economic_ledger_events(account_id=ACCOUNT_ID) == []
    assert (
        reader.get_exchange_statement_cursor(
            venue=VENUE,
            account_id=ACCOUNT_ID,
            statement_source=BINANCE_FUTURES_INCOME,
        )
        is None
    )


def test_transfer_changes_balance_but_is_not_strategy_pnl(statement_store):
    writer, reader = statement_store
    result = writer.record_binance_futures_income_statement(
        _funding_row(
            symbol="",
            incomeType="TRANSFER",
            income="-50.5000",
            tranId=200,
        ),
        **_context(),
    )

    assert result.reconciliation_status == LEDGERED
    event = reader.get_economic_ledger_events(account_id=ACCOUNT_ID)[0]
    assert event["event_type"] == BALANCE_ADJUSTMENT
    assert event["amount"] == "-50.5"
    projection = reader.project_economic_ledger(account_id=ACCOUNT_ID)
    assert projection.balance_deltas == {"USDT": Decimal("-50.5")}
    assert projection.economic_effect_usd_by_type == {}
    assert projection.total_economic_effect_usd == Decimal("0")


def test_trade_derived_statement_rows_are_evidence_only_and_not_double_counted(
    statement_store,
):
    writer, reader = statement_store
    writer.record_economic_fill(
        account_id=ACCOUNT_ID,
        trading_mode=TRADING_MODE,
        venue=VENUE,
        strategy_id=STRATEGY_ID,
        event_time="2026-01-01T00:00:00Z",
        symbol="ETHUSDT",
        instrument_type="PERPETUAL",
        side="SELL",
        quantity="1",
        price="2000",
        quantity_asset="ETH",
        quote_asset="USDT",
        exchange_fill_id="fill-commission-realized",
        order_id="order-1",
        commission_amount="0.20",
        commission_asset="USDT",
        realized_pnl_amount="4.50",
        realized_pnl_asset="USDT",
    )
    writer.flush()

    commission = writer.record_binance_futures_income_statement(
        _funding_row(
            symbol="ETHUSDT",
            incomeType="COMMISSION",
            income="-0.20",
            tranId=301,
            tradeId=5001,
        ),
        **_context(),
    )
    realized = writer.record_binance_futures_income_statement(
        _funding_row(
            symbol="ETHUSDT",
            incomeType="REALIZED_PNL",
            income="4.50",
            tranId=302,
            tradeId=5001,
        ),
        **_context(),
    )
    unknown = writer.record_binance_futures_income_statement(
        _funding_row(
            symbol="",
            incomeType="INSURANCE_CLEAR",
            income="-0.75",
            tranId=303,
        ),
        **_context(),
    )

    assert commission.reconciliation_status == MATCH_REQUIRED
    assert realized.reconciliation_status == MATCH_REQUIRED
    assert unknown.reconciliation_status == UNMAPPED
    assert commission.ledger_result.requested == 0
    assert realized.ledger_result.requested == 0
    assert unknown.ledger_result.requested == 0
    statements = reader.get_exchange_statement_entries(account_id=ACCOUNT_ID)
    assert [row["reconciliation_status"] for row in statements] == [
        MATCH_REQUIRED,
        MATCH_REQUIRED,
        UNMAPPED,
    ]

    events = reader.get_economic_ledger_events(account_id=ACCOUNT_ID)
    assert len(events) == 3
    assert [event["event_type"] for event in events] == [
        "FILL",
        COMMISSION,
        REALIZED_PNL,
    ]
    projection = reader.project_economic_ledger(account_id=ACCOUNT_ID)
    assert projection.economic_effect_usd_by_type == {
        COMMISSION: Decimal("-0.2"),
        REALIZED_PNL: Decimal("4.5"),
    }
    assert projection.total_economic_effect_usd == Decimal("4.3")


def test_margin_interest_has_exact_negative_charge_semantics(statement_store):
    writer, reader = statement_store
    result = writer.record_binance_margin_interest_statement(
        _margin_interest_row(),
        **_context(),
    )

    assert result.reconciliation_status == LEDGERED
    statement = reader.get_exchange_statement_entries(
        statement_source=BINANCE_MARGIN_INTEREST
    )[0]
    assert statement["amount"] == "0.00123"
    event = reader.get_economic_ledger_events(account_id=ACCOUNT_ID)[0]
    assert event["event_type"] == BORROW_INTEREST
    assert event["amount"] == "-0.00123"
    assert event["amount_usd"] == "-0.00123"
    projection = reader.project_economic_ledger(account_id=ACCOUNT_ID)
    assert projection.total_economic_effect_usd == Decimal("-0.00123")


def test_cursor_never_rewinds_and_uses_numeric_transaction_order(statement_store):
    writer, reader = statement_store

    def record(transaction_id: int, event_time_ms: int) -> None:
        writer.record_binance_futures_income_statement(
            _funding_row(
                symbol="",
                incomeType="WELCOME_BONUS",
                income="1",
                tranId=transaction_id,
                time=event_time_ms,
            ),
            **_context(),
        )

    record(9, 2_000)
    record(999, 1_000)
    cursor = reader.get_exchange_statement_cursor(
        venue=VENUE,
        account_id=ACCOUNT_ID,
        statement_source=BINANCE_FUTURES_INCOME,
    )
    assert cursor is not None
    assert cursor["event_time_ms"] == 2_000
    assert cursor["exchange_transaction_id"] == "9"

    record(11, 2_000)
    record(10, 2_000)
    cursor = reader.get_exchange_statement_cursor(
        venue=VENUE,
        account_id=ACCOUNT_ID,
        statement_source=BINANCE_FUTURES_INCOME,
    )
    assert cursor is not None
    assert cursor["event_time_ms"] == 2_000
    assert cursor["exchange_transaction_id"] == "11"


@pytest.mark.parametrize(
    ("normalizer", "payload", "expected_message"),
    [
        (normalize_binance_futures_income, _funding_row(tranId=None), "tranId"),
        (normalize_binance_futures_income, _funding_row(income="NaN"), "finite"),
        (normalize_binance_futures_income, _funding_row(symbol=""), "symbol"),
        (normalize_binance_futures_income, _funding_row(time=True), "time"),
        (
            normalize_binance_margin_interest,
            _margin_interest_row(interest="-0.01"),
            "positive charge",
        ),
        (
            normalize_binance_margin_interest,
            _margin_interest_row(txId=1.5),
            "string or integer",
        ),
    ],
)
def test_statement_normalization_fails_closed(normalizer, payload, expected_message):
    with pytest.raises(ExchangeStatementValidationError, match=expected_message):
        normalizer(payload, **_context())


def test_memory_statement_store_shares_authoritative_connection_and_closes_once():
    writer = StateWriter(":memory:")
    assert writer._statement_conn is writer.conn
    result = writer.record_binance_futures_income_statement(
        _funding_row(
            symbol="",
            incomeType="WELCOME_BONUS",
            income="1",
            tranId=987,
        ),
        **_context(),
    )
    assert result.inserted is True
    assert (
        writer.conn.execute(
            "SELECT COUNT(*) FROM exchange_statement_entries"
        ).fetchone()[0]
        == 1
    )
    # The statement subsystem does not own the shared connection; StateWriter
    # closes the authoritative in-memory database exactly once.
    writer.close()
