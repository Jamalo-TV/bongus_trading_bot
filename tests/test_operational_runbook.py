from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_runbook_declares_systemd_as_the_only_production_entrypoint() -> None:
    runbook = (PROJECT_ROOT / "RUNBOOK.md").read_text(encoding="utf-8")

    assert "sole authoritative production entry point" in runbook
    assert "sudo systemctl start bongus.service" in runbook
    assert "sudo systemctl stop bongus.service" in runbook
    assert "Always run Bongus inside tmux" not in runbook
    assert "python3 bongus/monitoring/king_watchdog.py" not in runbook
    assert "Do not launch the watchdog" in runbook
    assert "active-active" in runbook


def test_runbook_requires_clock_sync_before_starting_a_soak() -> None:
    runbook = (PROJECT_ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    start_section = runbook.split("## Stop Or Restart", maxsplit=1)[0]

    assert "chronyc -n tracking" in start_section
    assert "Leap status: Normal" in start_section
    assert "positive stratum" in start_section
    assert "no greater than 250 ms" in start_section
    assert "no greater than 100 ms is preferred" in start_section
    assert start_section.index("chronyc -n tracking") < start_section.index(
        "sudo systemctl start bongus.service"
    )


def test_runbook_forbids_passive_hwm_decay() -> None:
    runbook = (PROJECT_ROOT / "RUNBOOK.md").read_text(encoding="utf-8")

    assert "Automatic/passive HWM decay is forbidden in production" in runbook
    assert '"hwm_auto_decay_after_hours": 0.0' in runbook
    assert '"hwm_auto_decay_fraction": 0.0' in runbook
    assert '"hwm_auto_decay_after_hours": 72.0' not in runbook


def test_runbook_records_backup_clock_and_independent_monitoring_contract() -> None:
    runbook = (PROJECT_ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    normalized = " ".join(runbook.split())

    assert "every 10 minutes" in normalized
    assert "8 GB" in normalized
    assert "20 GB" in normalized
    assert "20.5 GB" in normalized
    assert "900 seconds" in normalized
    assert "warns above 100 ms" in normalized
    assert "critical above 250 ms" in normalized
    assert "125 seconds" in normalized
    assert "restore drill monthly" in normalized
    normalized_casefold = normalized.casefold()
    assert "quarterly" in normalized_casefold
    assert "blank linux host" in normalized_casefold
