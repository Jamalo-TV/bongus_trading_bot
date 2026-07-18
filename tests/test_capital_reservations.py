from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import sqlite3

import pytest

from bongus.portfolio.capital_reservations import (
    CapitalReservationBook,
    CapitalState,
    ReservationError,
    ReservationPolicy,
    ReservationPurpose,
    ReservationRequest,
)


NOW = datetime(2026, 7, 18, tzinfo=timezone.utc)


@pytest.fixture
def book(tmp_path):
    value = CapitalReservationBook(str(tmp_path / "state.db"))
    yield value
    value.close()


def capital(**overrides):
    values = {
        "equity_usd": "10000",
        "spot_cash_available_usd": "5000",
        "futures_margin_available_usd": "4000",
        "current_pair_gross_usd": "0",
        "max_pair_gross_usd": "10000",
        "current_initial_margin_usd": "0",
    }
    values.update(overrides)
    return CapitalState(**values)


def policy(**overrides):
    values = {
        "repair_reserve_usd": "500",
        "exit_reserve_usd": "250",
        "minimum_liquidation_buffer_usd": "1000",
        "max_margin_utilization": "0.8",
    }
    values.update(overrides)
    return ReservationPolicy(**values)


def request(reservation_id="r1", **overrides):
    values = {
        "reservation_id": reservation_id,
        "purpose": ReservationPurpose.ENTRY,
        "symbol": "BTCUSDT",
        "cycle_id": "cycle-1",
        "spot_cash_usd": "2500",
        "futures_margin_usd": "1250",
        "fees_usd": "10",
        "pair_gross_increment_usd": "5000",
        "config_version": "cfg-1",
    }
    values.update(overrides)
    return ReservationRequest(**values)


def test_entry_reserves_only_capital_left_after_repair_exit_and_liquidation_buffers(book) -> None:
    admitted = book.reserve(request(), capital=capital(), policy=policy(), now=NOW)
    assert admitted.allowed
    assert admitted.projection.reserved_spot_cash_usd == Decimal("2500")
    assert admitted.projection.entry_spot_cash_remaining_usd == Decimal("1740")
    assert admitted.projection.entry_futures_margin_remaining_usd == Decimal("1000")

    rejected = book.reserve(request("r2"), capital=capital(), policy=policy(), now=NOW)
    assert not rejected.allowed
    assert "spot_cash_after_repair_exit_reserves" in rejected.reasons
    assert "futures_margin_after_repair_exit_liquidation_reserves" in rejected.reasons


def test_idempotent_request_and_content_collision(book) -> None:
    first = book.reserve(request(), capital=capital(), policy=policy(), now=NOW)
    second = book.reserve(request(), capital=capital(), policy=policy(), now=NOW)
    assert first.allowed and second.allowed and second.duplicate
    assert len(book.active()) == 1
    with pytest.raises(ReservationError, match="content collision"):
        book.reserve(request(spot_cash_usd="2000"), capital=capital(), policy=policy(), now=NOW)


def test_unknown_exchange_effect_never_expires_or_releases_without_terminal_proof(book) -> None:
    req = request(expires_at=(NOW + timedelta(seconds=1)).isoformat())
    assert book.reserve(req, capital=capital(), policy=policy(), now=NOW).allowed
    book.mark_dispatched("r1", evidence={"intent_id": "i1"})
    book.mark_delivery_unknown("r1", evidence={"timeout": True})
    with pytest.raises(ReservationError, match="terminal proof"):
        book.release("r1", reason="timeout", exchange_terminal_proven=False)

    # A later admission runs expiry maintenance, but UNKNOWN remains reserved.
    rejected = book.reserve(request("r2"), capital=capital(), policy=policy(), now=NOW + timedelta(days=1))
    assert not rejected.allowed
    assert book.active()[0]["state"] == "UNKNOWN"
    book.release("r1", reason="exchange_query_terminal", exchange_terminal_proven=True)
    assert book.active() == []


def test_unsent_expired_reservation_is_reclaimable(book) -> None:
    expiring = request(expires_at=(NOW + timedelta(seconds=1)).isoformat())
    assert book.reserve(expiring, capital=capital(), policy=policy(), now=NOW).allowed
    replacement = book.reserve(request("r2"), capital=capital(), policy=policy(), now=NOW + timedelta(seconds=2))
    assert replacement.allowed
    rows = book.conn.execute(
        "SELECT reservation_id, state, release_reason FROM capital_reservations ORDER BY reservation_id"
    ).fetchall()
    assert tuple(rows[0]) == ("r1", "RELEASED", "expired_before_dispatch")


def test_exit_and_repair_can_use_protected_pool_but_not_actual_missing_capital(book) -> None:
    repair = request(
        purpose=ReservationPurpose.HEDGE_REPAIR,
        cycle_id="cycle-1",
        spot_cash_usd="3900",
        futures_margin_usd="2500",
        pair_gross_increment_usd="0",
    )
    admitted = book.reserve(repair, capital=capital(), policy=policy(), now=NOW)
    assert admitted.allowed

    impossible = request(
        "r2",
        purpose=ReservationPurpose.EXIT,
        spot_cash_usd="2000",
        futures_margin_usd="1000",
        pair_gross_increment_usd="0",
    )
    rejected = book.reserve(impossible, capital=capital(), policy=policy(), now=NOW)
    assert not rejected.allowed
    assert "insufficient_raw_spot_cash_for_repair_or_exit" in rejected.reasons
    assert "insufficient_raw_margin_for_repair_or_exit" in rejected.reasons


def test_pair_gross_and_margin_utilization_are_pair_level_hard_caps(book) -> None:
    gross_rejected = book.reserve(
        request(pair_gross_increment_usd="5000"),
        capital=capital(current_pair_gross_usd="6000"),
        policy=policy(),
        now=NOW,
    )
    assert "pair_gross_cap" in gross_rejected.reasons

    margin_rejected = book.reserve(
        request(futures_margin_usd="2000"),
        capital=capital(equity_usd="5000", current_initial_margin_usd="2500"),
        policy=policy(max_margin_utilization="0.8"),
        now=NOW,
    )
    assert "initial_margin_utilization_cap" in margin_rejected.reasons


def test_inverse_entry_consumes_symbol_borrow_budget_and_fails_closed(book) -> None:
    inverse = request(
        spot_cash_usd="0",
        spot_borrow_usd="2500",
    )
    unknown = book.reserve(
        inverse,
        capital=capital(spot_borrow_available_usd="0"),
        policy=policy(),
        now=NOW,
    )
    assert not unknown.allowed
    assert unknown.reasons == ("spot_borrow_availability",)

    admitted = book.reserve(
        inverse,
        capital=capital(spot_borrow_available_usd="2500"),
        policy=policy(),
        now=NOW,
    )
    assert admitted.allowed
    assert admitted.projection.reserved_spot_borrow_usd == Decimal("2500")
    assert admitted.projection.entry_spot_borrow_remaining_usd == Decimal("0")

    exhausted = book.reserve(
        request(
            "r2",
            spot_cash_usd="0",
            spot_borrow_usd="1",
            pair_gross_increment_usd="1000",
        ),
        capital=capital(spot_borrow_available_usd="2500"),
        policy=policy(),
        now=NOW,
    )
    assert not exhausted.allowed
    assert "spot_borrow_availability" in exhausted.reasons


def test_borrow_reservations_are_scoped_to_the_requested_symbol(book) -> None:
    assert book.reserve(
        request(
            spot_cash_usd="0",
            spot_borrow_usd="2000",
            futures_margin_usd="500",
        ),
        capital=capital(spot_borrow_available_usd="2000"),
        policy=policy(),
        now=NOW,
    ).allowed

    other_symbol = book.reserve(
        request(
            "r2",
            symbol="ETHUSDT",
            cycle_id="cycle-2",
            spot_cash_usd="0",
            spot_borrow_usd="2000",
            futures_margin_usd="500",
        ),
        capital=capital(spot_borrow_available_usd="2000"),
        policy=policy(),
        now=NOW,
    )
    assert other_symbol.allowed
    assert other_symbol.projection.reserved_spot_borrow_usd == Decimal("2000")


def test_legacy_reservation_migration_preserves_idempotent_replay(tmp_path) -> None:
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE capital_reservations (
            reservation_id TEXT PRIMARY KEY,
            request_hash TEXT NOT NULL,
            purpose TEXT NOT NULL,
            symbol TEXT NOT NULL,
            cycle_id TEXT NOT NULL,
            spot_cash_usd TEXT NOT NULL,
            futures_margin_usd TEXT NOT NULL,
            fees_usd TEXT NOT NULL,
            pair_gross_increment_usd TEXT NOT NULL,
            config_version TEXT NOT NULL,
            state TEXT NOT NULL,
            expires_at TEXT,
            exchange_terminal_proven INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            released_at TEXT,
            release_reason TEXT NOT NULL DEFAULT ''
        );
        """
    )
    normalized = request().normalized()
    legacy_normalized = {
        key: value for key, value in normalized.items() if key != "spot_borrow_usd"
    }
    legacy_hash = hashlib.sha256(
        json.dumps(legacy_normalized, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    conn.execute(
        """INSERT INTO capital_reservations
           (reservation_id, request_hash, purpose, symbol, cycle_id,
            spot_cash_usd, futures_margin_usd, fees_usd,
            pair_gross_increment_usd, config_version, state, expires_at,
            metadata_json, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            normalized["reservation_id"],
            legacy_hash,
            normalized["purpose"],
            normalized["symbol"],
            normalized["cycle_id"],
            normalized["spot_cash_usd"],
            normalized["futures_margin_usd"],
            normalized["fees_usd"],
            normalized["pair_gross_increment_usd"],
            normalized["config_version"],
            "RESERVED",
            normalized["expires_at"],
            json.dumps(normalized["metadata"], sort_keys=True, separators=(",", ":")),
            NOW.isoformat(),
            NOW.isoformat(),
        ),
    )
    conn.commit()
    conn.close()

    migrated = CapitalReservationBook(str(db_path))
    try:
        replay = migrated.reserve(
            request(), capital=capital(), policy=policy(), now=NOW
        )
        assert replay.allowed and replay.duplicate
        assert migrated.active()[0]["spot_borrow_usd"] == "0"
    finally:
        migrated.close()


def test_treasury_never_spends_reserved_repair_or_exit_cash(book) -> None:
    treasury = request(
        purpose=ReservationPurpose.TREASURY,
        cycle_id="",
        spot_cash_usd="4500",
        futures_margin_usd="0",
        pair_gross_increment_usd="0",
    )
    decision = book.reserve(treasury, capital=capital(), policy=policy(), now=NOW)
    assert not decision.allowed
    assert "spot_cash_after_repair_exit_reserves" in decision.reasons
