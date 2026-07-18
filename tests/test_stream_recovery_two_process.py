from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
import socket
import subprocess
from typing import Any

from bongus.market_data.rust_data_subscriber import RustDataSubscriber


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "execution_engine" / "Cargo.toml"


def _unused_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_real_rust_python_disconnect_expiry_overflow_and_cursor_replay() -> None:
    async def campaign() -> None:
        port = _unused_local_port()
        process = subprocess.Popen(
            [
                "cargo",
                "run",
                "--quiet",
                "--locked",
                "--manifest-path",
                str(MANIFEST),
                "--",
                "--stream-recovery-harness",
                f"127.0.0.1:{port}",
            ],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        events: list[dict[str, Any]] = []
        finished = asyncio.Event()

        async def capture(event: dict[str, Any]) -> None:
            events.append(dict(event))
            if event.get("event") == "TelemetryGap":
                finished.set()

        subscriber = RustDataSubscriber(
            host="127.0.0.1",
            port=port,
            on_depth=lambda **_event: None,
        )
        for event_name in ("L2Depth", "PrivateStreamStatus", "TelemetryGap"):
            subscriber.on(event_name, capture)
        subscriber_task = asyncio.create_task(subscriber.run())
        try:
            await asyncio.wait_for(finished.wait(), timeout=30)
            return_code = await asyncio.wait_for(
                asyncio.to_thread(process.wait), timeout=10
            )
            assert return_code == 0, process.stderr.read() if process.stderr else ""
        finally:
            subscriber_task.cancel()
            with suppress(asyncio.CancelledError):
                await subscriber_task
            if process.poll() is None:
                process.terminate()
                await asyncio.to_thread(process.wait, 5)

        assert [event["event"] for event in events] == [
            "L2Depth",
            "PrivateStreamStatus",
            "PrivateStreamStatus",
            "PrivateStreamStatus",
            "PrivateStreamStatus",
            "TelemetryGap",
        ]
        assert [event["diagnostic_connection"] for event in events] == [
            1,
            2,
            3,
            3,
            4,
            4,
        ]
        private = [event for event in events if event["event"] == "PrivateStreamStatus"]
        assert [
            (event["stream_kind"], event["status"], event["cursor"])
            for event in private
        ] == [
            ("futures", "GAP", 100),
            ("futures", "BACKFILLED", 101),
            ("spot", "GAP", 200),
            ("spot", "BACKFILLED", 201),
        ]
        assert private[2]["reason"] == "listen_key_expired"
        assert events[-1]["skipped_messages"] == 37
        assert events[-1]["reason"] == "broadcast_receiver_overflow"

    asyncio.run(campaign())

