import asyncio
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch, MagicMock

from bongus.ipc.telemetry import TelemetryClient

class TestTelemetryClient(IsolatedAsyncioTestCase):

    def setUp(self):
        self.client = TelemetryClient(host='127.0.0.1', port=9000)

    @patch('asyncio.open_connection', new_callable=AsyncMock)
    async def test_stream_events_happy_path(self, mock_open_connection):
        # Mock the reader
        mock_reader = AsyncMock()
        mock_writer = MagicMock()
        mock_open_connection.return_value = (mock_reader, mock_writer)

        # Reader yields a valid JSON line then we break the loop using CancelledError
        # CancelledError inherits from BaseException, so it bypasses `except Exception:`
        mock_reader.readline.side_effect = [
            b'{"event": "test", "value": 123}\n',
            asyncio.CancelledError()
        ]

        gen = self.client.stream_events()

        event = await anext(gen)
        self.assertEqual(event, {"event": "test", "value": 123})

        # Test that the exception propagates out
        with self.assertRaises(asyncio.CancelledError):
            await anext(gen)

    @patch('asyncio.sleep', new_callable=AsyncMock)
    @patch('asyncio.open_connection', new_callable=AsyncMock)
    async def test_stream_events_remote_closes_stream(self, mock_open_connection, mock_sleep):
        mock_reader = AsyncMock()
        mock_writer = MagicMock()

        # Reader yields empty line (remote close)
        mock_reader.readline.return_value = b''

        # The inner loop will break, and the outer loop will try to reconnect.
        mock_open_connection.side_effect = [
            (mock_reader, mock_writer),
            asyncio.CancelledError()
        ]

        gen = self.client.stream_events()

        with self.assertRaises(asyncio.CancelledError):
            await anext(gen)

        self.assertEqual(mock_open_connection.call_count, 2)
        mock_sleep.assert_not_called()

    @patch('asyncio.open_connection', new_callable=AsyncMock)
    async def test_stream_events_json_decode_error(self, mock_open_connection):
        mock_reader = AsyncMock()
        mock_writer = MagicMock()
        mock_open_connection.return_value = (mock_reader, mock_writer)

        # Reader yields invalid JSON, then valid JSON, then terminates
        mock_reader.readline.side_effect = [
            b'invalid json\n',
            b'{"event": "valid"}\n',
            asyncio.CancelledError()
        ]

        gen = self.client.stream_events()

        event = await anext(gen)
        self.assertEqual(event, {"event": "valid"})
        self.assertEqual(mock_reader.readline.call_count, 2)

    @patch('asyncio.sleep', new_callable=AsyncMock)
    @patch('asyncio.open_connection', new_callable=AsyncMock)
    async def test_stream_events_connection_refused(self, mock_open_connection, mock_sleep):
        # The first time we connect, it raises ConnectionRefusedError
        # The sleep breaks the loop by raising CancelledError
        mock_open_connection.side_effect = ConnectionRefusedError("Connection refused")
        mock_sleep.side_effect = asyncio.CancelledError()

        gen = self.client.stream_events()

        with self.assertRaises(asyncio.CancelledError):
            await anext(gen)

        mock_sleep.assert_called_once_with(2)
        self.assertEqual(mock_open_connection.call_count, 1)

    @patch('asyncio.sleep', new_callable=AsyncMock)
    @patch('asyncio.open_connection', new_callable=AsyncMock)
    async def test_stream_events_unexpected_error(self, mock_open_connection, mock_sleep):
        # The first time we connect, it raises some random Exception
        # The sleep breaks the loop by raising CancelledError
        mock_open_connection.side_effect = Exception("Unexpected Error")
        mock_sleep.side_effect = asyncio.CancelledError()

        gen = self.client.stream_events()

        with self.assertRaises(asyncio.CancelledError):
            await anext(gen)

        mock_sleep.assert_called_once_with(2)
        self.assertEqual(mock_open_connection.call_count, 1)
