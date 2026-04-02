import asyncio
from unittest.mock import AsyncMock, Mock, patch

import aiohttp

from bongus.supervisor.telegram import TelegramBotClient


def _client_response_error(status: int) -> aiohttp.ClientResponseError:
    request_info = Mock()
    request_info.real_url = "https://api.telegram.org"
    return aiohttp.ClientResponseError(
        request_info=request_info,
        history=(),
        status=status,
        message=f"HTTP {status}",
    )


def test_get_updates_backs_off_after_409_and_recovers():
    client = TelegramBotClient(token="token", default_chat_id="123")
    client._command_conflict_backoff_s = 300.0

    post_mock = AsyncMock(
        side_effect=[
            _client_response_error(409),
            _client_response_error(409),
            {"result": [{"update_id": 7}]},
        ]
    )
    delete_mock = AsyncMock(return_value=True)

    with (
        patch.object(client, "_post_json", post_mock),
        patch.object(client, "_delete_webhook", delete_mock),
        patch.object(client, "_monotonic", side_effect=[1000.0, 1000.0, 1001.0, 1305.0]),
    ):
        assert asyncio.run(client.get_updates(offset=5)) == []
        assert asyncio.run(client.get_updates(offset=5)) == []
        assert asyncio.run(client.get_updates(offset=5)) == [{"update_id": 7}]

    assert delete_mock.await_count == 1
    assert post_mock.await_count == 3
    assert client._command_poll_retry_after_monotonic == 0.0


def test_set_my_commands_normalizes_payload():
    client = TelegramBotClient(token="token", default_chat_id="123")
    post_mock = AsyncMock(return_value={"ok": True})

    with patch.object(client, "_post_json", post_mock):
        asyncio.run(
            client.set_my_commands(
                [
                    {"command": "/HELP", "description": "Show all commands"},
                    {"command": "status", "description": "Show status"},
                    {"command": "status", "description": "Duplicate should be ignored"},
                ]
            )
        )

    post_mock.assert_awaited_once()
    _, called_url, called_payload = post_mock.await_args.args
    assert called_url.endswith("/setMyCommands")
    assert called_payload == {
        "commands": [
            {"command": "help", "description": "Show all commands"},
            {"command": "status", "description": "Show status"},
        ]
    }
