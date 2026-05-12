"""Generate a weekly Gemini report, persist proposals, and forward them to Telegram."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

from bongus.core.config_manager import ConfigManager
from bongus.engine.state_store import StateReader, StateWriter
from bongus.monitoring.performance_metrics import calculate_metrics

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DOTENV_PATH = _PROJECT_ROOT / ".env"
_LIVE_CONFIG_PATH = _PROJECT_ROOT / "live_config.json"

load_dotenv(_DOTENV_PATH)

config_manager = ConfigManager(config_path=_LIVE_CONFIG_PATH)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_REPORT_MODEL = os.getenv("GEMINI_REPORT_MODEL", "gemini-2.0-flash").strip()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN_BONGUS", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID_BONGUS", "").strip()


def _load_live_config() -> dict:
    if not _LIVE_CONFIG_PATH.exists():
        return {}
    with open(_LIVE_CONFIG_PATH, encoding="utf-8") as handle:
        data = json.load(handle)
    return data if isinstance(data, dict) else {}


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _collect_report_payload(reader: StateReader) -> dict:
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=7)
    trades = [
        trade
        for trade in reader.get_trades(limit=1_000)
        if (_parse_iso(trade.get("exit_time")) or datetime.min.replace(tzinfo=timezone.utc)) >= start
    ]
    execution_events = reader.get_execution_events_since(start.isoformat(), now.isoformat(), limit=2_000)
    risk = reader.get_risk()
    stats = reader.get_stats()
    metrics = calculate_metrics(reader, config=config_manager)

    return {
        "generated_at": now.isoformat(),
        "period_start": start.isoformat(),
        "period_end": now.isoformat(),
        "metrics": metrics,
        "risk": risk,
        "stats": stats,
        "live_config": config_manager.snapshot(),
        "trades": trades,
        "execution_events": execution_events,
    }


def _build_prompt(payload: dict) -> str:
    serialized = json.dumps(payload, ensure_ascii=True, indent=2)
    return (
        "You are reviewing a funding-arbitrage trading bot.\n"
        "Analyze the last 7 days of trading, identify anomalies, underperforming slots, "
        "and low-risk parameter improvements.\n"
        "Return ONLY valid JSON with this exact schema:\n"
        "{\n"
        '  "weekly_summary": "short markdown summary",\n'
        '  "proposals": [\n'
        "    {\n"
        '      "summary": "one-line recommendation",\n'
        '      "rationale": "brief reasoning",\n'
        '      "proposed_changes": {"config_key": value}\n'
        "    }\n"
        "  ]\n"
        "}\n"
        "Rules:\n"
        "- Do not include keys outside live_config.json-style runtime overrides.\n"
        "- Prefer 0 to 3 proposals.\n"
        "- Proposed changes must be conservative and human-reviewable.\n"
        "- Never suggest order execution, secrets, or autonomous actions.\n\n"
        f"INPUT:\n{serialized}\n"
    )


def _call_gemini(prompt: str) -> dict:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not configured")
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_REPORT_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )
    response = requests.post(
        url,
        json={"contents": [{"parts": [{"text": prompt}]}]},
        headers={"Content-Type": "application/json"},
        timeout=60,
    )
    response.raise_for_status()
    return response.json()


def _extract_json_payload(response: dict) -> dict:
    try:
        text = response["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("Gemini response did not contain text output") from exc

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise RuntimeError("Gemini response was not valid JSON")
        return json.loads(text[start : end + 1])


def _proposal_id(period_end: str, index: int, summary: str) -> str:
    suffix = hashlib.sha1(summary.encode("utf-8")).hexdigest()[:8]
    date_label = period_end[:10].replace("-", "")
    return f"weekly_{date_label}_{index:02d}_{suffix}"


def _send_telegram(message: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(
        url,
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        },
        timeout=20,
    ).raise_for_status()


def main() -> None:
    reader = StateReader()
    writer = StateWriter()
    try:
        payload = _collect_report_payload(reader)
        raw_response = _call_gemini(_build_prompt(payload))
        parsed = _extract_json_payload(raw_response)

        weekly_summary = str(parsed.get("weekly_summary") or "No summary returned.").strip()
        proposals = parsed.get("proposals") or []
        if not isinstance(proposals, list):
            proposals = []

        created_ids: list[str] = []
        for index, proposal in enumerate(proposals, start=1):
            if not isinstance(proposal, dict):
                continue
            summary = str(proposal.get("summary") or "").strip()
            proposed_changes = proposal.get("proposed_changes") or {}
            if not summary or not isinstance(proposed_changes, dict):
                continue
            proposal_id = _proposal_id(payload["period_end"], index, summary)
            created_ids.append(proposal_id)
            writer.record_ai_report_proposal(
                proposal_id=proposal_id,
                summary=summary,
                proposed_changes=proposed_changes,
                status="PENDING",
                report_period_start=payload["period_start"],
                report_period_end=payload["period_end"],
                raw_response=json.dumps(proposal, ensure_ascii=True),
            )

        lines = [
            "*Weekly Bongus Report*",
            weekly_summary,
            "",
            f"GO/NO-GO: `{payload['metrics'].get('go_no_go', 'UNKNOWN')}`",
            f"Validation: `{payload['metrics'].get('validation_status', 'UNKNOWN')}`",
            f"Sharpe: `{payload['metrics'].get('sharpe_ratio_annualized', 0.0):.2f}`",
            f"Max DD: `{payload['metrics'].get('max_drawdown_pct', 0.0) * 100:.2f}%`",
            f"30D Return: `{payload['metrics'].get('monthly_return_pct', 0.0) * 100:.2f}%`",
            f"Uptime: `{payload['metrics'].get('uptime_without_manual_intervention_pct', 0.0):.2f}%`",
            f"Clean run: `{payload['metrics'].get('intervention_free_days', 0.0):.1f}d`",
        ]
        blockers = list(payload["metrics"].get("validation_blockers") or [])
        if blockers:
            lines.append(f"Blockers: `{'; '.join(blockers[:2])}`")
        if created_ids:
            lines.append("")
            lines.append("*Proposals*")
            for proposal_id in created_ids:
                proposal = reader.get_ai_report_proposal(proposal_id) or {}
                lines.append(
                    f"- `{proposal_id}`: {proposal.get('summary', '')}\n"
                    f"  Reply `ja {proposal_id}` or `nein {proposal_id}`"
                )
        else:
            lines.append("")
            lines.append("No config changes proposed this week.")

        _send_telegram("\n".join(lines))
        print(json.dumps({"summary": weekly_summary, "proposal_ids": created_ids}, ensure_ascii=True))
    finally:
        reader.close()
        writer.close()


if __name__ == "__main__":
    main()
