import unittest
from unittest.mock import MagicMock, patch

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
    def test_close(self, mock_context_class):
        mock_context = MagicMock()
        mock_context_class.return_value = mock_context
        mock_socket = MagicMock()
        mock_context.socket.return_value = mock_socket

        client = ExecutionClient()
        client.close()

        mock_socket.close.assert_called_once()
        mock_context.term.assert_called_once()
