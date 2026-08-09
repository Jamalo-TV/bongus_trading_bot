from pathlib import Path

import pytest

import scripts.live_trader_v2 as live_trader


def test_heartbeat_replace_retries_transient_windows_reader_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "heartbeat.tmp"
    destination = tmp_path / "heartbeat.json"
    source.write_text("new\n", encoding="utf-8")
    destination.write_text("old\n", encoding="utf-8")
    real_replace = live_trader.os.replace
    calls = 0

    def flaky_replace(src: str, dst: str) -> None:
        nonlocal calls
        calls += 1
        if calls < 3:
            assert destination.read_text(encoding="utf-8") == "old\n"
            raise PermissionError("simulated Windows sharing violation")
        real_replace(src, dst)

    monkeypatch.setattr(live_trader.os, "replace", flaky_replace)
    monkeypatch.setattr(live_trader.time, "sleep", lambda _delay: None)

    live_trader._replace_heartbeat_file(str(source), str(destination))

    assert calls == 3
    assert destination.read_text(encoding="utf-8") == "new\n"


def test_heartbeat_replace_preserves_last_complete_file_when_lock_persists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "heartbeat.tmp"
    destination = tmp_path / "heartbeat.json"
    source.write_text("new\n", encoding="utf-8")
    destination.write_text("old\n", encoding="utf-8")

    monkeypatch.setattr(
        live_trader.os,
        "replace",
        lambda _src, _dst: (_ for _ in ()).throw(PermissionError("locked")),
    )
    monkeypatch.setattr(live_trader.time, "sleep", lambda _delay: None)

    with pytest.raises(PermissionError, match="locked"):
        live_trader._replace_heartbeat_file(str(source), str(destination), attempts=2)

    assert destination.read_text(encoding="utf-8") == "old\n"
    assert source.read_text(encoding="utf-8") == "new\n"
