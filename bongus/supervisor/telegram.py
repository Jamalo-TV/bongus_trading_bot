from __future__ import annotations

import logging
from typing import Any, Protocol

import aiohttp

logger = logging.getLogger(__name__)


class TelegramClientProtocol(Protocol):
    async def send_message(self, message: str, chat_id: str | None = None) -> None:
        ...

    async def get_updates(self, offset: int | None = None, timeout: int = 1) -> list[dict[str, Any]]:
        ...


class TelegramBotClient:
    def __init__(self, token: str, default_chat_id: str | None = None) -> None:
        self.token = token
        self.default_chat_id = str(default_chat_id) if default_chat_id else None
        self.base_url = f"https://api.telegram.org/bot{token}"
        self._attempted_webhook_reset = False

    async def send_message(self, message: str, chat_id: str | None = None) -> None:
        target = str(chat_id or self.default_chat_id or "")
        if not self.token or not target:
            return

        async with aiohttp.ClientSession() as session:
            await self._post_json(
                session,
                f"{self.base_url}/sendMessage",
                {"chat_id": target, "text": message},
            )

    async def get_updates(self, offset: int | None = None, timeout: int = 1) -> list[dict[str, Any]]:
        if not self.token:
            return []
        payload: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            payload["offset"] = offset

        async with aiohttp.ClientSession() as session:
            try:
                response = await self._post_json(session, f"{self.base_url}/getUpdates", payload)
            except aiohttp.ClientResponseError as exc:
                if exc.status != 409:
                    raise
                if not self._attempted_webhook_reset and await self._delete_webhook(session):
                    self._attempted_webhook_reset = True
                    response = await self._post_json(session, f"{self.base_url}/getUpdates", payload)
                else:
                    logger.warning(
                        "Telegram getUpdates returned HTTP 409. "
                        "Another consumer or webhook is active; supervisor commands are temporarily unavailable."
                    )
                    return []
            return response.get("result", []) if isinstance(response, dict) else []

    async def _post_json(
        self,
        session: aiohttp.ClientSession,
        url: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as response:
            response.raise_for_status()
            data = await response.json()
            return data if isinstance(data, dict) else {}

    async def _delete_webhook(self, session: aiohttp.ClientSession) -> bool:
        try:
            await self._post_json(
                session,
                f"{self.base_url}/deleteWebhook",
                {"drop_pending_updates": False},
            )
            logger.warning("Telegram webhook was cleared after an HTTP 409 conflict on getUpdates.")
            return True
        except Exception as exc:
            logger.warning("Telegram webhook reset failed after HTTP 409 conflict: %s", exc)
            return False


def normalize_command(text: str) -> tuple[str, list[str]]:
    command_text = (text or "").strip()
    if not command_text:
        return "", []

    parts = command_text.split()
    command = parts[0].split("@", 1)[0].lower()
    return command, parts[1:]
