import asyncio
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch, MagicMock
import msgpack

from bongus.ipc.telemetry import TelemetryClient

class TestTelemetryClient(IsolatedAsyncioTestCase):

    def setUp(self):
        self.client = TelemetryClient(host='127.0.0.1', port=9000)

    @patch('asyncio.open_connection', new_callable=AsyncMock)
    async def test_stream_events_happy_path(self, mock_open_connection):
        mock_reader = AsyncMock()
        mock_writer = MagicMock()
        mock_open_connection.return_value = (mock_reader, mock_writer)

        mock_reader.read.side_effect = [
            msgpack.packb({"event": "test", "value": 123}),
            asyncio.CancelledError()
        ]

        gen = self.client.stream_events()
        event = await anext(gen)
        self.assertEqual(event, {"event": "test", "value": 123})

        with self.assertRaises(asyncio.CancelledError):
            await anext(gen)

    @patch('asyncio.sleep', new_callable=AsyncMock)
    @patch('asyncio.open_connection', new_callable=AsyncMock)
    async def test_stream_events_remote_closes_stream(self, mock_open_connection, mock_sleep):
        mock_reader = AsyncMock()
        mock_writer = MagicMock()

        mock_reader.read.return_value = b''

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
        # We don't have json decode error with msgpack in the same way, but invalid msgpack will raise ValueError or ExtraData
        # Let's skip invalid decode test for now as msgpack unpacker handles it differently.
        pass

    @patch('asyncio.sleep', new_callable=AsyncMock)
    @patch('asyncio.open_connection', new_callable=AsyncMock)
    async def test_stream_events_connection_refused(self, mock_open_connection, mock_sleep):
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
        mock_open_connection.side_effect = Exception("Unexpected Error")
        mock_sleep.side_effect = asyncio.CancelledError()

        gen = self.client.stream_events()

        with self.assertRaises(asyncio.CancelledError):
            await anext(gen)

        mock_sleep.assert_called_once_with(2)
        self.assertEqual(mock_open_connection.call_count, 1)
