from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from dataclasses import asdict
from datetime import datetime, time, timedelta, timezone
from typing import Iterable
from zoneinfo import ZoneInfo

from bongus.core.config_manager import ConfigManager
from bongus.engine.state_store import StateReader
from bongus.supervisor.core import build_recommendations, collect_snapshot, snapshot_to_dict
from bongus.supervisor.models import (
    AlertSeverity,
    RecommendationStatus,
    ReportKind,
    ReportSchedule,
)
from bongus.supervisor.reporting import ReportNarrator, build_narrator
from bongus.supervisor.store import SupervisorStore
from bongus.supervisor.telegram import TelegramBotClient, TelegramClientProtocol, normalize_command

logger = logging.getLogger(__name__)


SUPERVISOR_TELEGRAM_COMMANDS: tuple[dict[str, str], ...] = (
    {"command": "help", "description": "Show all supervisor commands"},
    {"command": "status", "description": "Show the current supervisor status"},
    {"command": "pending", "description": "List pending recommendations"},
    {"command": "approve", "description": "Apply a recommendation by id"},
    {"command": "reject", "description": "Reject a recommendation by id"},
    {"command": "acknowledge", "description": "Clear startup manual review for symbol"},
    {"command": "pause_entries", "description": "Pause new entries immediately"},
    {"command": "resume_entries", "description": "Resume new entries"},
    {"command": "report", "description": "Send a fresh manual supervisor report"},
    {"command": "start", "description": "Show the help message"},
)


class SupervisorService:
    def __init__(
        self,
        db_path: str = "state.db",
        config_path: str = "live_config.json",
        timezone_name: str | None = None,
        telegram_client: TelegramClientProtocol | None = None,
        narrator: ReportNarrator | None = None,
        state_reader: StateReader | None = None,
        store: SupervisorStore | None = None,
        config_manager: ConfigManager | None = None,
        report_schedules: Iterable[ReportSchedule] | None = None,
        allowed_chat_ids: Iterable[str] | None = None,
    ) -> None:
        self.state_reader = state_reader or StateReader(db_path=db_path)
        self.store = store or SupervisorStore(db_path=db_path)
        self.config_manager = config_manager or ConfigManager(config_path=config_path)
        self.tz = ZoneInfo(timezone_name or os.getenv("SUPERVISOR_TIMEZONE", "Europe/Zurich"))
        self.telegram_client = telegram_client
        self.narrator = narrator or build_narrator()
        self.owns_reader = state_reader is None
        self.owns_store = store is None
        self.owns_config = config_manager is None
        self.monitor_interval_seconds = int(os.getenv("SUPERVISOR_MONITOR_INTERVAL_SECONDS", "60"))
        self.telegram_offset = self._load_offset()
        chat_ids = list(self._default_allowed_chat_ids() if allowed_chat_ids is None else allowed_chat_ids)
        self.allowed_chat_ids = {str(chat_id) for chat_id in chat_ids if str(chat_id).strip()}
        self._telegram_commands_synced = False
        self.report_schedules = list(
            [
                ReportSchedule(ReportKind.MIDWEEK.value, weekday=2, hour=12, minute=0, title="Bongus Midweek Report"),
                ReportSchedule(ReportKind.WEEKLY.value, weekday=6, hour=10, minute=0, title="Bongus Weekly Report"),
            ]
            if report_schedules is None
            else report_schedules
        )

    async def run_forever(self) -> None:
        self.config_manager.start_watching()
        try:
            while True:
                try:
                    await self.run_once()
                except sqlite3.OperationalError as exc:
                    logger.warning(
                        "Supervisor DB temporarily unavailable (will retry in %ds): %s",
                        self.monitor_interval_seconds,
                        exc,
                    )
                    await asyncio.sleep(self.monitor_interval_seconds)
                except Exception as exc:
                    logger.exception(
                        "Supervisor loop failed unexpectedly (will retry in %ds): %s",
                        self.monitor_interval_seconds,
                        exc,
                    )
                    await asyncio.sleep(self.monitor_interval_seconds)
        finally:
            self.close()

    async def run_once(self, now: datetime | None = None) -> None:
        current_utc = now or datetime.now(timezone.utc)
        await self._sync_telegram_commands()
        self.store.purge_expired_recommendations(current_utc.isoformat())
        snapshot = collect_snapshot(self.state_reader, self.store)

        await self._maybe_autopause(snapshot, current_utc)
        await self._maybe_send_alerts(snapshot, current_utc)

        for schedule in self.report_schedules:
            await self._maybe_send_scheduled_report(schedule, snapshot, current_utc)

        await self._process_telegram_commands(current_utc)

        if now is None:
            await asyncio.sleep(self.monitor_interval_seconds)

    def close(self) -> None:
        if self.owns_reader:
            self.state_reader.close()
        if self.owns_store:
            self.store.close()
        if self.owns_config:
            self.config_manager.stop_watching()

    async def _send_message_safely(self, message: str, *, chat_id: str | None = None) -> bool:
        if not self.telegram_client:
            return False
        try:
            await self._telegram_client().send_message(message, chat_id=chat_id)
            return True
        except Exception as exc:
            logger.exception("Telegram send failed: %s", exc)
            return False

    def _write_risk_snapshot(self, snapshot: dict[str, object], *, recorded_at: datetime | None = None) -> None:
        now_iso = (recorded_at or datetime.now(timezone.utc)).isoformat()
        rows = [
            (key, value if isinstance(value, str) else json.dumps(value, sort_keys=True), now_iso)
            for key, value in snapshot.items()
        ]
        self.store.conn.executemany(
            """
            INSERT INTO risk_state (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            rows,
        )
        self.store.conn.commit()

    @staticmethod
    def _normalized_symbols(values: object) -> list[str]:
        raw_values: list[str] = []
        if isinstance(values, str):
            raw_values = [item.strip() for item in values.replace(",", " ").split()]
        elif isinstance(values, (list, tuple, set)):
            raw_values = [str(item).strip() for item in values]

        normalized: list[str] = []
        seen: set[str] = set()
        for raw_symbol in raw_values:
            symbol = str(raw_symbol or "").strip().upper()
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            normalized.append(symbol)
        return normalized

    async def _maybe_autopause(self, snapshot, now: datetime) -> None:
        if snapshot.regime not in {"STRESS", "HALT_NEW_ENTRIES"} and not snapshot.kill_switch:
            return
        if bool(self.config_manager.get("pause_new_entries")):
            return

        self.config_manager.apply_updates({"pause_new_entries": True})
        if self.telegram_client and self.store.should_emit_alert("pause_new_entries", 900, now):
            trigger = snapshot.anomalies[0] if snapshot.anomalies else "Supervisor regime escalated."
            message = (
                "Supervisor action: new entries were paused automatically.\n"
                f"Reason: regime={snapshot.regime}, drawdown={snapshot.drawdown_pct:.1%}, kill_switch={snapshot.kill_switch}.\n"
                f"Trigger: {trigger}"
            )
            self.store.record_alert("pause_new_entries", AlertSeverity.WARNING.value, message, snapshot_to_dict(snapshot))
            await self._send_message_safely(message)

    async def _maybe_send_alerts(self, snapshot, now: datetime) -> None:
        if not self.telegram_client:
            return

        risk = self.state_reader.get_risk()
        settling_iso = str(risk.get("runtime_settling_until_iso") or "")
        if settling_iso:
            try:
                settling_until = datetime.fromisoformat(settling_iso.replace("Z", "+00:00"))
                if settling_until.tzinfo is None:
                    settling_until = settling_until.replace(tzinfo=timezone.utc)
                if now.astimezone(timezone.utc) < settling_until.astimezone(timezone.utc):
                    return
            except ValueError:
                pass

        if snapshot.kill_switch and self.store.should_emit_alert("kill_switch_active", 600, now):
            message = "Critical: Bongus kill switch is active. Use /status to inspect and /resume_entries only after review."
            self.store.record_alert("kill_switch_active", AlertSeverity.CRITICAL.value, message, snapshot_to_dict(snapshot))
            await self._send_message_safely(message)

        if snapshot.anomalies and self.store.should_emit_alert("supervisor_anomaly_summary", 3600, now):
            top_items = "\n".join(f"- {item}" for item in snapshot.anomalies[:3])
            message = (
                f"Supervisor anomaly summary ({snapshot.regime}):\n"
                f"{top_items}\n"
                "Commands: /status or /pending"
            )
            self.store.record_alert(
                "supervisor_anomaly_summary",
                AlertSeverity.WARNING.value,
                message,
                snapshot_to_dict(snapshot),
            )
            await self._send_message_safely(message)

        await self._maybe_alert_stale_pending_intent(now)

    async def _maybe_alert_stale_pending_intent(self, now: datetime) -> None:
        risk = self.state_reader.get_risk()
        runtime_mode = str(risk.get("runtime_mode") or "")
        safe_mode_reason = str(risk.get("safe_mode_reason") or "")
        if runtime_mode != "SAFE_MODE" or "stale_pending_intent" not in safe_mode_reason:
            return

        raw_changed_at = risk.get("last_runtime_mode_change")
        try:
            mode_changed_at = datetime.fromisoformat(str(raw_changed_at).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return

        elapsed_seconds = (now - mode_changed_at).total_seconds()
        if elapsed_seconds < 600:
            return

        if not self.store.should_emit_alert("stale_pending_intent_safe_mode", 600, now):
            return

        enter_symbols: list[str] = risk.get("stale_pending_enter_symbols") or []
        exit_symbols: list[str] = risk.get("stale_pending_exit_symbols") or []
        stuck_symbols = sorted(set(enter_symbols) | set(exit_symbols))
        symbols_str = ", ".join(stuck_symbols) if stuck_symbols else "unknown"
        elapsed_minutes = elapsed_seconds / 60

        message = (
            f"SAFE_MODE: stale_pending_intent active for {elapsed_minutes:.0f}m.\n"
            f"Stuck symbols: {symbols_str}\n"
            "Bot cannot open new positions until resolved. Check trader logs."
        )
        self.store.record_alert(
            "stale_pending_intent_safe_mode",
            AlertSeverity.WARNING.value,
            message,
            {
                "safe_mode_reason": safe_mode_reason,
                "stuck_symbols": stuck_symbols,
                "elapsed_seconds": elapsed_seconds,
            },
        )
        await self._send_message_safely(message)

    async def _maybe_send_scheduled_report(self, schedule: ReportSchedule, snapshot, now: datetime) -> None:
        scheduled_for = self._scheduled_slot(schedule, now)
        if now.astimezone(self.tz) < scheduled_for:
            return

        slot_key = scheduled_for.astimezone(timezone.utc).isoformat()
        if self.store.has_report_for_slot(schedule.kind, slot_key):
            return

        recommendations = build_recommendations(snapshot, self.config_manager.snapshot(), schedule.kind)
        self.store.save_recommendations(recommendations)
        title = f"{schedule.title} ({scheduled_for.strftime('%A %H:%M %Z')})"
        message = self.narrator.render(title, snapshot, recommendations)
        payload = {
            "snapshot": snapshot_to_dict(snapshot),
            "recommendations": [asdict(rec) for rec in recommendations],
        }
        self.store.record_report(schedule.kind, slot_key, title, message, payload)
        if self.telegram_client:
            await self._send_message_safely(message)

    async def _process_telegram_commands(self, now: datetime) -> None:
        if not self.telegram_client:
            return
        client = self._telegram_client()

        try:
            updates = await client.get_updates(offset=self.telegram_offset, timeout=1)
        except Exception as exc:
            logger.warning("Supervisor could not poll Telegram commands: %s", exc)
            return

        for update in updates:
            update_id = int(update.get("update_id", 0))
            self.telegram_offset = max(self.telegram_offset, update_id + 1)
            self.store.set_runtime_value("telegram_update_offset", str(self.telegram_offset))

            message = update.get("message") or {}
            chat = message.get("chat") or {}
            chat_id = str(chat.get("id", ""))
            text = str(message.get("text", "") or "")
            if not text.startswith("/"):
                continue
            if self.allowed_chat_ids and chat_id not in self.allowed_chat_ids:
                await client.send_message("Unauthorized chat id.", chat_id=chat_id)
                continue
            await self._handle_command(chat_id, text, now)

    async def _handle_command(self, chat_id: str, text: str, now: datetime) -> None:
        command, args = normalize_command(text)

        if command in {"/help", "/start"}:
            await self._telegram_client().send_message(
                self._help_text(),
                chat_id=chat_id,
            )
            return

        if command == "/status":
            snapshot = collect_snapshot(self.state_reader, self.store)
            summary = self.narrator.render("Bongus Supervisor Status", snapshot, [])
            await self._telegram_client().send_message(summary, chat_id=chat_id)
            return

        if command in {"/pending", "/recommendations"}:
            pending = self.store.list_recommendations(statuses=[RecommendationStatus.PENDING.value], limit=10)
            if not pending:
                await self._telegram_client().send_message("No pending recommendations.", chat_id=chat_id)
                return
            lines = ["Pending recommendations:"]
            for rec in pending:
                lines.append(
                    f"- {rec['recommendation_id']}: {rec['target_key']} {rec['current_value']} -> {rec['proposed_value']}"
                )
            await self._telegram_client().send_message("\n".join(lines), chat_id=chat_id)
            return

        if command == "/approve" and args:
            await self._approve_recommendation(chat_id, args[0])
            return

        if command == "/reject" and args:
            found = self.store.update_recommendation_status(args[0], RecommendationStatus.REJECTED.value)
            response = "Recommendation rejected." if found else "Recommendation not found."
            await self._telegram_client().send_message(response, chat_id=chat_id)
            return

        if command == "/acknowledge":
            if not args:
                await self._telegram_client().send_message(
                    "Usage: /acknowledge <symbol>",
                    chat_id=chat_id,
                )
                return
            await self._acknowledge_startup_recovery(chat_id, args, now)
            return

        if command == "/pause_entries":
            self.config_manager.apply_updates({"pause_new_entries": True})
            await self._telegram_client().send_message("New entries paused.", chat_id=chat_id)
            return

        if command == "/resume_entries":
            self.config_manager.apply_updates({"pause_new_entries": False})
            await self._telegram_client().send_message("New entries resumed.", chat_id=chat_id)
            return

        if command == "/report":
            snapshot = collect_snapshot(self.state_reader, self.store)
            recommendations = build_recommendations(snapshot, self.config_manager.snapshot(), ReportKind.MANUAL.value)
            self.store.save_recommendations(recommendations)
            title = "Bongus Manual Report"
            message = self.narrator.render(title, snapshot, recommendations)
            await self._telegram_client().send_message(message, chat_id=chat_id)
            return

        await self._telegram_client().send_message("Unknown command. Try /help.", chat_id=chat_id)

    async def _sync_telegram_commands(self) -> None:
        if not self.telegram_client or self._telegram_commands_synced:
            return
        try:
            await self._telegram_client().set_my_commands(list(SUPERVISOR_TELEGRAM_COMMANDS))
        except Exception as exc:
            logger.warning("Supervisor could not register Telegram commands: %s", exc)
            return
        self._telegram_commands_synced = True

    def _help_text(self) -> str:
        lines = ["Supervisor commands:"]
        for item in SUPERVISOR_TELEGRAM_COMMANDS:
            command = item["command"]
            description = item["description"]
            if command in {"approve", "reject"}:
                lines.append(f"/{command} <id> - {description}")
            elif command == "acknowledge":
                lines.append(f"/{command} <symbol> - {description}")
            else:
                lines.append(f"/{command} - {description}")
        return "\n".join(lines)

    async def _approve_recommendation(self, chat_id: str, recommendation_id: str) -> None:
        recommendation = self.store.get_recommendation(recommendation_id)
        if not recommendation:
            await self._telegram_client().send_message("Recommendation not found.", chat_id=chat_id)
            return

        if recommendation["status"] != RecommendationStatus.PENDING.value:
            await self._telegram_client().send_message(
                f"Recommendation is already {recommendation['status'].lower()}.",
                chat_id=chat_id,
            )
            return

        self.config_manager.apply_updates({recommendation["target_key"]: recommendation["proposed_value"]})
        self.store.update_recommendation_status(recommendation_id, RecommendationStatus.APPLIED.value)
        await self._telegram_client().send_message(
            (
                f"Applied {recommendation_id}: {recommendation['target_key']} "
                f"{recommendation['current_value']} -> {recommendation['proposed_value']}"
            ),
            chat_id=chat_id,
        )

    async def _acknowledge_startup_recovery(
        self,
        chat_id: str,
        symbols: list[str],
        now: datetime,
    ) -> None:
        normalized = self._normalized_symbols(symbols)

        positions = {
            str(row.get("symbol", "")).upper(): row
            for row in self.state_reader.get_positions_for_current_mode()
            if row.get("symbol")
        }
        eligible: list[str] = []
        skipped: list[str] = []
        for symbol in normalized:
            row = positions.get(symbol)
            if row is None:
                skipped.append(f"{symbol}: no open position")
                continue
            if str(row.get("recovery_state") or "").strip().lower() != "manual_review":
                skipped.append(f"{symbol}: not awaiting startup manual review")
                continue
            if str(row.get("direction") or "").strip().lower() != "long":
                skipped.append(f"{symbol}: unsupported recovered direction still needs manual handling")
                continue
            eligible.append(symbol)

        if eligible:
            existing_pending = self._normalized_symbols(
                self.state_reader.get_risk().get("startup_recovery_acknowledged_symbols")
            )
            pending_symbols = self._normalized_symbols([*existing_pending, *eligible])
            self._write_risk_snapshot(
                {
                    "startup_recovery_acknowledged_symbols": pending_symbols,
                    "startup_recovery_acknowledged_at": now.isoformat(),
                    "startup_recovery_acknowledged_by": f"telegram:{chat_id}",
                },
                recorded_at=now,
            )

        lines = []
        if eligible:
            lines.append(
                f"Queued startup recovery acknowledgement for: {', '.join(eligible)}."
            )
            lines.append("Trader will clear the manual-review block on its next cycle.")
        else:
            lines.append("No startup manual-review symbols were queued.")
        if skipped:
            lines.append("Skipped: " + "; ".join(skipped))
        await self._telegram_client().send_message("\n".join(lines), chat_id=chat_id)

    def _scheduled_slot(self, schedule: ReportSchedule, now: datetime) -> datetime:
        local_now = now.astimezone(self.tz)
        scheduled_today = datetime.combine(local_now.date(), time(schedule.hour, schedule.minute), tzinfo=self.tz)
        days_back = (local_now.weekday() - schedule.weekday) % 7
        slot = scheduled_today - timedelta(days=days_back)
        if local_now < slot:
            slot -= timedelta(days=7)
        return slot

    def _load_offset(self) -> int:
        raw = self.store.get_runtime_value("telegram_update_offset", "0")
        try:
            return int(raw)
        except ValueError:
            return 0

    def _default_allowed_chat_ids(self) -> list[str]:
        configured = os.getenv("TELEGRAM_SUPERVISOR_ALLOWED_CHAT_IDS", "")
        if configured.strip():
            return [item.strip() for item in configured.split(",") if item.strip()]
        default_chat_id = os.getenv("TELEGRAM_CHAT_ID_BONGUS", "")
        return [default_chat_id] if default_chat_id else []

    def _telegram_client(self) -> TelegramClientProtocol:
        assert self.telegram_client is not None
        return self.telegram_client
