from __future__ import annotations

from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "execution_engine" / "Cargo.toml"


def test_python_exchange_emulator_recovers_every_accepted_order_timeout() -> None:
    observed: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"posts": 0, "gets": 0, "post_path": "", "params": {}}
    )
    lock = threading.Lock()

    class AcceptedTimeoutExchange(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def _respond(self, client_order_id: str) -> None:
            body = json.dumps(
                {
                    "symbol": "BTCUSDT",
                    "orderId": abs(hash(client_order_id)) % 1_000_000 + 1,
                    "clientOrderId": client_order_id,
                    "status": "NEW",
                    "executedQty": "0",
                },
                separators=(",", ":"),
            ).encode("utf-8")
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                # Expected: the Rust client's bounded timeout closes the first
                # POST response after the exchange emulator has committed it.
                pass

        def do_POST(self) -> None:
            content_length = int(self.headers.get("Content-Length", "0"))
            params = parse_qs(urlparse(self.path).query, keep_blank_values=True)
            body_params = parse_qs(
                self.rfile.read(content_length).decode("utf-8"),
                keep_blank_values=True,
            )
            params.update(body_params)
            client_order_id = params["newClientOrderId"][0]
            with lock:
                state = observed[client_order_id]
                state["posts"] += 1
                state["post_path"] = urlparse(self.path).path
                state["params"] = params
            time.sleep(0.2)
            self._respond(client_order_id)

        def do_GET(self) -> None:
            query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
            client_order_id = query["origClientOrderId"][0]
            with lock:
                state = observed[client_order_id]
                state["gets"] += 1
            self._respond(client_order_id)

    server = ThreadingHTTPServer(("127.0.0.1", 0), AcceptedTimeoutExchange)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        completed = subprocess.run(
            [
                "cargo",
                "run",
                "--quiet",
                "--locked",
                "--manifest-path",
                str(MANIFEST),
                "--",
                "--rest-timeout-harness",
                f"http://127.0.0.1:{server.server_port}",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        )
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)

    payload = json.loads(completed.stdout)
    outcomes = payload["outcomes"]
    assert {outcome["name"] for outcome in outcomes} == {
        "spot_limit",
        "futures_limit_entry",
        "futures_limit_exit",
        "spot_market",
        "futures_market_entry",
        "futures_market_exit",
    }
    assert all(outcome["recovered_after_ambiguous_submit"] for outcome in outcomes)
    assert not any(outcome["retried_after_negative_proof"] for outcome in outcomes)

    expected = {
        "bngs_timeout_spot_limit": ("/api/v3/order", "LIMIT_MAKER", False),
        "bngs_timeout_futures_limit_entry": ("/fapi/v1/order", "LIMIT", False),
        "bngs_timeout_futures_limit_exit": ("/fapi/v1/order", "LIMIT", True),
        "bngs_timeout_spot_market": ("/api/v3/order", "MARKET", False),
        "bngs_timeout_futures_market_entry": ("/fapi/v1/order", "MARKET", False),
        "bngs_timeout_futures_market_exit": ("/fapi/v1/order", "MARKET", True),
    }
    assert set(observed) == set(expected)
    for client_order_id, (path, order_type, reduce_only) in expected.items():
        state = observed[client_order_id]
        params = state["params"]
        assert state["posts"] == 1
        assert state["gets"] == 1
        assert state["post_path"] == path
        assert params["type"] == [order_type]
        assert (params.get("reduceOnly") == ["true"]) is reduce_only
