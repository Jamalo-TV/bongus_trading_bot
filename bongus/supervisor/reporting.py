from __future__ import annotations

import json
import os
from dataclasses import asdict
from typing import Protocol

import requests

from bongus.supervisor.models import SupervisorRecommendation, SupervisorSnapshot


class ReportNarrator(Protocol):
    def render(self, title: str, snapshot: SupervisorSnapshot, recommendations: list[SupervisorRecommendation]) -> str:
        ...


class RuleBasedNarrator:
    def render(self, title: str, snapshot: SupervisorSnapshot, recommendations: list[SupervisorRecommendation]) -> str:
        lines = [
            title,
            f"Regime: {snapshot.regime}",
            f"Net PnL: ${snapshot.total_pnl_usd:,.2f} | Drawdown: {snapshot.drawdown_pct:.1%}",
            f"Exposure: ${snapshot.gross_exposure_usd:,.0f} / ${snapshot.max_gross_exposure_usd:,.0f}",
            f"Funding vs costs: ${snapshot.total_funding_usd:,.2f} / ${snapshot.total_execution_cost_usd:,.2f}",
            f"Trades: {snapshot.trade_count} | Win rate: {snapshot.win_rate:.1%} | Open positions: {snapshot.open_positions}",
        ]
        if snapshot.anomalies:
            lines.append("Anomalies:")
            lines.extend(f"- {item}" for item in snapshot.anomalies[:4])
        else:
            lines.append("Anomalies: none above threshold.")

        if recommendations:
            lines.append("Pending approvals:")
            for rec in recommendations:
                lines.append(
                    f"- {rec.recommendation_id}: {rec.target_key} {rec.current_value} -> {rec.proposed_value}"
                )
                lines.append(f"  Why: {rec.rationale}")
        else:
            lines.append("Pending approvals: none. Default action is no change.")

        lines.append("Commands: /status, /pending, /approve <id>, /reject <id>, /pause_entries, /resume_entries")
        return "\n".join(lines)


class OpenAIReportNarrator:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        fallback: ReportNarrator | None = None,
    ) -> None:
        self.api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        self.model = model or os.getenv("SUPERVISOR_LLM_MODEL", "gpt-5.4-mini")
        self.fallback = fallback or RuleBasedNarrator()

    def render(self, title: str, snapshot: SupervisorSnapshot, recommendations: list[SupervisorRecommendation]) -> str:
        if not self.api_key:
            return self.fallback.render(title, snapshot, recommendations)

        prompt_payload = {
            "title": title,
            "snapshot": asdict(snapshot),
            "recommendations": [asdict(rec) for rec in recommendations],
            "rules": [
                "Never invent trades or values.",
                "Do not propose leverage changes.",
                "Do not suggest autonomous exits.",
                "Keep the report concise and operator-focused.",
            ],
        }
        payload = {
            "model": self.model,
            "input": [
                {
                    "role": "system",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "You are a trading supervisor narrator. Explain deterministic metrics clearly and cautiously. You are not allowed to invent recommendations or alter actions.",
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": json.dumps(prompt_payload, sort_keys=True),
                        }
                    ],
                },
            ],
        }
        try:
            response = requests.post(
                "https://api.openai.com/v1/responses",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
            text = data.get("output_text")
            if isinstance(text, str) and text.strip():
                return text.strip()

            for item in data.get("output", []):
                for content in item.get("content", []):
                    if content.get("type") == "output_text" and content.get("text"):
                        return str(content["text"]).strip()
        except Exception:
            return self.fallback.render(title, snapshot, recommendations)

        return self.fallback.render(title, snapshot, recommendations)


def build_narrator() -> ReportNarrator:
    if os.getenv("SUPERVISOR_USE_OPENAI", "").lower() in {"1", "true", "yes"}:
        return OpenAIReportNarrator()
    return RuleBasedNarrator()
