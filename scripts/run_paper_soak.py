#!/usr/bin/env python3
"""Run the actual supervised runtime without credentials, then verify 30+ minutes.

This operational check cannot approve live trading, validate real fills, or prove
profitability. Its report records immutable source/binary hashes and raw samples.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psutil

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bongus.testing.paper_soak import (
    ContinuousWindow, health_errors, isolated_environment, projection_errors, shutdown_log_errors,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected object: {path.name}")
    return value


def write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True, indent=2, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def source_hashes(root: Path, binary: Path) -> dict[str, str]:
    paths: set[Path] = {binary}
    for directory in ("bongus", "scripts", "config", "execution_engine/src"):
        paths.update(path for path in (root / directory).rglob("*")
                     if path.is_file() and path.suffix in {".py", ".json", ".sh", ".ps1", ".rs"})
    for name in ("requirements.lock", "requirements-runtime.txt", "live_config.json",
                 "rust-toolchain.toml", "release-manifest.json", "execution_engine/Cargo.toml",
                 "execution_engine/Cargo.lock", "execution_engine/config_sync_schema_v3.json"):
        if (root / name).is_file():
            paths.add(root / name)
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(paths)}


def read_risk(path: Path) -> dict[str, Any]:
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=2)) as conn:
        values = conn.execute("SELECT key, value FROM risk_state").fetchall()
    result = {}
    for key, value in values:
        try:
            result[key] = json.loads(value)
        except (ValueError, TypeError):
            result[key] = value
    return result


def read_projection_status(path: Path) -> tuple[int, str | None]:
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=2)) as conn:
        return conn.execute(
            "SELECT COUNT(*), MIN(first_seen_at) FROM telemetry_receipts WHERE status='PROCESSING'",
        ).fetchone()


def child_snapshot(process: psutil.Process) -> list[dict[str, Any]]:
    rows = []
    for child in process.children(recursive=True):
        try:
            if child.is_running() and child.status() != psutil.STATUS_ZOMBIE:
                rows.append({"pid": child.pid, "created_at": child.create_time(),
                             "name": child.name(), "rss_bytes": child.memory_info().rss,
                             "command": child.cmdline()})
        except psutil.NoSuchProcess:
            # The role check/identity comparison below diagnoses exited children.
            continue
    return sorted(rows, key=lambda row: row["pid"])


def stop_runtime(proc: subprocess.Popen, children: list[psutil.Process]) -> bool:
    if proc.poll() is None:
        if os.name == "nt":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=150)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
    _, alive = psutil.wait_procs(children, timeout=5)
    clean = not alive and proc.returncode == 0
    for child in alive:
        try:
            child.terminate()
        except psutil.NoSuchProcess:
            pass
    _, remaining = psutil.wait_procs(alive, timeout=10)
    for child in remaining:
        try:
            child.kill()
        except psutil.NoSuchProcess:
            pass
    return clean


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration-seconds", type=float, default=1800)
    parser.add_argument("--startup-timeout-seconds", type=float, default=300)
    parser.add_argument("--output", type=Path, required=True,
                        help="New dedicated data/evidence directory; must not exist")
    args = parser.parse_args()
    window = ContinuousWindow(args.duration_seconds)
    output = args.output.resolve()
    if output.exists():
        parser.error("output must be new; existing runtime state is never reused")
    manifest = load_json(ROOT / "bongus" / "runtime" / "process_manifest.json")
    relative = Path(manifest["processes"]["rust"]["target"])
    if relative.is_absolute() or ".." in relative.parts:
        parser.error("unsafe Rust binary path in process manifest")
    binary = ROOT / relative
    if os.name == "nt":
        binary = binary.with_suffix(".exe")
    if not binary.is_file():
        parser.error("build the native release execution engine first")
    for port in (5555, 9000, 8080):
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                parser.error(f"port {port} is already in use; stop the other runtime first")
    hashes = source_hashes(ROOT, binary)
    output.mkdir(parents=True, mode=0o750)
    (output / "runtime").mkdir(mode=0o750)
    if os.name == "posix":
        output.chmod(0o750)
        (output / "runtime").chmod(0o750)
    config = load_json(ROOT / "live_config.json")
    config.update({"pause_new_entries": False, "sentiment_enabled": False,
                   "ai_report_agent_enabled": False, "decision_engine_stage": "shadow",
                   "account_equity_usd": 5000.0, "notional_per_trade": 500.0,
                   "per_symbol_notional_cap_usd": 500.0, "max_gross_exposure_usd": 1000.0})
    write_json(output / "live_config.json", config)
    env = isolated_environment(os.environ, output)
    env["ACCOUNT_EQUITY_USD"] = str(config["account_equity_usd"])
    env["MAX_GROSS_EXPOSURE_USD"] = str(config["max_gross_exposure_usd"])
    env["BONGUS_RUST_BINARY_SHA256"] = hashlib.sha256(binary.read_bytes()).hexdigest()
    # Observer services open existing role databases read-only. Initialize the
    # new store before launching peers so first startup has no observer race.
    subprocess.run(
        [sys.executable, "-c",
         "from bongus.core.config import STATE_DB_PATH,AUDIT_DB_PATH,RESEARCH_DB_PATH; "
         "from bongus.engine.split_state_store import SplitStateWriter; "
         "SplitStateWriter(state_path=STATE_DB_PATH,audit_path=AUDIT_DB_PATH,"
         "research_path=RESEARCH_DB_PATH).close()"],
        cwd=ROOT, env=env, check=True, timeout=60,
        umask=0o027 if os.name == "posix" else -1,
    )
    report: dict[str, Any] = {
        "schema_version": 1, "kind": "operational_paper_soak", "status": "RUNNING",
        "started_at": utc_now().isoformat(), "required_seconds": args.duration_seconds,
        "platform": sys.platform, "python_version": sys.version,
        "source_hashes": hashes, "credential_loading_disabled": True,
        "live_approval": False, "profitability_proven": False, "continuous_seconds": 0.0,
    }
    write_json(output / "paper-soak-report.json", report)
    children: list[psutil.Process] = []
    with (output / "watchdog-console.log").open("w", encoding="utf-8") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "bongus.monitoring.king_watchdog"], cwd=ROOT, env=env,
            stdout=log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
            umask=0o027 if os.name == "posix" else -1,
        )
        parent = psutil.Process(proc.pid)
        startup = time.monotonic()
        try:
            with (output / "paper-soak-samples.jsonl").open("w", encoding="utf-8") as samples:
                while True:
                    now = utc_now()
                    errors: list[str] = []
                    heartbeat: dict[str, Any] = {}
                    risk: dict[str, Any] = {}
                    pending_count: int | None = None
                    oldest_pending_at: str | None = None
                    if proc.poll() is not None:
                        raise RuntimeError(f"watchdog exited: {proc.returncode}")
                    rows = child_snapshot(parent)
                    children = parent.children(recursive=True)
                    try:
                        heartbeat = load_json(output / "runtime" / "runtime_heartbeat.json")
                        risk = read_risk(output / "state.db")
                        errors = health_errors(heartbeat, risk, now=now)
                        pending_count, oldest_pending_at = read_projection_status(output / "state.db")
                        errors.extend(projection_errors(pending_count, oldest_pending_at, now=now))
                    except (OSError, ValueError, sqlite3.Error) as exc:
                        errors.append(f"startup_or_read_error:{type(exc).__name__}")
                    commands = [" ".join(row["command"]) for row in rows]
                    identity_rows = []
                    for expected in ("execution_engine", "scripts.live_trader_v2", "web_dashboard",
                                     "supervisor_service", "telegram_alerter"):
                        if not any(expected in command for command in commands):
                            errors.append(f"missing_process:{expected}")
                        identity_rows.extend((expected, row["pid"], row["created_at"])
                                             for row in rows if expected in " ".join(row["command"]))
                    identity = (heartbeat.get("session_id"),
                                tuple(identity_rows))
                    trader_pids = [pid for role, pid, _ in identity_rows
                                   if role == "scripts.live_trader_v2"]
                    if len(trader_pids) != 1 or heartbeat.get("pid") != trader_pids[0]:
                        errors.append("runtime_trader_pid_mismatch")
                    observation_error = None
                    try:
                        elapsed = window.observe(time.monotonic(), identity, errors)
                    except RuntimeError as exc:
                        # Preserve the failing sample, not only the last healthy one.
                        observation_error = exc
                        elapsed = report["continuous_seconds"]
                    sample = {"observed_at": now.isoformat(), "continuous_seconds": elapsed,
                              "errors": errors, "heartbeat": heartbeat, "risk": risk,
                              "pending_projection_count": pending_count,
                              "oldest_pending_projection_at": oldest_pending_at,
                              "processes": rows}
                    samples.write(json.dumps(sample, sort_keys=True, allow_nan=False) + "\n")
                    samples.flush()
                    os.fsync(samples.fileno())
                    report["continuous_seconds"] = elapsed
                    report["last_observed_at"] = now.isoformat()
                    report["last_errors"] = errors
                    write_json(output / "paper-soak-report.json", report)
                    print(json.dumps({"continuous_seconds": round(elapsed, 1), "errors": errors}), flush=True)
                    if observation_error is not None:
                        raise observation_error
                    if (elapsed >= args.duration_seconds
                            and pending_count == 0):
                        break
                    if window.started is None and time.monotonic() - startup > args.startup_timeout_seconds:
                        raise RuntimeError("runtime did not become ready within startup timeout")
                    time.sleep(5)
            if hashes != source_hashes(ROOT, binary):
                raise RuntimeError("runtime source or executable changed during the soak")
            report["status"] = "PASS"
        except (Exception, KeyboardInterrupt) as exc:
            report.update(status="FAIL", failure=f"{type(exc).__name__}: {exc}")
        finally:
            try:
                report["clean_shutdown"] = stop_runtime(proc, children)
            except (OSError, psutil.Error, subprocess.TimeoutExpired) as exc:
                report["clean_shutdown"] = False
                report["shutdown_error"] = str(exc)
            report["shutdown_log_errors"] = shutdown_log_errors(
                (output / "watchdog-console.log").read_text(encoding="utf-8", errors="replace"),
            )
            if report["shutdown_log_errors"]:
                report["clean_shutdown"] = False
            if not report["clean_shutdown"]:
                report.update(status="FAIL", failure="runtime did not stop cleanly")
            integrity = {}
            for name in ("state.db", "audit.db", "research.db"):
                try:
                    with closing(sqlite3.connect(f"{(output / name).as_uri()}?mode=ro", uri=True)) as conn:
                        integrity[name] = [row[0] for row in conn.execute("PRAGMA integrity_check")]
                except sqlite3.Error as exc:
                    integrity[name] = [str(exc)]
            report["database_integrity"] = integrity
            if any(value != ["ok"] for value in integrity.values()):
                report.update(status="FAIL", failure="database integrity check failed")
            try:
                with closing(sqlite3.connect(f"{(output / 'state.db').as_uri()}?mode=ro", uri=True)) as conn:
                    pending = {
                        table: conn.execute(
                            f"SELECT COUNT(*) FROM {table} WHERE status='PROCESSING'",
                        ).fetchone()[0]
                        for table in ("telemetry_receipts", "telemetry_publications")
                    }
                report["pending_critical_projections_after_shutdown"] = pending
                if any(pending.values()):
                    report.update(status="FAIL", failure="critical projections remained pending after shutdown")
            except sqlite3.Error as exc:
                report.update(status="FAIL", failure=f"critical projection shutdown check failed: {exc}")
            report["finished_at"] = utc_now().isoformat()
            report["samples_sha256"] = hashlib.sha256(
                (output / "paper-soak-samples.jsonl").read_bytes()).hexdigest()
            write_json(output / "paper-soak-report.json", report)
    print(json.dumps({"status": report["status"], "report": str(output / "paper-soak-report.json")}), flush=True)
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
