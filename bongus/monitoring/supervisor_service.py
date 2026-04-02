from __future__ import annotations

import asyncio
import logging
import os
import sys

from dotenv import load_dotenv

if __package__ in {None, ""}:
    _BOOTSTRAP_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if _BOOTSTRAP_ROOT not in sys.path:
        sys.path.insert(0, _BOOTSTRAP_ROOT)

from bongus.supervisor.reporting import build_narrator
from bongus.supervisor.service import SupervisorService
from bongus.supervisor.telegram import TelegramBotClient

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def build_service() -> SupervisorService:
    token = os.getenv("TELEGRAM_TOKEN_BONGUS", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID_BONGUS", "")
    telegram_client = TelegramBotClient(token=token, default_chat_id=chat_id) if token else None
    return SupervisorService(telegram_client=telegram_client, narrator=build_narrator())


async def main() -> None:
    service = build_service()
    await service.run_forever()


if __name__ == "__main__":
    asyncio.run(main())
