from unittest.mock import MagicMock, patch

import msgpack
import zmq

from bongus.ipc.execution import ExecutionClient


class TestExecutionClient:

    @patch("bongus.ipc.execution.zmq.Context")
    def test_init(self, mock_context_class):
        # Setup mock objects
        mock_context = MagicMock()
        mock_socket = MagicMock()
        mock_context_class.return_value = mock_context
        mock_context.socket.return_value = mock_socket

        endpoint = "tcp://127.0.0.1:5555"
        client = ExecutionClient(endpoint)

        # Assert context was created
        mock_context_class.assert_called_once()

        # Assert socket was created with zmq.PUSH
        mock_context.socket.assert_called_once_with(zmq.PUSH)

        # Assert socket connected to endpoint
        mock_socket.connect.assert_called_once_with(endpoint)

        assert client.endpoint == endpoint
        assert client.context == mock_context
        assert client.socket == mock_socket

    @patch("bongus.ipc.execution.zmq.Context")
    def test_send_order_intent(self, mock_context_class):
        # Setup mock objects
        mock_context = MagicMock()
        mock_socket = MagicMock()
        mock_context_class.return_value = mock_context
        mock_context.socket.return_value = mock_socket

        client = ExecutionClient()

        # Data to send
        payload = {"intent": "Enter", "symbol": "BTCUSDT", "exposure_scale": 1.0}

        # Action
        client.send_order_intent(payload)

        # Assert correct data was packed and sent with NOBLOCK flag
        expected_packed_data = msgpack.packb(payload)
        mock_socket.send.assert_called_once_with(expected_packed_data, zmq.NOBLOCK)

    @patch("bongus.ipc.execution.zmq.Context")
    def test_close(self, mock_context_class):
        # Setup mock objects
        mock_context = MagicMock()
        mock_socket = MagicMock()
        mock_context_class.return_value = mock_context
        mock_context.socket.return_value = mock_socket

        client = ExecutionClient()

        # Action
        client.close()

        # Assert socket was closed
        mock_socket.close.assert_called_once()

        # Assert context was terminated
        mock_context.term.assert_called_once()
