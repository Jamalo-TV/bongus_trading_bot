import unittest
from unittest.mock import patch, MagicMock
import msgpack
import zmq

from bongus.ipc.execution import ExecutionClient

class TestExecutionClient(unittest.TestCase):
    @patch('bongus.ipc.execution.zmq.Context')
    def test_init(self, mock_context_class):
        mock_context = MagicMock()
        mock_context_class.return_value = mock_context
        mock_socket = MagicMock()
        mock_context.socket.return_value = mock_socket

        client = ExecutionClient(endpoint="tcp://127.0.0.1:9999")

        self.assertEqual(client.endpoint, "tcp://127.0.0.1:9999")
        mock_context_class.assert_called_once()
        mock_context.socket.assert_called_once_with(zmq.PUSH)
        mock_socket.connect.assert_called_once_with("tcp://127.0.0.1:9999")

    @patch('bongus.ipc.execution.zmq.Context')
    def test_send_order_intent(self, mock_context_class):
        mock_context = MagicMock()
        mock_context_class.return_value = mock_context
        mock_socket = MagicMock()
        mock_context.socket.return_value = mock_socket

        client = ExecutionClient()

        payload = {"intent": "Enter", "symbol": "BTCUSDT", "max_slippage_bps": 5}
        client.send_order_intent(payload)

        expected_packed = msgpack.packb(payload)
        mock_socket.send.assert_called_once_with(expected_packed, zmq.NOBLOCK)

    @patch('bongus.ipc.execution.zmq.Context')
    def test_restore_position_tracking(self, mock_context_class):
        mock_context = MagicMock()
        mock_context_class.return_value = mock_context
        mock_socket = MagicMock()
        mock_context.socket.return_value = mock_socket

        client = ExecutionClient()
        client.restore_position_tracking(
            symbol="BTCUSDT",
            direction="long",
            qty=1.25,
            spot_entry_price=100.0,
            perp_entry_price=101.0,
            spot_mark_price=102.0,
            perp_mark_price=99.0,
            spot_quantity=1.2,
            perp_quantity=1.25,
        )

        expected_packed = msgpack.packb(
            {
                "intent": "RESTORE_POSITION",
                "symbol": "BTCUSDT",
                "direction": "long",
                "quantity": 1.25,
                "spot_entry_price": 100.0,
                "perp_entry_price": 101.0,
                "spot_mark_price": 102.0,
                "perp_mark_price": 99.0,
                "spot_quantity": 1.2,
                "perp_quantity": 1.25,
            }
        )
        mock_socket.send.assert_called_once_with(expected_packed, zmq.NOBLOCK)

    @patch('bongus.ipc.execution.zmq.Context')
    def test_close(self, mock_context_class):
        mock_context = MagicMock()
        mock_context_class.return_value = mock_context
        mock_socket = MagicMock()
        mock_context.socket.return_value = mock_socket

        client = ExecutionClient()
        client.close()

        mock_socket.close.assert_called_once()
        mock_context.term.assert_called_once()
