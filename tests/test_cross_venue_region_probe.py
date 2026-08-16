from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from bongus.research.cross_venue.region_probe import (
    NANOSECONDS_PER_HOUR,
    AppendOnlyProbeLog,
    ProbeMetric,
    ProbeObservation,
    ProbeRegion,
    RegionProbeError,
    evaluate_region_evidence,
    verify_probe_log,
    verify_region_selection_report,
    write_region_selection_report,
)
from bongus.research.cross_venue.region_probe_network import (
    ProbeRunnerConfig,
    PublicRegionProbeRunner,
    RestProbeSample,
    WsProbeSample,
)
from bongus.research.cross_venue.schema import Venue

ROOT = Path(__file__).parents[1]
PROBE_CLI = ROOT / "scripts" / "probe_cross_venue_region.py"
EVALUATE_CLI = ROOT / "scripts" / "evaluate_cross_venue_regions.py"
BASE_TIME_NS = 1_700_000_000_000_000_000
CODE_HASH = "a" * 64
CONFIG_HASH = "b" * 64


def _observation(
    *,
    region: ProbeRegion,
    venue: Venue,
    metric: ProbeMetric,
    offset_ns: int,
    value_ns: int | None = None,
    source_event_time_ns: int | None = None,
    expected_messages: int = 0,
    received_messages: int = 0,
    reconnect_count: int = 0,
    gaps_detected: int = 0,
    gaps_recovered: int = 0,
) -> ProbeObservation:
    capture = BASE_TIME_NS + offset_ns
    receive = capture + (value_ns if metric is ProbeMetric.REST_RTT and value_ns is not None else 0)
    return ProbeObservation.create(
        run_id=f"run-{region.value}",
        region=region,
        probe_host_id=f"host-{region.value}",
        venue=venue,
        metric=metric,
        capture_time_ns=capture,
        receive_time_ns=receive,
        available_time_ns=receive,
        code_sha256=CODE_HASH,
        configuration_sha256=CONFIG_HASH,
        value_ns=value_ns,
        source_event_time_ns=source_event_time_ns,
        expected_messages=expected_messages,
        received_messages=received_messages,
        reconnect_count=reconnect_count,
        gaps_detected=gaps_detected,
        gaps_recovered=gaps_recovered,
        connection_id=f"connection-{region.value}-{venue.value}",
        sequence_id=str(offset_ns),
    )


def _complete_events(*, duration_hours: int = 48) -> tuple[ProbeObservation, ...]:
    result: list[ProbeObservation] = []
    for region in ProbeRegion:
        for venue in Venue:
            result.append(
                _observation(
                    region=region,
                    venue=venue,
                    metric=ProbeMetric.RUN_START,
                    offset_ns=0,
                )
            )
            latency_base = {
                (ProbeRegion.GERMANY, Venue.BINANCE): 10_000_000,
                (ProbeRegion.GERMANY, Venue.HYPERLIQUID): 20_000_000,
                (ProbeRegion.FRANCE, Venue.BINANCE): 30_000_000,
                (ProbeRegion.FRANCE, Venue.HYPERLIQUID): 25_000_000,
            }[(region, venue)]
            for index, multiplier in enumerate((1, 2, 3), start=1):
                rest_time = index * 10 * 1_000_000_000
                result.append(
                    _observation(
                        region=region,
                        venue=venue,
                        metric=ProbeMetric.REST_RTT,
                        offset_ns=rest_time,
                        value_ns=latency_base * multiplier,
                    )
                )
                event_receive = BASE_TIME_NS + rest_time + 1_000_000_000
                event_age = latency_base * (multiplier + 1)
                result.append(
                    _observation(
                        region=region,
                        venue=venue,
                        metric=ProbeMetric.WS_EVENT_AGE,
                        offset_ns=rest_time + 1_000_000_000,
                        value_ns=event_age,
                        source_event_time_ns=event_receive - event_age,
                    )
                )
                result.append(
                    _observation(
                        region=region,
                        venue=venue,
                        metric=ProbeMetric.WS_JITTER,
                        offset_ns=rest_time + 2_000_000_000,
                        value_ns=latency_base // multiplier,
                    )
                )
            late_time = duration_hours * NANOSECONDS_PER_HOUR - 60_000_000_000
            late_receive = BASE_TIME_NS + late_time + 1_000_000_000
            result.extend(
                (
                    _observation(
                        region=region,
                        venue=venue,
                        metric=ProbeMetric.REST_RTT,
                        offset_ns=late_time,
                        value_ns=latency_base,
                    ),
                    _observation(
                        region=region,
                        venue=venue,
                        metric=ProbeMetric.WS_EVENT_AGE,
                        offset_ns=late_time + 1_000_000_000,
                        value_ns=latency_base * 2,
                        source_event_time_ns=late_receive - latency_base * 2,
                    ),
                    _observation(
                        region=region,
                        venue=venue,
                        metric=ProbeMetric.WS_JITTER,
                        offset_ns=late_time + 2_000_000_000,
                        value_ns=latency_base,
                    ),
                    _observation(
                        region=region,
                        venue=venue,
                        metric=ProbeMetric.MESSAGE_WINDOW,
                        offset_ns=late_time + 3_000_000_000,
                        expected_messages=100,
                        received_messages=99,
                    ),
                )
            )
            result.extend(
                (
                    _observation(
                        region=region,
                        venue=venue,
                        metric=ProbeMetric.MESSAGE_WINDOW,
                        offset_ns=50_000_000_000,
                        expected_messages=100,
                        received_messages=99,
                    ),
                    _observation(
                        region=region,
                        venue=venue,
                        metric=ProbeMetric.RECONNECT,
                        offset_ns=51_000_000_000,
                        reconnect_count=1,
                    ),
                    _observation(
                        region=region,
                        venue=venue,
                        metric=ProbeMetric.GAP_RECOVERY,
                        offset_ns=52_000_000_000,
                        value_ns=latency_base * 4,
                        gaps_detected=1,
                        gaps_recovered=1,
                    ),
                    _observation(
                        region=region,
                        venue=venue,
                        metric=ProbeMetric.RUN_END,
                        offset_ns=duration_hours * NANOSECONDS_PER_HOUR,
                    ),
                )
            )
    return tuple(sorted(result, key=lambda event: (event.available_time_ns, event.event_id)))


def test_hash_chained_log_and_best_worst_venue_selection(tmp_path: Path) -> None:
    log = AppendOnlyProbeLog(tmp_path / "region-probe.jsonl")
    events = _complete_events()
    assert log.append_many(events) == len(events)
    assert log.append(events[-1]) is False

    verification = log.verify()
    report = evaluate_region_evidence(verification)
    assert report.status == "selected"
    assert report.selected_region is ProbeRegion.GERMANY
    assert report.grants_live_authority is False
    assert all(region.eligible for region in report.regions)
    assert all(
        venue.loss_fraction is not None
        and venue.reconnect_count == 1
        and venue.gaps_detected == venue.gaps_recovered == 1
        for region in report.regions
        for venue in region.venues
    )

    output = write_region_selection_report(report, tmp_path / "selection.json")
    assert verify_region_selection_report(output)["selected_region"] == "germany"


def test_duration_gate_and_hash_tampering_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "region-probe.jsonl"
    log = AppendOnlyProbeLog(path)
    log.append_many(_complete_events(duration_hours=47))
    report = evaluate_region_evidence(log.verify())
    assert report.status == "evidence_incomplete"
    assert report.selected_region is None
    assert any(
        "duration_outside_inclusive_48_to_72_hours" in venue.incomplete_reasons
        for region in report.regions
        for venue in region.venues
    )

    raw = path.read_bytes()
    path.write_bytes(raw.replace(b'"received_messages":99', b'"received_messages":98', 1))
    with pytest.raises(RegionProbeError, match="hash mismatch"):
        verify_probe_log(path)


def test_conflicting_content_id_and_out_of_order_append_are_rejected(tmp_path: Path) -> None:
    events = _complete_events()
    with pytest.raises(ValueError, match="event_id"):
        replace(events[0], code_sha256="c" * 64)

    log = AppendOnlyProbeLog(tmp_path / "region-probe.jsonl")
    log.append(events[-1])
    with pytest.raises(RegionProbeError, match="ordered by availability"):
        log.append(events[0])


def test_direct_fixture_probe_and_evaluation_clis_never_use_network(tmp_path: Path) -> None:
    fixture = tmp_path / "region-fixture.json"
    fixture.write_text(
        json.dumps(
            {"observations": [event.as_wire for event in _complete_events()]},
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    evidence = tmp_path / "region-probe.jsonl"
    captured = subprocess.run(
        [
            sys.executable,
            str(PROBE_CLI),
            "--fixture",
            str(fixture),
            "--evidence",
            str(evidence),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert captured.returncode == 0, captured.stderr
    assert json.loads(captured.stdout)["mode"] == "fixture"

    output = tmp_path / "region-selection.json"
    evaluated = subprocess.run(
        [
            sys.executable,
            str(EVALUATE_CLI),
            str(evidence),
            "--output",
            str(output),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert evaluated.returncode == 0, evaluated.stderr
    payload = json.loads(evaluated.stdout)
    assert payload["selected_region"] == "germany"
    assert payload["grants_live_authority"] is False


class _Clock:
    def __init__(self) -> None:
        self.value = BASE_TIME_NS
        self.monotonic = 0

    def wall(self) -> int:
        self.value += 1_000_000
        return self.value

    def mono(self) -> int:
        self.monotonic += 1_000_000
        return self.monotonic


class _FixtureTransport:
    def __init__(self, clock: _Clock) -> None:
        self.clock = clock

    def rest_sample(self, venue: Venue) -> RestProbeSample:
        del venue
        capture = self.clock.wall()
        receive = self.clock.wall()
        return RestProbeSample(capture, receive, receive - capture, "fixture-rest")

    def ws_sample(self, venue: Venue, *, timeout_milliseconds: int) -> WsProbeSample:
        del venue, timeout_milliseconds
        receive = self.clock.wall()
        return WsProbeSample(receive - 10_000, receive, "fixture-ws", str(receive))

    def reconnect(
        self,
        venue: Venue,
        *,
        timeout_milliseconds: int,
    ) -> tuple[int, WsProbeSample]:
        del venue, timeout_milliseconds
        receive = self.clock.wall()
        return 20_000, WsProbeSample(receive - 10_000, receive, "fixture-ws-reconnect", str(receive))

    def close(self) -> None:
        return None


def test_public_runner_records_both_venues_with_injected_transport_only(tmp_path: Path) -> None:
    clock = _Clock()
    log = AppendOnlyProbeLog(tmp_path / "runner.jsonl")
    runner = PublicRegionProbeRunner(
        log=log,
        run_id="fixture-run",
        region=ProbeRegion.GERMANY,
        probe_host_id="fixture-host",
        transport=_FixtureTransport(clock),
        config=ProbeRunnerConfig(duration_hours=48),
        clock_ns=clock.wall,
        monotonic_ns=clock.mono,
        sleeper=lambda _seconds: None,
    )
    runner.begin()
    runner.sample_cycle(force_rest=True, force_reconnect=True)
    runner.sample_cycle(force_rest=True)
    runner.finish()

    events = log.verify().events
    assert {event.venue for event in events} == set(Venue)
    assert {ProbeMetric.REST_RTT, ProbeMetric.WS_EVENT_AGE, ProbeMetric.WS_JITTER}.issubset(
        {event.metric for event in events}
    )
    assert sum(event.reconnect_count for event in events) == 2
