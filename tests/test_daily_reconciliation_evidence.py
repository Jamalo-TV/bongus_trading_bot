from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from bongus.testing.daily_reconciliation_evidence import (
    DailyReconciliationError,
    append_record,
    build_bundle,
    build_interval,
    verify_journal,
)


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _snapshot(
    *, balance: str = "100", position: str = "0", identity: bool = True
) -> dict:
    return {
        "observed_at": NOW.isoformat(),
        "snapshot_complete": True,
        "account_identity_verified": identity,
        "combined_balances": {"USDT": balance},
        "balance_tolerances": {"USDT": "0.01"},
        "asset_prices_usd": {"USDT": "1", "BTC": "50000"},
        "perpetual_positions": {"BTCUSDT:BOTH": position} if position != "0" else {},
        "position_tolerance": "0.00000001",
    }


def test_first_record_is_baseline_only_and_hash_chained(tmp_path: Path) -> None:
    journal = tmp_path / "journal"
    record, _ = append_record(
        journal,
        observed_at=NOW.isoformat(),
        environment="testnet",
        account_ref={"uri": "account.json", "sha256": "a" * 64},
        snapshot=_snapshot(),
        interval=None,
    )
    records = verify_journal(journal)
    assert records == [record]
    bundle, _ = build_bundle(
        records,
        journal_directory=journal,
        output_directory=tmp_path / "out",
        generated_at=NOW,
    )
    assert bundle["days"] == []
    assert bundle["machine_attestation"]["baseline_only"] is True
    assert bundle["machine_attestation"]["all_intervals_reconciled"] is False


def test_matching_exchange_and_ledger_deltas_reconcile() -> None:
    previous = _snapshot()
    current = {
        **_snapshot(balance="101", position="0.001"),
        "observed_at": (NOW + timedelta(days=1)).isoformat(),
    }
    interval = build_interval(
        previous,
        current,
        ledger_balance_deltas={"USDT": "1"},
        ledger_position_deltas={"BTCUSDT": "0.001"},
        ledger_event_count=2,
        ledger_unvalued_event_count=0,
        ledger_rows_sha256="b" * 64,
    )
    assert interval["within_exchange_precision"] is True
    assert interval["unexplained_max_usd"] == 0.0
    assert interval["status"] == "RECONCILED"


def test_unexplained_or_unverified_delta_fails_closed() -> None:
    previous = _snapshot(identity=False)
    current = {
        **_snapshot(balance="102"),
        "observed_at": (NOW + timedelta(days=1)).isoformat(),
    }
    interval = build_interval(
        previous,
        current,
        ledger_balance_deltas={"USDT": "1"},
        ledger_position_deltas={},
        ledger_event_count=1,
        ledger_unvalued_event_count=0,
        ledger_rows_sha256="c" * 64,
    )
    assert interval["within_exchange_precision"] is False
    assert interval["unexplained_max_usd"] == 1.0
    assert interval["prerequisites"]["previous_account_identity_verified"] is False


def test_journal_tamper_is_detected(tmp_path: Path) -> None:
    journal = tmp_path / "journal"
    _, path = append_record(
        journal,
        observed_at=NOW.isoformat(),
        environment="paper",
        account_ref={"uri": "account.json", "sha256": "d" * 64},
        snapshot=_snapshot(),
        interval=None,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["snapshot"]["combined_balances"]["USDT"] = "999"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(DailyReconciliationError, match="hash mismatch"):
        verify_journal(journal)
