import sqlite3
from decimal import Decimal
from typing import Any, TypedDict

import pytest

from bongus.engine.economic_ledger import (
    COMMISSION,
    DEPOSIT,
    FUNDING,
    INTERNAL_TRANSFER,
    REALIZED_PNL,
    RECONCILIATION_ADJUSTMENT,
    STABLECOIN_CONVERSION,
    WITHDRAWAL,
    EconomicLedgerEvent,
    LedgerIdempotencyConflict,
    LedgerValidationError,
    build_cashflow_event,
    build_commission_event,
)
from bongus.engine.state_store import CURRENT_SCHEMA_VERSION, StateReader, StateWriter


@pytest.fixture
def ledger_store(tmp_path):
    db_path = str(tmp_path / "ledger.db")
    writer = StateWriter(db_path=db_path)
    reader = StateReader(db_path=db_path)
    try:
        yield writer, reader
    finally:
        reader.close()
        writer.close()


class _Scope(TypedDict):
    account_id: str
    trading_mode: str
    venue: str
    strategy_id: str


def _scope() -> _Scope:
    return {
        "account_id": "binance-testnet-main",
        "trading_mode": "testnet",
        "venue": "BINANCE",
        "strategy_id": "funding-arb-v2",
    }


def test_execution_and_economic_fill_are_atomic_and_restart_idempotent(ledger_store):
    writer, reader = ledger_store
    payload = {
        "symbol": "BTCUSDT",
        "client_order_id": "bngs_s_abc",
        "status": "PARTIALLY_FILLED",
        "filled_qty": 0.1,
        "cumulative_filled_qty": 0.1,
        "avg_fill_price": 60_000,
        "last_fill_price": 60_000,
        "execution_type": "TRADE",
        "market": "spot",
        "side": "BUY",
        "order_id": 99,
        "trade_id": 101,
        "account_id": "binance-testnet-main",
        "strategy_id": "funding-arb-v2",
        "cycle_id": "cycle-atomic",
        "intent_id": "intent-atomic",
        "leg_id": "spot-atomic",
        "config_version_hash": "cfg-a",
        "event_time": "2026-01-01T00:00:00Z",
    }
    economic = {
        **_scope(),
        "event_time": "2026-01-01T00:00:00Z",
        "symbol": "BTCUSDT",
        "instrument_type": "SPOT",
        "side": "BUY",
        "quantity": "0.1",
        "price": "60000",
        "quantity_asset": "BTC",
        "quote_asset": "USDT",
        "exchange_fill_id": "spot:BTCUSDT:101",
        "source_event_id": "spot:BTCUSDT:101",
        "order_id": "99",
        "client_order_id": "bngs_s_abc",
        "cycle_id": "cycle-atomic",
        "intent_id": "intent-atomic",
        "runtime_mode": "LIVE",
        "session_id": "run-one",
    }

    first = writer.record_execution_and_economic_fill(payload, economic)
    replay = writer.record_execution_and_economic_fill(
        {**payload, "event_time": "2026-01-01T00:00:01Z"},
        {**economic, "runtime_mode": "SAFE_MODE", "session_id": "run-two"},
    )

    assert first.inserted == 1
    assert replay.duplicates == 1
    raw = reader.get_execution_events(limit=10, scope_current=False)
    assert len(raw) == 2
    assert raw[0]["cumulative_filled_qty"] == pytest.approx(0.1)
    assert raw[0]["trade_id"] == "101"
    assert raw[0]["leg_id"] == "spot-atomic"
    assert len(reader.get_economic_ledger_events(**_scope())) == 1

    with pytest.raises(LedgerIdempotencyConflict):
        writer.record_execution_and_economic_fill(
            {**payload, "filled_qty": 0.2},
            {**economic, "quantity": "0.2"},
        )
    assert len(reader.get_execution_events(limit=10, scope_current=False)) == 2


def test_fill_and_commission_replay_is_exactly_idempotent(ledger_store):
    writer, reader = ledger_store
    fill = {
        **_scope(),
        "event_time": "2026-01-01T00:00:00Z",
        "symbol": "BTCUSDT",
        "instrument_type": "spot",
        "side": "buy",
        "quantity": "0.01000000",
        "price": "65000.00",
        "quantity_asset": "BTC",
        "quote_asset": "USDT",
        "exchange_fill_id": "trade-100",
        "order_id": "order-10",
        "client_order_id": "bongus-entry-10",
        "cycle_id": "cycle-1",
        "intent_id": "intent-1",
        "commission_amount": "0.2500",
        "commission_asset": "USDT",
        "metadata": {"maker": True},
        "raw_payload": {"first_delivery": True},
    }

    first = writer.record_economic_fill(**fill)
    replay = writer.record_economic_fill(
        **{
            **fill,
            "event_time": "2026-01-01T00:00:00+00:00",
            "quantity": "0.01",
            "price": "65000",
            "commission_amount": "0.25",
            # Raw delivery metadata is deliberately not part of economic identity.
            "raw_payload": {"first_delivery": False, "replayed": True},
        }
    )

    assert first.requested == 2
    assert first.inserted == 2
    assert first.duplicates == 0
    assert replay.inserted == 0
    assert replay.duplicates == 2
    assert first.event_keys == replay.event_keys

    events = reader.get_economic_ledger_events(**_scope())
    assert len(events) == 2
    assert events[0]["quantity"] == "0.01"
    assert events[0]["amount"] == "-650"
    assert events[1]["amount"] == "-0.25"

    projection = reader.project_economic_ledger(**_scope())
    assert projection.event_count == 2
    assert projection.fill_count == 1
    assert projection.spot_inventory_deltas == {"BTC": Decimal("0.01")}
    assert projection.balance_deltas == {
        "BTC": Decimal("0.01"),
        "USDT": Decimal("-650.25"),
    }
    assert projection.gross_fill_notional_usd == Decimal("650")
    assert projection.total_economic_effect_usd == Decimal("-0.25")


def test_identity_collision_rolls_back_the_entire_batch(ledger_store):
    writer, reader = ledger_store
    writer.record_economic_fill(
        **_scope(),
        event_time="2026-01-01T00:00:00Z",
        symbol="ETHUSDT",
        instrument_type="PERPETUAL",
        side="SELL",
        quantity="1",
        price="2000",
        quantity_asset="ETH",
        quote_asset="USDT",
        exchange_fill_id="fill-existing",
        order_id="order-existing",
        commission_amount="0.10",
        commission_asset="USDT",
    )
    new_funding = build_cashflow_event(
        event_type=FUNDING,
        **_scope(),
        event_time="2026-01-01T08:00:00Z",
        symbol="ETHUSDT",
        instrument_type="PERPETUAL",
        asset="USDT",
        amount="2.5",
        exchange_event_id="funding-new",
    )
    conflicting_commission = build_commission_event(
        **_scope(),
        event_time="2026-01-01T00:00:00Z",
        symbol="ETHUSDT",
        instrument_type="PERPETUAL",
        commission_amount="0.20",
        commission_asset="USDT",
        exchange_fill_id="fill-existing",
        order_id="order-existing",
    )

    with pytest.raises(LedgerIdempotencyConflict):
        writer.record_economic_events((new_funding, conflicting_commission))

    events = reader.get_economic_ledger_events(**_scope())
    assert len(events) == 2
    assert all(event["exchange_event_id"] != "funding-new" for event in events)


def test_partial_and_final_fills_keep_each_incremental_commission_once(ledger_store):
    writer, reader = ledger_store
    common = {
        **_scope(),
        "symbol": "SOLUSDT",
        "instrument_type": "PERPETUAL",
        "side": "BUY",
        "price": "100",
        "quantity_asset": "SOL",
        "quote_asset": "USDT",
        "order_id": "order-partial-final",
        "client_order_id": "bongus-order-partial-final",
        "cycle_id": "cycle-partial-final",
        "intent_id": "intent-partial-final",
    }
    writer.record_economic_fill(
        **common,
        event_time="2026-01-01T00:00:01Z",
        exchange_fill_id="fill-partial",
        quantity="0.4",
        commission_amount="0.20",
        commission_asset="USDT",
        metadata={"order_status": "PARTIALLY_FILLED"},
    )
    terminal_fields = {
        **common,
        "event_time": "2026-01-01T00:00:02Z",
        "exchange_fill_id": "fill-terminal",
        "quantity": "0.6",
        "commission_amount": "0.30",
        "commission_asset": "USDT",
        "metadata": {"order_status": "FILLED"},
    }
    writer.record_economic_fill(**terminal_fields)
    terminal_replay = writer.record_economic_fill(**terminal_fields)

    assert terminal_replay.inserted == 0
    assert terminal_replay.duplicates == 2
    projection = reader.project_economic_ledger(**_scope(), cycle_id="cycle-partial-final")
    assert projection.event_count == 4
    assert projection.fill_count == 2
    assert projection.perpetual_position_deltas == {"SOLUSDT": Decimal("1")}
    assert projection.amounts_by_type_and_asset[COMMISSION] == {"USDT": Decimal("-0.5")}
    assert projection.balance_deltas == {"USDT": Decimal("-0.5")}
    assert projection.total_economic_effect_usd == Decimal("-0.5")


def test_perpetual_fill_commission_and_realized_pnl_are_one_atomic_batch(ledger_store):
    writer, reader = ledger_store
    fields = {
        **_scope(),
        "event_time": "2026-01-01T00:00:00Z",
        "symbol": "BTCUSDT",
        "instrument_type": "PERPETUAL",
        "side": "SELL",
        "quantity": "0.1",
        "price": "60000",
        "quantity_asset": "BTC",
        "quote_asset": "USDT",
        "exchange_fill_id": "perp-realized-1",
        "order_id": "perp-order-1",
        "commission_amount": "0.25",
        "commission_asset": "USDT",
        "realized_pnl_amount": "3.50",
        "realized_pnl_asset": "USDT",
    }

    first = writer.record_economic_fill(**fields)
    replay = writer.record_economic_fill(**fields)

    assert first.requested == 3
    assert first.inserted == 3
    assert replay.duplicates == 3
    projection = reader.project_economic_ledger(**_scope())
    assert projection.perpetual_position_deltas == {"BTCUSDT": Decimal("-0.1")}
    assert projection.balance_deltas == {"USDT": Decimal("3.25")}
    assert projection.economic_effect_usd_by_type == {
        COMMISSION: Decimal("-0.25"),
        REALIZED_PNL: Decimal("3.5"),
    }
    assert projection.total_economic_effect_usd == Decimal("3.25")


def test_commissions_in_multiple_assets_are_preserved_and_valued(ledger_store):
    writer, reader = ledger_store
    common = {
        **_scope(),
        "event_time": "2026-01-01T00:00:00Z",
        "symbol": "BTCUSDT",
        "instrument_type": "PERPETUAL",
        "side": "SELL",
        "quantity": "0.01",
        "price": "60000",
        "quantity_asset": "BTC",
        "quote_asset": "USDT",
    }
    writer.record_economic_fill(
        **common,
        exchange_fill_id="fill-usdt-fee",
        order_id="order-1",
        commission_amount="0.10",
        commission_asset="USDT",
    )
    writer.record_economic_fill(
        **common,
        exchange_fill_id="fill-bnb-fee",
        order_id="order-2",
        commission_amount="0.01",
        commission_asset="BNB",
        commission_amount_usd="6",
    )

    projection = reader.project_economic_ledger(**_scope())
    assert projection.amounts_by_type_and_asset[COMMISSION] == {
        "BNB": Decimal("-0.01"),
        "USDT": Decimal("-0.1"),
    }
    assert projection.balance_deltas == {
        "BNB": Decimal("-0.01"),
        "USDT": Decimal("-0.1"),
    }
    assert projection.economic_effect_usd_by_type[COMMISSION] == Decimal("-6.1")
    assert projection.unvalued_economic_event_count == 0


def test_funding_borrow_and_balance_adjustments_have_distinct_pnl_semantics(ledger_store):
    writer, reader = ledger_store
    funding_fields = {
        **_scope(),
        "event_time": "2026-01-01T08:00:00Z",
        "symbol": "BTCUSDT",
        "instrument_type": "PERPETUAL",
        "asset": "USDT",
        "amount": "5",
        "exchange_event_id": "funding-1",
    }
    funding_first = writer.record_economic_funding(**funding_fields)
    funding_replay = writer.record_economic_funding(**funding_fields)
    writer.record_economic_borrow_interest(
        **_scope(),
        event_time="2026-01-01T09:00:00Z",
        asset="BTC",
        amount="0.002",
        amount_usd="120",
        exchange_event_id="interest-1",
    )
    writer.record_economic_balance_adjustment(
        **_scope(),
        event_time="2026-01-01T10:00:00Z",
        asset="USDT",
        amount="100",
        exchange_event_id="deposit-1",
        metadata={"reason": "deposit"},
    )

    assert funding_first.inserted == 1
    assert funding_replay.duplicates == 1
    projection = reader.project_economic_ledger(**_scope())
    assert projection.balance_deltas == {
        "BTC": Decimal("-0.002"),
        "USDT": Decimal("105"),
    }
    assert projection.economic_effect_usd_by_type == {
        "BORROW_INTEREST": Decimal("-120"),
        "FUNDING": Decimal("5"),
    }
    # The deposit reconciles cash but is not strategy profit.
    assert projection.total_economic_effect_usd == Decimal("-115")


def test_reconciliation_reports_exact_differences_and_tolerances(ledger_store):
    writer, reader = ledger_store
    writer.record_economic_fill(
        **_scope(),
        event_time="2026-01-01T00:00:00Z",
        symbol="BTCUSDT",
        instrument_type="SPOT",
        side="BUY",
        quantity="0.1",
        price="100",
        quantity_asset="BTC",
        quote_asset="USDT",
        exchange_fill_id="reconcile-fill",
        order_id="reconcile-order",
        commission_amount="0.01",
        commission_asset="USDT",
    )
    writer.record_economic_funding(
        **_scope(),
        event_time="2026-01-01T08:00:00Z",
        symbol="BTCUSDT",
        instrument_type="PERPETUAL",
        asset="USDT",
        amount="1",
        exchange_event_id="reconcile-funding",
    )

    mismatch = reader.reconcile_economic_ledger(
        **_scope(),
        opening_balances={"BTC": "1", "USDT": "1000"},
        exchange_balances={"BTC": "1.10000001", "USDT": "990.49"},
        tolerances={"BTC": "0.0000001", "USDT": "0.01"},
    )
    assert mismatch.matched is False
    assert mismatch.expected_balances == {
        "BTC": Decimal("1.1"),
        "USDT": Decimal("990.99"),
    }
    assert mismatch.differences == {
        "BTC": Decimal("0.00000001"),
        "USDT": Decimal("-0.50"),
    }
    assert mismatch.unexplained_assets == ("USDT",)

    matched = reader.reconcile_economic_ledger(
        **_scope(),
        opening_balances={"BTC": "1", "USDT": "1000"},
        exchange_balances={"BTC": "1.10000001", "USDT": "990.99"},
        tolerances={"BTC": "0.0000001", "USDT": "0.01"},
    )
    assert matched.matched is True
    assert matched.unexplained_assets == ()


def test_ledger_rows_are_database_immutable(ledger_store):
    writer, _ = ledger_store
    writer.record_economic_funding(
        **_scope(),
        event_time="2026-01-01T08:00:00Z",
        symbol="BTCUSDT",
        instrument_type="PERPETUAL",
        asset="USDT",
        amount="1",
        exchange_event_id="immutable-funding",
    )

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        writer.conn.execute("UPDATE economic_ledger_events SET amount = '2'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        writer.conn.execute("DELETE FROM economic_ledger_events")


def test_schema_migration_adds_ledger_without_changing_legacy_tables(tmp_path):
    db_path = str(tmp_path / "legacy.db")
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE portfolio_stats (key TEXT PRIMARY KEY, value REAL, updated_at TEXT)")
    connection.execute("INSERT INTO portfolio_stats(key, value, updated_at) VALUES ('equity', 123, '2026-01-01')")
    connection.commit()
    connection.close()

    writer = StateWriter(db_path=db_path)
    reader = StateReader(db_path=db_path)
    try:
        assert reader.get_stats()["equity"] == 123
        columns = {row["name"] for row in reader.conn.execute("PRAGMA table_info(economic_ledger_events)").fetchall()}
        assert {
            "ledger_schema_version",
            "event_key",
            "account_id",
            "venue",
            "strategy_id",
            "cycle_id",
            "intent_id",
            "order_id",
            "exchange_event_id",
            "exchange_fill_id",
            "availability_time",
            "code_hash",
            "config_hash",
            "schema_hash",
        } <= columns
        version = reader.conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()["value"]
        assert version == str(CURRENT_SCHEMA_VERSION)
    finally:
        reader.close()
        writer.close()


def test_explicit_non_pnl_cashflows_and_provenance_envelope_are_preserved(
    ledger_store,
) -> None:
    writer, reader = ledger_store
    provenance: dict[str, Any] = {
        "availability_time": "2026-01-01T00:00:01Z",
        "code_hash": "code-sha256",
        "config_hash": "config-sha256",
        "schema_hash": "schema-sha256",
    }
    events = [
        build_cashflow_event(
            event_type=DEPOSIT,
            **_scope(),
            event_time="2026-01-01T00:00:00Z",
            asset="USDT",
            amount="100",
            exchange_event_id="deposit-100",
            **provenance,
        ),
        build_cashflow_event(
            event_type=WITHDRAWAL,
            **_scope(),
            event_time="2026-01-01T00:00:00Z",
            asset="USDT",
            amount="-10",
            exchange_event_id="withdrawal-10",
            **provenance,
        ),
        build_cashflow_event(
            event_type=INTERNAL_TRANSFER,
            **_scope(),
            event_time="2026-01-01T00:00:00Z",
            asset="USDT",
            amount="-25",
            exchange_event_id="transfer-25",
            **provenance,
        ),
        build_cashflow_event(
            event_type=STABLECOIN_CONVERSION,
            **_scope(),
            event_time="2026-01-01T00:00:00Z",
            asset="USDT",
            amount="-5",
            exchange_event_id="conversion-5",
            **provenance,
        ),
        build_cashflow_event(
            event_type=STABLECOIN_CONVERSION,
            **_scope(),
            event_time="2026-01-01T00:00:00Z",
            asset="USDC",
            amount="4.99",
            exchange_event_id="conversion-5",
            **provenance,
        ),
        build_cashflow_event(
            event_type=RECONCILIATION_ADJUSTMENT,
            **_scope(),
            event_time="2026-01-01T00:00:00Z",
            asset="USDT",
            amount="0.01",
            exchange_event_id="reconciliation-1",
            **provenance,
        ),
    ]

    result = writer.record_economic_events(events)
    projection = reader.project_economic_ledger(**_scope())
    stored = reader.get_economic_ledger_events(**_scope())

    assert result.inserted == 6
    assert len(set(result.event_keys)) == 6
    assert {row["event_type"] for row in stored} == {
        DEPOSIT,
        WITHDRAWAL,
        INTERNAL_TRANSFER,
        STABLECOIN_CONVERSION,
        RECONCILIATION_ADJUSTMENT,
    }
    assert all(row["availability_time"] == "2026-01-01T00:00:01+00:00" for row in stored)
    assert all(row["code_hash"] == "code-sha256" for row in stored)
    assert projection.total_economic_effect_usd == Decimal("0")
    assert projection.incomplete_envelope_event_count == 0


def test_provenance_envelope_does_not_change_existing_economic_identity(
    ledger_store,
) -> None:
    writer, reader = ledger_store
    common = {
        **_scope(),
        "event_time": "2026-01-01T08:00:00Z",
        "symbol": "BTCUSDT",
        "instrument_type": "PERPETUAL",
        "asset": "USDT",
        "amount": "1",
        "exchange_event_id": "same-funding-identity",
    }
    first = writer.record_economic_events(
        (
            build_cashflow_event(
                event_type=FUNDING,
                **common,
                availability_time="2026-01-01T08:00:01Z",
                code_hash="code-a",
                config_hash="config-a",
                schema_hash="schema-a",
            ),
        )
    )
    replay = writer.record_economic_events((build_cashflow_event(event_type=FUNDING, **common),))

    assert first.event_keys == replay.event_keys
    assert replay.duplicates == 1
    assert reader.get_economic_ledger_events(**_scope())[0]["code_hash"] == "code-a"


def test_reconciliation_keeps_absent_components_unknown(ledger_store) -> None:
    writer, reader = ledger_store
    writer.record_economic_funding(
        **_scope(),
        event_time="2026-01-01T08:00:00Z",
        symbol="BTCUSDT",
        instrument_type="PERPETUAL",
        asset="USDT",
        amount="1",
        exchange_event_id="unknown-reconcile-funding",
    )

    result = reader.reconcile_economic_ledger(
        **_scope(),
        opening_balances={"USDT": "100", "BTC": "1"},
        exchange_balances={"USDT": "101"},
        tolerances={"BTC": "0.0001"},
    )

    assert result.matched is False
    assert result.expected_balances["USDT"] == Decimal("101")
    assert result.tolerances["USDT"] is None
    assert result.expected_balances["BTC"] is None
    assert result.exchange_balances["BTC"] is None
    assert result.differences["BTC"] is None
    assert result.unknown_components == {
        "BTC": ("projected_delta", "exchange_balance"),
        "USDT": ("tolerance",),
    }


def test_availability_cannot_precede_exchange_event_time(ledger_store) -> None:
    writer, _ = ledger_store
    event = EconomicLedgerEvent(
        event_type=DEPOSIT,
        event_time="2026-01-01T00:00:01Z",
        availability_time="2026-01-01T00:00:00Z",
        account_id="a",
        trading_mode="paper",
        venue="BINANCE",
        strategy_id="s",
        amount="1",
        amount_asset="USDT",
        source_event_id="bad-time",
    )
    with pytest.raises(LedgerValidationError, match="must not precede"):
        writer.record_economic_events((event,))
