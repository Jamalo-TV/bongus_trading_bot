from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import time
from unittest.mock import MagicMock, patch

from bongus.engine.cooldown_manager import CooldownManager
from bongus.engine.state_store import StateWriter
from bongus.market_data.feed_recovery import FeedCursorStore, FeedSource, FeedState
from scripts.live_trader_v2 import LiveTraderV2


def _build_trader(db_path: str) -> LiveTraderV2:
    with patch("scripts.live_trader_v2.ConfigManager.start_watching", autospec=True):
        return LiveTraderV2(
            db_path=db_path,
            config_path=f"{db_path}.config.json",
        )


def _close_trader(trader: LiveTraderV2) -> None:
    trader.execution.close()
    trader.capital_reservations.close()
    trader.feed_cursors.close()
    trader.cooldowns.close()
    trader.state_reader.close()
    trader.state_writer.close()


def test_depth_gap_block_survives_restart_until_both_books_prove_ready(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("TRADING_MODE", "paper")
    db_path = str(tmp_path / "state.db")
    first = _build_trader(db_path)
    first._handle_feed_gap(
        {
            "symbol": "BTCUSDT",
            "market": "perp",
            "last_update_id": 10,
            "first_update_id": 12,
            "previous_final_update_id": 11,
            "final_update_id": 15,
            "reason": "depth_sequence_gap",
        }
    )
    rows = {
        row["stream"]: row
        for row in first.feed_cursors.snapshot()
        if row["symbol"] == "BTCUSDT"
    }
    assert rows["depth_spot"]["state"] == FeedState.GAPPED.value
    assert rows["depth_perp"]["state"] == FeedState.GAPPED.value
    _close_trader(first)

    restored = _build_trader(db_path)
    try:
        assert "BTCUSDT" in restored._symbol_safe_mode_blocks
        assert "depth_sequence_gap" in restored._symbol_safe_mode_reasons["BTCUSDT"]

        restored._handle_sequenced_depth_event(
            {
                "symbol": "BTCUSDT",
                "market": "perp",
                "first_update_id": 16,
                "previous_final_update_id": 15,
                "final_update_id": 20,
                "sequence_contiguous": True,
            }
        )
        assert "depth_sequence_gap" in restored._symbol_safe_mode_reasons["BTCUSDT"]

        restored._handle_sequenced_depth_event(
            {
                "symbol": "BTCUSDT",
                "market": "spot",
                "final_update_id": 30,
                "is_snapshot": True,
                "sequence_contiguous": True,
            }
        )
        assert "depth_sequence_gap" not in restored._symbol_safe_mode_reasons.get(
            "BTCUSDT",
            set(),
        )
        assert "BTCUSDT" not in restored._symbol_safe_mode_blocks
    finally:
        _close_trader(restored)


def test_global_and_symbol_safe_modes_survive_restart_until_explicit_clear(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("TRADING_MODE", "paper")
    db_path = str(tmp_path / "state.db")
    first = _build_trader(db_path)
    first._set_safe_mode_flag("economic_ledger_reconciliation", True)
    first._set_symbol_safe_mode_reason("BTCUSDT", "position_divergence", True)
    _close_trader(first)

    restored = _build_trader(db_path)
    try:
        assert "economic_ledger_reconciliation" in restored._safe_mode_flags
        assert restored._runtime_mode == "SAFE_MODE"
        assert restored._entry_policy_block_reason() == (
            "safe mode: economic_ledger_reconciliation"
        )
        assert restored._symbol_safe_mode_reasons["BTCUSDT"] == {
            "position_divergence"
        }
        assert restored._describe_symbol_block("BTCUSDT") == (
            "symbol safe mode (position_divergence)"
        )

        restored._set_safe_mode_flag("economic_ledger_reconciliation", False)
        restored._set_symbol_safe_mode_reason(
            "BTCUSDT", "position_divergence", False
        )
    finally:
        _close_trader(restored)

    cleared = _build_trader(db_path)
    try:
        assert "economic_ledger_reconciliation" not in cleared._safe_mode_flags
        assert "BTCUSDT" not in cleared._symbol_safe_mode_blocks
    finally:
        _close_trader(cleared)


def test_depth_gap_is_entry_only_and_does_not_block_reduce_only_exit(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("TRADING_MODE", "paper")
    trader = _build_trader(str(tmp_path / "state.db"))
    try:
        trader._handle_feed_gap(
            {
                "symbol": "BTCUSDT",
                "market": "perp",
                "last_update_id": 10,
                "first_update_id": 12,
                "final_update_id": 15,
            }
        )
        send = MagicMock(return_value=True)
        trader.execution.send_order_intent = send

        trader._dispatch_exit(
            "BTCUSDT",
            direction="long",
            position_row={
                "symbol": "BTCUSDT",
                "qty": 1.0,
                "hedge_ratio": 1.0,
                "side": "LONG_SPOT_SHORT_PERP",
            },
        )

        send.assert_called_once()
        assert send.call_args.args[0]["intent"] == "EXIT_LONG"
    finally:
        _close_trader(trader)


def test_memory_store_guards_share_authoritative_connection_and_close_once() -> None:
    writer = StateWriter(":memory:")
    cooldowns = CooldownManager(
        connection=writer._cooldown_conn,
        lock=writer._guard_lock,
    )
    feeds = FeedCursorStore(
        connection=writer._feed_recovery_conn,
        lock=writer._guard_lock,
    )
    cooldowns.activate_symbol("BTCUSDT", 60.0, "stress", now_ts=time.time())
    feeds.record_gap(
        FeedSource("binance", "depth_perp", "BTCUSDT"),
        prior_sequence=10,
        final_sequence=15,
    )

    assert writer._cooldown_conn is writer.conn
    assert writer._feed_recovery_conn is writer.conn
    assert writer._command_conn is writer.conn
    assert writer.conn.execute("SELECT COUNT(*) FROM cooldown_entries").fetchone()[0] == 1
    assert writer.conn.execute("SELECT COUNT(*) FROM feed_cursors").fetchone()[0] == 1
    envelope = writer.reserve_execution_command(
        {
            "intent": "ENTER_LONG",
            "intent_id": "memory-command-1",
            "symbol": "BTCUSDT",
            "quantity": 1.0,
            "urgency": 0.5,
            "max_slippage_bps": 5.0,
            "exposure_scale": 1.0,
            "account_id": "account-a",
            "environment": "paper",
            "strategy_id": "funding-v2",
            "cycle_id": "cycle-1",
            "config_version_hash": "config-abc",
        },
        producer_id="test-producer",
        ttl_ms=30_000,
    )
    assert envelope["intent_id"] == "memory-command-1"

    # The guard wrappers do not own the shared connection; StateWriter closes
    # the authoritative connection exactly once.
    cooldowns.close()
    feeds.close()
    writer.close()


def test_guard_connections_do_not_interleave_concurrent_runtime_writes(tmp_path) -> None:
    writer = StateWriter(str(tmp_path / "state.db"))
    cooldowns = CooldownManager(
        connection=writer._cooldown_conn,
        lock=writer._guard_lock,
    )
    feeds = FeedCursorStore(
        connection=writer._feed_recovery_conn,
        lock=writer._guard_lock,
    )
    source = FeedSource("binance", "depth_perp", "BTCUSDT")

    def write_runtime_state() -> None:
        for index in range(30):
            writer.set_risk_snapshot({"concurrent_guard_test": index})

    def write_cooldowns() -> None:
        base = time.time()
        for index in range(30):
            cooldowns.activate_symbol(
                "BTCUSDT",
                60.0 + index,
                f"stress_{index}",
                now_ts=base,
            )

    def write_feed_gaps() -> None:
        for index in range(30):
            feeds.record_gap(
                source,
                prior_sequence=100 + index,
                first_sequence=105 + index,
                final_sequence=110 + index,
            )

    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [
                pool.submit(write_runtime_state),
                pool.submit(write_cooldowns),
                pool.submit(write_feed_gaps),
            ]
            for future in futures:
                future.result()

        assert writer.conn.execute(
            "SELECT value FROM risk_state WHERE key = 'concurrent_guard_test'"
        ).fetchone()[0] == "29"
        assert cooldowns.snapshot()["symbol_cooldowns"]["BTCUSDT"]["reason"] == "stress_29"
        assert feeds.snapshot(source)[0]["state"] == FeedState.GAPPED.value
    finally:
        cooldowns.close()
        feeds.close()
        writer.close()
