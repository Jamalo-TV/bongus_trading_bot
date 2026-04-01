from __future__ import annotations

from typing import Any, Protocol

import aiohttp


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
            response = await self._post_json(session, f"{self.base_url}/getUpdates", payload)
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


def normalize_command(text: str) -> tuple[str, list[str]]:
    command_text = (text or "").strip()
    if not command_text:
        return "", []

    parts = command_text.split()
    command = parts[0].split("@", 1)[0].lower()
    return command, parts[1:]
