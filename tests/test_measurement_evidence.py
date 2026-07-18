from __future__ import annotations

import sqlite3

from bongus.engine.state_store import StateWriter
from bongus.testing.measurement_evidence import (
    build_phase0_metrics,
    derive_runtime_measurement,
)


def _account(funding_count: int = 0) -> dict:
    return {
        "exchange_facts": {
            "funding_statements": [
                {"content_hash": f"funding-{index}"} for index in range(funding_count)
            ]
        }
    }


def test_empty_runtime_samples_are_not_vacuous_success(tmp_path) -> None:
    writer = StateWriter(str(tmp_path / "state.db"))
    try:
        result = derive_runtime_measurement(writer.conn, account_artifact=_account())
    finally:
        writer.close()
    assert result["lineage"]["decision_order_fill_lineage_pct"] is None
    assert result["exchange_mapping"]["exchange_fill_funding_mapping_pct"] is None
    assert result["daily_reconciliation"]["within_exchange_precision"] is False


def test_legacy_fill_and_external_funding_remain_in_denominator(tmp_path) -> None:
    writer = StateWriter(str(tmp_path / "state.db"))
    try:
        writer.conn.execute(
            """INSERT INTO execution_events
               (symbol, client_order_id, status, filled_qty, execution_type, event_time)
               VALUES ('BTCUSDT', 'legacy', 'FILLED', 1.0, 'TRADE',
                       '2026-01-01T00:00:00+00:00')"""
        )
        writer.conn.commit()
        result = derive_runtime_measurement(
            writer.conn, account_artifact=_account(funding_count=2)
        )
    finally:
        writer.close()
    assert result["lineage"]["sampled_exchange_trade_updates"] == 1
    assert result["lineage"]["decision_order_fill_lineage_pct"] == 0.0
    assert result["exchange_mapping"]["sampled_exchange_effects"] == 3
    assert result["exchange_mapping"]["exchange_fill_funding_mapping_pct"] == 0.0


def test_daily_reconciliation_needs_attestation_and_real_days(tmp_path) -> None:
    writer = StateWriter(str(tmp_path / "state.db"))
    try:
        result = derive_runtime_measurement(
            writer.conn,
            account_artifact=_account(),
            daily_reconciliation_artifact={
                "evidence_kind": "runtime_daily_reconciliation",
                "machine_attestation": {"attested": True},
                "days": [
                    {
                        "unexplained_max_usd": 0.005,
                        "exchange_precision_usd": 0.001,
                    }
                ],
            },
        )
    finally:
        writer.close()
    assert result["daily_reconciliation"]["sampled_days"] == 1
    assert result["daily_reconciliation"]["daily_unexplained_max_usd"] == 0.005
    assert result["daily_reconciliation"]["within_exchange_precision"] is True


def test_phase0_metric_projection_preserves_missing_values() -> None:
    metrics = build_phase0_metrics(
        clean_ci_passed=True,
        deterministic_causal_replay=True,
        runtime_measurement={
            "lineage": {"decision_order_fill_lineage_pct": None},
            "exchange_mapping": {"exchange_fill_funding_mapping_pct": 0.0},
            "daily_reconciliation": {
                "daily_unexplained_max_usd": None,
                "within_exchange_precision": False,
            },
        },
    )
    assert metrics["clean_ci_passed"] is True
    assert metrics["deterministic_causal_replay"] is True
    assert metrics["decision_order_fill_lineage_pct"] is None
    assert metrics["within_exchange_precision"] is False
