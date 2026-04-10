from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DOTENV_PATH = os.path.join(PROJECT_ROOT, ".env")

load_dotenv(DOTENV_PATH)


def _coerce_int(value: object) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _fmt_unix(value: object) -> str:
    timestamp = _coerce_int(value)
    if timestamp is None:
        return "-"
    if timestamp <= 0:
        return "-"
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _post(base_url: str, method: str, payload: dict | None = None) -> dict:
    response = requests.post(
        f"{base_url}/{method}",
        json=payload or {},
        timeout=10,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected Telegram response for {method}: {data!r}")
    return data


def main() -> int:
    token = os.getenv("TELEGRAM_TOKEN_BONGUS", "").strip()
    if not token:
        print("TELEGRAM_TOKEN_BONGUS is not set.")
        return 1

    base_url = f"https://api.telegram.org/bot{token}"

    try:
        me = _post(base_url, "getMe")
        webhook = _post(base_url, "getWebhookInfo")
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        print(f"Telegram API request failed with HTTP {status}: {exc}")
        return 1
    except Exception as exc:
        print(f"Telegram diagnostic failed: {exc}")
        return 1

    me_result = me.get("result") or {}
    webhook_result = webhook.get("result") or {}

    summary = {
        "bot_id": me_result.get("id"),
        "bot_username": me_result.get("username"),
        "bot_can_join_groups": me_result.get("can_join_groups"),
        "webhook_url": webhook_result.get("url") or "",
        "webhook_has_custom_certificate": webhook_result.get("has_custom_certificate"),
        "webhook_pending_update_count": webhook_result.get("pending_update_count"),
        "webhook_last_error_date_utc": _fmt_unix(webhook_result.get("last_error_date")),
        "webhook_last_error_message": webhook_result.get("last_error_message") or "",
        "webhook_last_sync_error_date_utc": _fmt_unix(webhook_result.get("last_synchronization_error_date")),
        "webhook_max_connections": webhook_result.get("max_connections"),
        "webhook_ip_address": webhook_result.get("ip_address") or "",
        "webhook_allowed_updates": webhook_result.get("allowed_updates") or [],
    }

    print(json.dumps(summary, indent=2, sort_keys=True))
    print()

    webhook_url = str(webhook_result.get("url") or "").strip()
    if webhook_url:
        print("Interpretation: a webhook is currently configured for this bot token.")
        print("That webhook can cause HTTP 409 for getUpdates until it is removed.")
    else:
        print("Interpretation: no webhook is configured for this bot token.")
        print("If getUpdates still returns HTTP 409, another long-polling consumer is using the same token.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
