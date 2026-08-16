from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from bongus.research.cross_venue.evaluation import PREDECLARED_UNIVERSE
from bongus.research.cross_venue.historical import (
    HistoricalArtifactError,
    HistoricalScreenPolicy,
    evaluate_historical_feasibility,
    load_historical_artifact,
    load_historical_screen_policy,
    seal_historical_artifact,
    verify_historical_report,
    write_historical_report,
)
from bongus.research.cross_venue.normalization import mapping_for_asset
from bongus.research.cross_venue.schema import CanonicalAsset, Venue
from bongus.research.cross_venue.storage import canonical_json_bytes

ROOT = Path(__file__).parents[1]
CLI = ROOT / "scripts" / "screen_binance_hyperliquid_history.py"
HOUR_NS = 3_600_000_000_000
TEST_PREREGISTRATION_SHA256 = "c" * 64


def _policy() -> HistoricalScreenPolicy:
    return HistoricalScreenPolicy(
        hold_days=1,
        minimum_common_history_days=2,
        minimum_complete_windows_per_asset=2,
        minimum_total_complete_windows=10,
    )


def _event(
    *,
    asset: CanonicalAsset,
    venue: Venue,
    settlement_hour: int,
    rate: str,
    flags: list[str] | None = None,
) -> dict[str, object]:
    mapping = mapping_for_asset(asset)
    interval = 8 if venue is Venue.BINANCE else 1
    symbol = mapping.binance_symbol if venue is Venue.BINANCE else mapping.hyperliquid_coin
    contract = mapping.binance_contract_id if venue is Venue.BINANCE else mapping.hyperliquid_contract_id
    settlement_ns = settlement_hour * HOUR_NS
    return {
        "event_id": f"b0-{asset.value}-{venue.value}-{settlement_hour}",
        "venue": venue.value,
        "canonical_asset": asset.value,
        "venue_symbol": symbol,
        "contract_id": contract,
        "settlement_time_ns": str(settlement_ns),
        "available_time_ns": str(settlement_ns + 1),
        "funding_rate": rate,
        "funding_interval_hours": str(interval),
        "finalized": True,
        "price_kind": "mark" if venue is Venue.BINANCE else "oracle",
        "source_payload_sha256": hashlib.sha256(
            f"{asset.value}-{venue.value}-{settlement_hour}".encode()
        ).hexdigest(),
        "quality_flags": flags or [],
    }


def _artifact_content(
    *,
    hyperliquid_rates: dict[CanonicalAsset, str] | None = None,
    days: int = 2,
    flagged: bool = False,
) -> dict[str, object]:
    rates = hyperliquid_rates or {asset: "0.0002" for asset in PREDECLARED_UNIVERSE}
    events: list[dict[str, object]] = []
    end_hour = days * 24
    for asset in PREDECLARED_UNIVERSE:
        for hour in range(8, end_hour + 1, 8):
            events.append(asset_event := _event(asset=asset, venue=Venue.BINANCE, settlement_hour=hour, rate="0"))
            if flagged and asset is CanonicalAsset.BTC and hour == 8:
                asset_event["quality_flags"] = ["source_gap"]
        for hour in range(1, end_hour + 1):
            events.append(
                _event(
                    asset=asset,
                    venue=Venue.HYPERLIQUID,
                    settlement_hour=hour,
                    rate=rates[asset],
                )
            )
    return {
        "artifact_id": "b0-tiny-finalized-history",
        "universe": [asset.value for asset in PREDECLARED_UNIVERSE],
        "source_manifest_sha256": {"binance": "a" * 64, "hyperliquid": "b" * 64},
        "events": events,
    }


def _write_artifact(tmp_path: Path, content: dict[str, object], name: str = "history.json") -> Path:
    path = tmp_path / name
    path.write_bytes(canonical_json_bytes(seal_historical_artifact(content)) + b"\n")
    return path


def _evaluate(path: Path) -> dict[str, object]:
    return dict(
        evaluate_historical_feasibility(
            load_historical_artifact(path),
            policy=_policy(),
            preregistration_sha256=TEST_PREREGISTRATION_SHA256,
        )
    )


def test_b0_continue_is_deterministic_interval_correct_and_cost_complete(tmp_path: Path) -> None:
    artifact = _write_artifact(tmp_path, _artifact_content())
    first = _evaluate(artifact)
    second = _evaluate(artifact)

    assert first == second
    assert first["verdict"] == "CONTINUE"
    assert first["grants_live_authority"] is False
    assert first["authority"] == "historical_futility_screen_only"
    assert cast(dict[str, object], first["quality"])["passes"] is True
    aggregate = cast(dict[str, object], first["aggregate"])
    assert aggregate["complete_30d_window_count"] == 10
    assert aggregate["oracle_profitable_window_fraction"] == "1"
    costs = cast(dict[str, object], first["costs_per_30d_hold"])
    assert costs["four_commission_rate"] == "0.0020"
    assert costs["stablecoin_conversion_cost_rate"] == "0.001"
    assert costs["repair_failure_cost_rate"] == "0.0005"
    assert costs["favorable_basis_pnl_usd"] == "0"
    assert costs["slippage_cost_usd"] == "0"

    btc = cast(list[dict[str, object]], first["assets"])[0]
    binance = cast(dict[str, object], btc["binance"])
    hyperliquid = cast(dict[str, object], btc["hyperliquid"])
    assert binance["actual_intervals_hours"] == ["8"]
    assert hyperliquid["actual_intervals_hours"] == ["1"]
    assert hyperliquid["interval_normalized_daily_rate"] == "0.0048"
    assert btc["oracle_break_even_holding_days"] is not None
    first_window = cast(list[dict[str, object]], btc["windows"])[0]
    assert "oracle_net_return_on_total_reserved_capital" in first_window
    assert "primary_net_return_on_total_reserved_capital" in first_window

    report = tmp_path / "b0-report.json"
    write_historical_report(first, report)
    verified = verify_historical_report(report)
    assert verified["report_sha256"] == first["report_sha256"]
    with pytest.raises(HistoricalArtifactError, match="refusing to replace"):
        write_historical_report(first, report)


def test_b0_abandons_nonpositive_oracle_and_rare_cost_coverage(tmp_path: Path) -> None:
    weak = {asset: "0.00001" for asset in PREDECLARED_UNIVERSE}
    weak_report = _evaluate(_write_artifact(tmp_path, _artifact_content(hyperliquid_rates=weak), "weak.json"))
    assert weak_report["verdict"] == "ABANDON"
    assert weak_report["verdict_reasons"] == [
        "optimistic_ex_post_oracle_non_positive_after_all_costs"
    ]

    rare = {asset: "0" for asset in PREDECLARED_UNIVERSE}
    rare[CanonicalAsset.BTC] = "0.001"
    rare_report = _evaluate(_write_artifact(tmp_path, _artifact_content(hyperliquid_rates=rare), "rare.json"))
    rare_aggregate = cast(dict[str, object], rare_report["aggregate"])
    assert Decimal(cast(str, rare_aggregate["oracle_net_rate_after_all_costs"])) > 0
    assert rare_aggregate["oracle_profitable_window_fraction"] == "0.2"
    assert rare_report["verdict"] == "ABANDON"
    assert rare_report["verdict_reasons"] == [
        "optimistic_ex_post_oracle_rarely_covers_all_costs_over_30d_holds"
    ]


def test_b0_sums_discrete_events_without_manufacturing_a_common_interval(tmp_path: Path) -> None:
    content = _artifact_content(
        hyperliquid_rates={asset: "0.0001" for asset in PREDECLARED_UNIVERSE}
    )
    for event in cast(list[dict[str, object]], content["events"]):
        if event["venue"] == "binance":
            event["funding_rate"] = "0.0004"
    report = _evaluate(_write_artifact(tmp_path, content, "actual-intervals.json"))
    btc = cast(list[dict[str, object]], report["assets"])[0]
    window = cast(list[dict[str, object]], btc["windows"])[0]
    assert window["binance_sum_discrete_rates"] == "0.0012"
    assert window["hyperliquid_sum_discrete_rates"] == "0.0024"
    assert window["primary_gross_rate"] == "0.0012"
    assert window["oracle_direction"] == "binance_long_hyperliquid_short"


def test_b0_insufficient_evidence_precedes_economic_verdict(tmp_path: Path) -> None:
    short = _evaluate(_write_artifact(tmp_path, _artifact_content(days=1), "short.json"))
    assert short["verdict"] == "INSUFFICIENT_EVIDENCE"
    assert any(
        "common_history_below_minimum" in reason
        for reason in cast(list[str], short["verdict_reasons"])
    )

    flagged = _evaluate(_write_artifact(tmp_path, _artifact_content(flagged=True), "flagged.json"))
    assert flagged["verdict"] == "INSUFFICIENT_EVIDENCE"
    assert "quality_flagged_finalized_funding" in cast(list[str], flagged["verdict_reasons"])

    duplicate_content = _artifact_content()
    duplicate_events = cast(list[dict[str, object]], duplicate_content["events"])
    duplicate_events.append(dict(duplicate_events[0]))
    duplicate = _evaluate(_write_artifact(tmp_path, duplicate_content, "duplicate.json"))
    assert duplicate["verdict"] == "INSUFFICIENT_EVIDENCE"
    assert "duplicate_event_id" in cast(list[str], duplicate["verdict_reasons"])

    gap_content = _artifact_content()
    gap_events = cast(list[dict[str, object]], gap_content["events"])
    gap_content["events"] = [
        event
        for event in gap_events
        if not (
            event["canonical_asset"] == "BTC"
            and event["venue"] == "hyperliquid"
            and event["settlement_time_ns"] == str(10 * HOUR_NS)
        )
    ]
    gap = _evaluate(_write_artifact(tmp_path, gap_content, "gap.json"))
    assert gap["verdict"] == "INSUFFICIENT_EVIDENCE"
    assert "btc_hyperliquid_interval_coverage_below_minimum" in cast(
        list[str], gap["verdict_reasons"]
    )


def test_b0_artifact_hash_schema_and_actual_interval_fail_closed(tmp_path: Path) -> None:
    path = _write_artifact(tmp_path, _artifact_content())
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["content"]["events"][0]["funding_rate"] = "0.1"
    path.write_bytes(canonical_json_bytes(payload) + b"\n")
    with pytest.raises(HistoricalArtifactError, match="content hash mismatch"):
        load_historical_artifact(path)

    content = _artifact_content()
    cast(list[dict[str, object]], content["events"])[6]["funding_interval_hours"] = "8"
    wrong_interval = _write_artifact(tmp_path, content, "wrong-interval.json")
    with pytest.raises(HistoricalArtifactError, match="unsupported actual funding interval"):
        load_historical_artifact(wrong_interval)


def test_frozen_b0_preregistration_and_offline_cli(tmp_path: Path) -> None:
    policy, preregistration_sha256 = load_historical_screen_policy()
    assert policy.hold_days == 30
    assert policy.minimum_common_history_days == 90
    assert policy.minimum_coverage_fraction.as_tuple().exponent == -2
    assert policy.rarely_covers_fraction == Decimal("0.25")
    assert len(preregistration_sha256) == 64

    artifact = _write_artifact(tmp_path, _artifact_content())
    output = tmp_path / "cli-report.json"
    checked = subprocess.run(
        [sys.executable, "-I", str(CLI), str(artifact), "--output", str(output)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert checked.returncode == 0, checked.stderr
    summary = json.loads(checked.stdout)
    assert summary["verdict"] == "INSUFFICIENT_EVIDENCE"
    assert summary["grants_live_authority"] is False
    assert verify_historical_report(output)["report_sha256"] == summary["report_sha256"]
    assert "allow-network" not in CLI.read_text(encoding="utf-8")
