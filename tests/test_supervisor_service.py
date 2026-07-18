import asyncio
import json
from datetime import datetime, timedelta, timezone

from bongus.core.config_manager import ConfigManager
from bongus.engine.state_store import StateWriter
from bongus.supervisor.core import collect_snapshot
from bongus.supervisor.incidents import IncidentObservation
from bongus.supervisor.models import (
    IncidentScope,
    IncidentSeverity,
    RecommendationStatus,
    ReportSchedule,
    SupervisorRecommendation,
)
from bongus.supervisor.service import SupervisorService
from bongus.supervisor.store import SupervisorStore


class FakeTelegramClient:
    def __init__(self, updates=None):
        self.updates = list(updates or [])
        self.sent_messages = []
        self.commands = []

    async def send_message(self, message: str, chat_id: str | None = None) -> None:
        self.sent_messages.append((str(chat_id) if chat_id is not None else None, message))

    async def get_updates(self, offset: int | None = None, timeout: int = 1):
        del timeout
        available = [item for item in self.updates if offset is None or item["update_id"] >= offset]
        self.updates = []
        return available

    async def set_my_commands(self, commands):
        self.commands.append(list(commands))


class FailingTelegramClient(FakeTelegramClient):
    async def get_updates(self, offset: int | None = None, timeout: int = 1):
        del offset, timeout
        raise RuntimeError("telegram conflict")


class SendFailingTelegramClient(FakeTelegramClient):
    async def send_message(self, message: str, chat_id: str | None = None) -> None:
        del message, chat_id
        raise RuntimeError("telegram send failed")


def seed_supervisor_db(tmp_path):
    db_path = str(tmp_path / "service.db")
    writer = StateWriter(db_path=db_path)
    writer.set_stat("account_equity", 10_000.0)
    writer.set_stat("total_pnl", -500.0)
    writer.set_stat("gross_exposure", 10_000.0)
    writer.set_stat("max_gross_exposure", 50_000.0)
    writer.set_stat("ann_funding", 0.12)
    writer.set_stat("win_rate", 0.5)
    writer.set_stat("trade_count", 4)
    writer.set_risk("drawdown_pct", "0.09")
    writer.set_risk("kill_switch", "true")
    writer.set_risk("allow_new_risk", "false")
    writer.set_risk("pause_new_entries", "false")
    writer.set_risk("reasons", json.dumps(["market data staleness too high"]))
    writer.close()
    return db_path


def test_service_run_once_autopauses_and_sends_report(tmp_path):
    seeded_supervisor_db = seed_supervisor_db(tmp_path)
    config_path = str(tmp_path / "live_config.json")
    telegram = FakeTelegramClient()
    store = SupervisorStore(db_path=seeded_supervisor_db)
    config_manager = ConfigManager(config_path=config_path)
    now = datetime(2026, 4, 1, 10, 0, tzinfo=timezone.utc)
    schedule = ReportSchedule(kind="weekly", weekday=now.weekday(), hour=now.hour, minute=now.minute, title="Weekly")

    service = SupervisorService(
        db_path=seeded_supervisor_db,
        config_path=config_path,
        timezone_name="UTC",
        telegram_client=telegram,
        store=store,
        config_manager=config_manager,
        report_schedules=[schedule],
        allowed_chat_ids=["123"],
    )

    asyncio.run(service.run_once(now=now))

    with open(config_path, encoding="utf-8") as handle:
        live_config = json.load(handle)

    assert live_config["pause_new_entries"] is True
    assert any("paused automatically" in message for _, message in telegram.sent_messages)
    assert any("Trigger:" in message for _, message in telegram.sent_messages)
    assert store.list_recommendations(limit=5)
    service.close()


def test_service_approve_command_applies_recommendation(tmp_path):
    db_path = str(tmp_path / "approve.db")
    writer = StateWriter(db_path=db_path)
    writer.set_stat("account_equity", 10_000.0)
    writer.close()

    config_path = str(tmp_path / "live_config.json")
    store = SupervisorStore(db_path=db_path)
    config_manager = ConfigManager(config_path=config_path)
    now = datetime.now(timezone.utc)
    recommendation = SupervisorRecommendation(
        recommendation_id="rec123",
        created_at=now.isoformat(),
        report_kind="manual",
        kind="reduce_notional",
        target_key="notional_per_trade",
        current_value=2_500.0,
        proposed_value=1_875.0,
        rationale="Reduce size after drawdown.",
        expires_at=(now + timedelta(days=7)).isoformat(),
    )
    store.save_recommendations([recommendation])

    telegram = FakeTelegramClient(
        updates=[
            {
                "update_id": 1,
                "message": {
                    "chat": {"id": "42"},
                    "text": "/approve rec123",
                },
            }
        ]
    )

    service = SupervisorService(
        db_path=db_path,
        config_path=config_path,
        timezone_name="UTC",
        telegram_client=telegram,
        store=store,
        config_manager=config_manager,
        report_schedules=[],
        allowed_chat_ids=["42"],
    )

    asyncio.run(service.run_once(now=now))

    applied = store.get_recommendation("rec123")
    assert applied is not None
    assert applied["status"] == RecommendationStatus.APPLIED.value
    assert config_manager.get("notional_per_trade") == 1_875.0
    assert any("Applied rec123" in message for _, message in telegram.sent_messages)
    service.close()


def test_service_ignores_telegram_poll_failures(tmp_path):
    seeded_supervisor_db = seed_supervisor_db(tmp_path)
    config_path = str(tmp_path / "live_config.json")
    telegram = FailingTelegramClient()
    store = SupervisorStore(db_path=seeded_supervisor_db)
    config_manager = ConfigManager(config_path=config_path)
    now = datetime(2026, 4, 1, 10, 0, tzinfo=timezone.utc)

    service = SupervisorService(
        db_path=seeded_supervisor_db,
        config_path=config_path,
        timezone_name="UTC",
        telegram_client=telegram,
        store=store,
        config_manager=config_manager,
        report_schedules=[],
        allowed_chat_ids=["123"],
    )

    asyncio.run(service.run_once(now=now))

    with open(config_path, encoding="utf-8") as handle:
        live_config = json.load(handle)

    assert live_config["pause_new_entries"] is True
    service.close()


def test_service_records_alert_cooldown_even_when_telegram_send_fails(tmp_path):
    seeded_supervisor_db = seed_supervisor_db(tmp_path)
    config_path = str(tmp_path / "live_config.json")
    telegram = SendFailingTelegramClient()
    store = SupervisorStore(db_path=seeded_supervisor_db)
    config_manager = ConfigManager(config_path=config_path)
    now = datetime(2026, 4, 1, 10, 0, tzinfo=timezone.utc)

    service = SupervisorService(
        db_path=seeded_supervisor_db,
        config_path=config_path,
        timezone_name="UTC",
        telegram_client=telegram,
        store=store,
        config_manager=config_manager,
        report_schedules=[],
        allowed_chat_ids=["123"],
    )

    asyncio.run(service.run_once(now=now))

    recorded_keys = {
        row["alert_key"]
        for row in store.conn.execute("SELECT alert_key FROM supervisor_alerts").fetchall()
    }
    assert "pause_new_entries" in recorded_keys
    assert "kill_switch_active" in recorded_keys
    assert "supervisor_anomaly_summary" in recorded_keys
    assert store.should_emit_alert("kill_switch_active", 14400, now) is False
    service.close()


def test_service_suppresses_kill_switch_and_anomaly_alerts_during_runtime_settling(tmp_path):
    seeded_supervisor_db = seed_supervisor_db(tmp_path)
    writer = StateWriter(db_path=seeded_supervisor_db)
    writer.set_risk("runtime_settling_until_iso", (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat())
    writer.close()

    config_path = str(tmp_path / "live_config.json")
    telegram = FakeTelegramClient()
    store = SupervisorStore(db_path=seeded_supervisor_db)
    config_manager = ConfigManager(config_path=config_path)
    service = SupervisorService(
        db_path=seeded_supervisor_db,
        config_path=config_path,
        timezone_name="UTC",
        telegram_client=telegram,
        store=store,
        config_manager=config_manager,
        report_schedules=[],
        allowed_chat_ids=["123"],
    )

    now = datetime.now(timezone.utc)
    snapshot = collect_snapshot(service.state_reader, service.store)
    asyncio.run(service._maybe_send_alerts(snapshot, now))

    recorded_keys = {
        row["alert_key"]
        for row in store.conn.execute("SELECT alert_key FROM supervisor_alerts").fetchall()
    }
    assert telegram.sent_messages == []
    assert "kill_switch_active" not in recorded_keys
    assert "supervisor_anomaly_summary" not in recorded_keys
    service.close()


def test_service_registers_commands_and_help_command_lists_them(tmp_path):
    db_path = str(tmp_path / "help.db")
    writer = StateWriter(db_path=db_path)
    writer.set_stat("account_equity", 10_000.0)
    writer.close()

    config_path = str(tmp_path / "live_config.json")
    telegram = FakeTelegramClient(
        updates=[
            {
                "update_id": 1,
                "message": {
                    "chat": {"id": "42"},
                    "text": "/help",
                },
            }
        ]
    )
    store = SupervisorStore(db_path=db_path)
    config_manager = ConfigManager(config_path=config_path)
    now = datetime.now(timezone.utc)

    service = SupervisorService(
        db_path=db_path,
        config_path=config_path,
        timezone_name="UTC",
        telegram_client=telegram,
        store=store,
        config_manager=config_manager,
        report_schedules=[],
        allowed_chat_ids=["42"],
    )

    asyncio.run(service.run_once(now=now))

    assert telegram.commands
    help_messages = [message for chat_id, message in telegram.sent_messages if chat_id == "42"]
    assert any("/help - Show all supervisor commands" in message for message in help_messages)
    assert any("/approve <id> - Apply a recommendation by id" in message for message in help_messages)
    assert any("/acknowledge <symbol> - Clear startup manual review for symbol" in message for message in help_messages)
    assert any("/ack_incident <id> - Acknowledge a verified incident" in message for message in help_messages)
    service.close()


def test_service_acknowledge_command_queues_startup_recovery_ack(tmp_path):
    db_path = str(tmp_path / "acknowledge.db")
    writer = StateWriter(db_path=db_path)
    writer.set_risk_snapshot(
        {
            "trading_mode": "paper",
            "runtime_mode": "SAFE_MODE",
            "session_id": "test-session",
            "bot_started_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    writer.upsert_position(
        symbol="BTCUSDT",
        side="LONG_SPOT_SHORT_PERP",
        direction="long",
        spot_entry=100.0,
        perp_entry=100.0,
        qty=1.0,
        hedge_ratio=0.0,
        recovery_state="manual_review",
    )
    writer.flush()
    writer.close()

    config_path = str(tmp_path / "live_config.json")
    telegram = FakeTelegramClient(
        updates=[
            {
                "update_id": 1,
                "message": {
                    "chat": {"id": "42"},
                    "text": "/acknowledge BTCUSDT",
                },
            }
        ]
    )
    store = SupervisorStore(db_path=db_path)
    config_manager = ConfigManager(config_path=config_path)
    now = datetime.now(timezone.utc)

    service = SupervisorService(
        db_path=db_path,
        config_path=config_path,
        timezone_name="UTC",
        telegram_client=telegram,
        store=store,
        config_manager=config_manager,
        report_schedules=[],
        allowed_chat_ids=["42"],
    )

    asyncio.run(service.run_once(now=now))

    risk_row = store.conn.execute(
        "SELECT value FROM risk_state WHERE key = 'startup_recovery_acknowledged_symbols'"
    ).fetchone()
    assert risk_row is not None
    assert json.loads(risk_row["value"]) == ["BTCUSDT"]
    assert any("Queued startup recovery acknowledgement for: BTCUSDT." in message for _, message in telegram.sent_messages)
    service.close()


def test_service_acknowledge_command_merges_pending_symbols(tmp_path):
    db_path = str(tmp_path / "acknowledge-merge.db")
    writer = StateWriter(db_path=db_path)
    writer.set_risk_snapshot(
        {
            "trading_mode": "paper",
            "runtime_mode": "SAFE_MODE",
            "session_id": "test-session",
            "bot_started_at": datetime.now(timezone.utc).isoformat(),
            "startup_recovery_acknowledged_symbols": ["ETHUSDT"],
        }
    )
    writer.upsert_position(
        symbol="BTCUSDT",
        side="LONG_SPOT_SHORT_PERP",
        direction="long",
        spot_entry=100.0,
        perp_entry=100.0,
        qty=1.0,
        hedge_ratio=0.0,
        recovery_state="manual_review",
    )
    writer.flush()
    writer.close()

    config_path = str(tmp_path / "live_config.json")
    telegram = FakeTelegramClient(
        updates=[
            {
                "update_id": 1,
                "message": {
                    "chat": {"id": "42"},
                    "text": "/acknowledge BTCUSDT",
                },
            }
        ]
    )
    store = SupervisorStore(db_path=db_path)
    config_manager = ConfigManager(config_path=config_path)
    now = datetime.now(timezone.utc)

    service = SupervisorService(
        db_path=db_path,
        config_path=config_path,
        timezone_name="UTC",
        telegram_client=telegram,
        store=store,
        config_manager=config_manager,
        report_schedules=[],
        allowed_chat_ids=["42"],
    )

    asyncio.run(service.run_once(now=now))

    risk_row = store.conn.execute(
        "SELECT value FROM risk_state WHERE key = 'startup_recovery_acknowledged_symbols'"
    ).fetchone()
    assert risk_row is not None
    assert json.loads(risk_row["value"]) == ["ETHUSDT", "BTCUSDT"]
    service.close()


def test_service_rejects_recommendation_that_would_increase_risk(tmp_path):
    db_path = str(tmp_path / "reject-risk-increase.db")
    StateWriter(db_path=db_path).close()
    config_path = str(tmp_path / "live_config.json")
    store = SupervisorStore(db_path=db_path)
    config_manager = ConfigManager(config_path=config_path)
    now = datetime.now(timezone.utc)
    store.save_recommendations(
        [
            SupervisorRecommendation(
                recommendation_id="riskup123",
                created_at=now.isoformat(),
                report_kind="manual",
                kind="increase_notional",
                target_key="notional_per_trade",
                current_value=2_500.0,
                proposed_value=3_000.0,
                rationale="This must not bypass promotion gates.",
                expires_at=(now + timedelta(days=7)).isoformat(),
            )
        ]
    )
    telegram = FakeTelegramClient(
        updates=[
            {
                "update_id": 1,
                "message": {"chat": {"id": "42"}, "text": "/approve riskup123"},
            }
        ]
    )
    service = SupervisorService(
        db_path=db_path,
        config_path=config_path,
        timezone_name="UTC",
        telegram_client=telegram,
        store=store,
        config_manager=config_manager,
        report_schedules=[],
        allowed_chat_ids=["42"],
    )

    asyncio.run(service.run_once(now=now))

    rejected = store.get_recommendation("riskup123")
    assert rejected is not None
    assert rejected["status"] == RecommendationStatus.REJECTED.value
    assert config_manager.get("notional_per_trade") == 2_500.0
    assert any("could increase risk" in message for _, message in telegram.sent_messages)
    service.close()


def test_resume_entries_fails_closed_while_incident_or_kill_switch_exists(tmp_path):
    db_path = seed_supervisor_db(tmp_path)
    config_path = str(tmp_path / "live_config.json")
    telegram = FakeTelegramClient(
        updates=[{"update_id": 1, "message": {"chat": {"id": "42"}, "text": "/resume_entries"}}]
    )
    store = SupervisorStore(db_path=db_path)
    config_manager = ConfigManager(config_path=config_path)
    config_manager.apply_updates({"pause_new_entries": True})
    service = SupervisorService(
        db_path=db_path,
        config_path=config_path,
        timezone_name="UTC",
        telegram_client=telegram,
        store=store,
        config_manager=config_manager,
        report_schedules=[],
        allowed_chat_ids=["42"],
    )
    asyncio.run(service.run_once(now=datetime.now(timezone.utc)))
    assert config_manager.get("pause_new_entries") is True
    assert any("Cannot resume entries" in message for _, message in telegram.sent_messages)
    service.close()


def test_verified_critical_incident_can_be_acknowledged_by_exact_command(tmp_path):
    db_path = str(tmp_path / "incident-ack.db")
    writer = StateWriter(db_path=db_path)
    writer.set_stat("account_equity", 10_000.0)
    writer.set_risk_snapshot({"trading_mode": "paper", "kill_switch": False})
    writer.close()
    config_path = str(tmp_path / "live_config.json")
    telegram = FakeTelegramClient()
    store = SupervisorStore(db_path=db_path)
    service = SupervisorService(
        db_path=db_path,
        config_path=config_path,
        timezone_name="UTC",
        telegram_client=telegram,
        store=store,
        config_manager=ConfigManager(config_path=config_path),
        report_schedules=[],
        allowed_chat_ids=["42"],
    )
    incident = service.incidents.observe(
        IncidentObservation(
            incident_key="risk:kill_switch",
            category="kill_switch",
            scope_type=IncidentScope.GLOBAL,
            scope_value="",
            severity=IncidentSeverity.CRITICAL,
            owner="risk",
            recipe_id="verify_runtime_invariant",
            requires_ack=True,
        ),
        now=datetime.now(timezone.utc) - timedelta(minutes=2),
    )
    service.incidents.run_due(now=datetime.now(timezone.utc))
    telegram.updates = [
        {
            "update_id": 1,
            "message": {
                "chat": {"id": "42"},
                "text": f"/ack_incident {str(incident['incident_id'])[:8]}",
            },
        }
    ]
    asyncio.run(service.run_once(now=datetime.now(timezone.utc)))
    resolved = service.incidents.get(str(incident["incident_id"]))
    assert resolved is not None and resolved["state"] == "RESOLVED"
    assert any("acknowledged and resolved" in message for _, message in telegram.sent_messages)
    service.close()
