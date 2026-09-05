"""Regression proofs for kill recovery, owner-thread callbacks and emergency accounting."""

import asyncio
from datetime import datetime, timedelta, timezone
import threading
from unittest.mock import AsyncMock, patch

import pytest

from bongus.engine.risk_engine import RiskDecision
from bongus.engine.state_store import StateWriter
from bongus.market_data.rust_data_subscriber import RustDataSubscriber
from bongus.supervisor.service import SupervisorService
from tests import test_live_trader_startup as startup_support
from tests.test_supervisor_service import FakeTelegramClient


def _close(trader):
    trader._config.stop_watching()
    trader.execution.close()
    trader.state_reader.close()
    trader.state_writer.close()
    trader._storage_executor.shutdown(wait=True, cancel_futures=True)


@pytest.fixture
def trader(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "paper")
    instance = startup_support.TestLiveTraderStartupReconciliation()._build_trader(str(tmp_path / "safety.db"))
    yield instance
    _close(instance)


def test_kill_survives_restart_and_cannot_clear_without_explicit_request(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "paper")
    path = str(tmp_path / "restart.db")
    first = startup_support.TestLiveTraderStartupReconciliation()._build_trader(path)
    trigger = RiskDecision(False, True, True, 0.0, ["drawdown"])
    first._apply_kill_episode(trigger, [], 0.11)
    episode_id = first._kill_episode["episode_id"]
    _close(first)
    recovered = startup_support.TestLiveTraderStartupReconciliation()._build_trader(path)
    try:
        assert recovered._risk_kill_switch
        assert not recovered._risk_allow_new_risk
        safe = RiskDecision(True, False, False, 1.0, [])
        assert recovered._apply_kill_episode(safe, [], 0.01).kill_switch
        assert recovered._kill_episode["episode_id"] == episode_id
        assert recovered.state_reader.get_risk()["kill_switch"] is True
    finally:
        _close(recovered)


def test_kill_release_requires_new_request_after_trigger_and_flatness(trader):
    trader._apply_kill_episode(RiskDecision(False, True, True, 0.0, ["drawdown"]), [], 0.11)
    safe = RiskDecision(True, False, False, 1.0, [])
    trader._config.apply_updates({
        "kill_recovery_request_id": "old-request",
        "kill_recovery_requested_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
        "kill_recovery_requested_by": "operator",
    })
    assert trader._apply_kill_episode(safe, [], 0.01).kill_switch
    trader._config.apply_updates({
        "kill_recovery_request_id": "new-request",
        "kill_recovery_requested_at": datetime.now(timezone.utc).isoformat(),
    })
    residual = [{"symbol": "BTCUSDT", "qty": .001, "recovery_state": "manual_review"}]
    assert trader._apply_kill_episode(safe, residual, 0.01).kill_switch
    assert trader._kill_episode["status"] == "manual_review_required"
    released = trader._apply_kill_episode(safe, [], 0.01)
    assert not released.kill_switch
    assert trader.state_reader.get_risk()["kill_recovery_consumed_request_id"] == "new-request"
    trader._apply_kill_episode(RiskDecision(False, True, True, 0.0, ["second incident"]), [], 0.11)
    assert trader._apply_kill_episode(safe, [], 0.01).kill_switch


def test_local_empty_does_not_complete_live_flatten(trader):
    trader._trading_mode = "live"
    trader._account_reconciliation_ready = False
    trader.state_writer.set_risk_snapshot({
        "operator_flatten_all_request_id": "flatten-1",
        "operator_flatten_all_status": "requested",
        "account_reconciliation_exchange_only_symbols": ["ETHUSDT"],
    })
    trader.state_writer.flush()
    assert trader._maybe_process_operator_flatten_all_request([])
    risk = trader.state_reader.get_risk()
    assert risk["operator_flatten_all_status"] == "awaiting_reconciliation"
    assert risk["operator_flatten_all_remaining_symbols"] == ["ETHUSDT"]
    assert risk["allow_new_risk"] is False


def test_live_recovery_needs_two_fresh_complete_account_proofs(trader):
    trader._trading_mode = "live"
    trader._account_reconciliation_ready = True
    trader._account_truth_ready = True
    trader._account_truth_status = "COMPLETE"
    trader._account_truth_expires_at = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
    risk = {"economic_ledger_reconciled": True, "account_flat_proof_ready": True, "account_flat_confirmation_count": 1}
    assert not trader._account_flat_recovery_proof(risk, [])
    risk["account_flat_confirmation_count"] = 2
    assert trader._account_flat_recovery_proof(risk, [])
    trader._account_truth_expires_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    assert not trader._account_flat_recovery_proof(risk, [])


def test_watcher_callback_runs_on_owner_loop_and_rebuilds_policy(trader):
    async def scenario():
        trader._loop = asyncio.get_running_loop()
        owner = threading.get_ident()
        threads = []
        original = trader._set_config_reload_status
        previous = trader.decision_engine
        def record(payload):
            threads.append(threading.get_ident())
            original(payload)
        with patch.object(trader, "_set_config_reload_status", side_effect=record):
            worker = threading.Thread(target=trader._on_config_reloaded, args=({"pause_new_entries": (False, True)}, {}))
            worker.start()
            worker.join(timeout=1)
            await asyncio.sleep(.02)
        assert threads and set(threads) == {owner}
        assert trader.decision_engine is not previous
    asyncio.run(scenario())


def test_live_mark_callback_preserves_newer_cache_and_predictor_on_stale_replay(trader):
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    with patch.object(trader.predictor, "push_sample") as predictor, \
         patch.object(trader.regime_filter, "on_mark_price") as regime:
        trader._on_mark_price("BTCUSDT", 100, .0002, exchange_event_time_ms=now_ms)
        trader._on_mark_price("BTCUSDT", 90, -.001, exchange_event_time_ms=now_ms - 1000)
        trader._on_mark_price("BTCUSDT", 80, -.002, exchange_event_time_ms=now_ms)
    assert trader._mark_prices["BTCUSDT"] == 100
    predictor.assert_called_once()
    regime.assert_called_once()


def test_shutdown_cancels_pending_preflight_before_closing_database(trader):
    async def scenario():
        entered = asyncio.Event()
        async def preflight():
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("startup resumed after shutdown")
        with patch.object(trader, "_run_preflight", side_effect=preflight), \
             patch.object(trader, "_on_startup", new_callable=AsyncMock) as startup, \
             patch.object(trader, "_install_signal_handlers"), \
             patch.object(trader.subscriber, "run", new_callable=AsyncMock):
            running = asyncio.create_task(trader.run())
            await asyncio.wait_for(entered.wait(), 2)
            await asyncio.wait_for(trader.shutdown(reason="test"), 2)
            await asyncio.wait_for(running, 2)
        startup.assert_not_awaited()
        assert trader._shutdown_complete.is_set()
    asyncio.run(scenario())


def test_unowned_inventory_cannot_be_adopted_or_liquidated_after_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "paper")
    path = str(tmp_path / "ownership.db")
    first = startup_support.TestLiveTraderStartupReconciliation()._build_trader(path)
    first.state_writer.set_risk_snapshot({"ownership_review_symbols": ["BTCUSDT"]})
    first.state_writer.flush()
    _close(first)
    recovered = startup_support.TestLiveTraderStartupReconciliation()._build_trader(path)
    try:
        with patch.object(recovered.execution, "restore_position_tracking") as restore, \
             patch.object(recovered.execution, "send_order_intent") as execute:
            assert not recovered._sync_position_to_execution_engine({"symbol": "BTCUSDT", "qty": 1})
            recovered._dispatch_exit("BTCUSDT", position_row={"symbol": "BTCUSDT", "qty": 1})
        restore.assert_not_called()
        execute.assert_not_called()
        assert "divergence_exit_blocked" in recovered._safe_mode_flags
    finally:
        _close(recovered)


def test_emergency_flat_replay_retains_cost_basis_and_cannot_invent_pnl(trader):
    event = {
        "event": "EmergencyExitState", "state": "FLAT", "symbol": "BTCUSDT",
        "intent_id": "exit-1", "publication_id": "emergency:exit-1:FLAT",
        "flat_proof": True, "verified_spot_inventory_decimal": "0",
        "verified_futures_inventory_decimal": "0", "telemetry_sequence": 10,
    }
    trader._on_emergency_exit_state(event)
    first = trader.state_reader.get_risk()["emergency_accounting_pending"]
    trader._on_emergency_exit_state({**event, "telemetry_sequence": 99})
    assert trader.state_reader.get_risk()["emergency_accounting_pending"] == first
    with patch.object(trader, "_finalize_exit_fill") as finalize:
        trader._reconcile_emergency_accounting({"position_risk": [], "spot_account": {"balances": []}})
    finalize.assert_not_called()
    assert trader.state_reader.get_risk()["emergency_accounting_pending"]
    assert not trader._account_reconciliation_ready


@pytest.mark.parametrize("missing_field", ["flat_proof", "verified_spot_inventory_decimal", "verified_futures_inventory_decimal"])
def test_emergency_missing_or_nonzero_flat_proof_blocks(trader, missing_field):
    event = {
        "event": "EmergencyExitState", "state": "FLAT", "symbol": "BTCUSDT",
        "intent_id": "exit-1", "publication_id": "emergency:exit-1:FLAT",
        "flat_proof": True, "verified_spot_inventory_decimal": "0",
        "verified_futures_inventory_decimal": "0",
    }
    event.pop(missing_field)
    trader._on_emergency_exit_state(event)
    risk = trader.state_reader.get_risk()
    assert risk["execution_reconciliation_required"]
    assert not risk.get("emergency_accounting_pending")


def test_empty_telegram_allowlist_blocks_poll_and_direct_mutation(tmp_path):
    StateWriter(db_path=str(tmp_path / "supervisor.db")).close()
    client = FakeTelegramClient([{"update_id": 1, "message": {"chat": {"id": "123"}, "text": "/resume"}}])
    service = SupervisorService(db_path=str(tmp_path / "supervisor.db"), config_path=str(tmp_path / "config.json"),
                                telegram_client=client, allowed_chat_ids=[], report_schedules=[])
    try:
        handler = AsyncMock()
        with patch.object(service, "_handle_command", handler):
            asyncio.run(service._process_telegram_commands(datetime.now(timezone.utc)))
        handler.assert_not_awaited()
        with patch.object(service.config_manager, "apply_updates") as mutate:
            asyncio.run(service._handle_command("123", "/resume", datetime.now(timezone.utc)))
        mutate.assert_not_called()
    finally:
        service.close()


@pytest.mark.parametrize("event_name", ["OrderUpdate", "EmergencyExitState"])
def test_publication_replay_new_sequences_projects_once_across_restart(tmp_path, event_name):
    path = str(tmp_path / "publications.db")
    event = {
        "event": event_name, "publication_id": "terminal:exit:1", "symbol": "BTCUSDT",
        "telemetry_schema_version": 1, "telemetry_ack_required": True,
        "telemetry_sequence": 1, "event_time_ms": 1000,
        "terminal_sequence": 1, "terminal_watermark": 1,
    }
    writer = StateWriter(db_path=path)
    try:
        assert writer.append_durable_telemetry_receipt(event)
        assert writer.append_durable_telemetry_receipt({**event, "telemetry_sequence": 2, "terminal_sequence": 2, "terminal_watermark": 2})
        received = []
        subscriber = RustDataSubscriber(
            durable_receipt_append=writer.append_durable_telemetry_receipt,
            durable_receipt_complete=writer.complete_durable_telemetry,
            durable_receipt_loader=writer.pending_durable_telemetry_events,
        )
        subscriber.on(event_name, received.append)
        async def recover():
            await subscriber.recover_pending_projections()
            await subscriber.wait_for_projection_idle(timeout=2)
            await subscriber._stop_projection_worker()
        asyncio.run(recover())
        assert len(received) == 1
        assert writer.pending_durable_telemetry_events() == []
    finally:
        writer.close()
    writer = StateWriter(db_path=path)
    try:
        assert not writer.append_durable_telemetry_receipt({**event, "telemetry_sequence": 3, "terminal_sequence": 3, "terminal_watermark": 3})
        with pytest.raises(ValueError, match="publication conflict"):
            writer.append_durable_telemetry_receipt({**event, "telemetry_sequence": 4, "symbol": "ETHUSDT"})
    finally:
        writer.close()


def test_emergency_uses_original_and_repair_fills_but_waits_for_economic_completion(trader):
    event = {
        "event_time_ms": int(datetime.now(timezone.utc).timestamp() * 1000),
        "spot_generations": [{"client_order_id": "spot-repair"}],
        "futures_generations": [{"client_order_id": "perp-repair"}],
        "original_exit_spot_client_order_ids": ["spot-original"],
        "original_exit_futures_client_order_ids": ["perp-original"],
    }
    record = {"symbol": "BTCUSDT", "intent_id": "exit-owned", "event": event,
              "position": {"symbol": "BTCUSDT", "qty": 1, "entry_time": "2026-01-01T00:00:00+00:00"}}
    trader.state_writer.set_risk_snapshot({"emergency_accounting_pending": {"emergency:exit-owned:FLAT": record}})
    trader.state_writer.flush()
    fills = [
        {"event_type": "FILL", "instrument_type": instrument, "client_order_id": client,
         "quantity": str(qty), "price": str(price)}
        for instrument, client, qty, price in [
            ("SPOT", "spot-original", .25, 100), ("SPOT", "spot-repair", .75, 104),
            ("PERPETUAL", "perp-original", .5, 100), ("PERPETUAL", "perp-repair", .5, 102),
        ]
    ]
    with patch.object(trader, "_pending_intent_row", return_value={"intent_type": "EXIT_LONG"}), \
         patch.object(trader.state_reader, "get_trade_execution_cost_evidence", return_value={"complete": True}), \
         patch.object(trader.state_reader, "get_economic_ledger_events", return_value=fills), \
         patch.object(trader, "_finalize_exit_fill", side_effect=[False, True]) as finalize:
        trader._reconcile_emergency_accounting({"position_risk": [], "spot_account": {"balances": []}})
        assert trader.state_reader.get_risk()["emergency_accounting_pending"]
        trader._reconcile_emergency_accounting({"position_risk": [], "spot_account": {"balances": []}})
    assert finalize.call_args.kwargs["spot_fill_price"] == 103
    assert finalize.call_args.kwargs["perp_fill_price"] == 101
    assert not trader.state_reader.get_risk()["emergency_accounting_pending"]
