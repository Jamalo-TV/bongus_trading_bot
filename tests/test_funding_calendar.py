from datetime import datetime, timezone

import pytest

from bongus.market_data.funding_calendar import FundingCalendar


UTC = timezone.utc


def test_calendar_uses_symbol_interval_and_exact_next_funding_time() -> None:
    calendar = FundingCalendar()
    observed = datetime(2026, 7, 18, 9, 0, tzinfo=UTC)
    calendar.update_funding_info(
        [
            {
                "symbol": "BTCUSDT",
                "fundingIntervalHours": 4,
                "adjustedFundingRateCap": "0.003",
                "adjustedFundingRateFloor": "-0.002",
            }
        ],
        observed_at=observed,
    )
    next_settlement = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    calendar.update_premium_index(
        {
            "symbol": "BTCUSDT",
            "nextFundingTime": int(next_settlement.timestamp() * 1000),
        },
        observed_at=observed,
    )

    assert calendar.interval_hours("BTCUSDT") == 4
    assert calendar.next_settlement("BTCUSDT", after=observed) == next_settlement
    assert calendar.minutes_to_next("BTCUSDT", now=observed) == 180.0
    assert calendar.clamp_rate("BTCUSDT", 0.01) == 0.003
    assert calendar.clamp_rate("BTCUSDT", -0.01) == -0.002


def test_settlements_between_is_entry_exclusive_and_exit_inclusive() -> None:
    calendar = FundingCalendar()
    anchor = datetime(2026, 7, 18, 16, 0, tzinfo=UTC)
    calendar.update_premium_index(
        {
            "symbol": "ETHUSDT",
            "nextFundingTime": int(anchor.timestamp() * 1000),
        },
        observed_at=datetime(2026, 7, 18, 15, 0, tzinfo=UTC),
    )

    settlements = calendar.settlements_between(
        "ETHUSDT",
        datetime(2026, 7, 18, 8, 0, tzinfo=UTC),
        datetime(2026, 7, 19, 8, 0, tzinfo=UTC),
    )

    assert settlements == [
        datetime(2026, 7, 18, 16, 0, tzinfo=UTC),
        datetime(2026, 7, 19, 0, 0, tzinfo=UTC),
        datetime(2026, 7, 19, 8, 0, tzinfo=UTC),
    ]


def test_calendar_rejects_invalid_interval_and_floor_cap() -> None:
    calendar = FundingCalendar()
    with pytest.raises(ValueError, match="invalid funding interval"):
        calendar.update_funding_info(
            [{"symbol": "BADUSDT", "fundingIntervalHours": 0}]
        )
    with pytest.raises(ValueError, match="floor exceeds cap"):
        calendar.update_funding_info(
            [
                {
                    "symbol": "BADUSDT",
                    "adjustedFundingRateCap": "0.001",
                    "adjustedFundingRateFloor": "0.002",
                }
            ]
        )


def test_ranker_reporting_annualization_stays_fixed_for_adjusted_interval(monkeypatch) -> None:
    from bongus.market_data import funding_ranker as funding_ranker_module

    class _FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    next_time = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)

    def fake_get(url, timeout):
        if url.endswith("/fundingInfo"):
            return _FakeResponse(
                [{"symbol": "BTCUSDT", "fundingIntervalHours": 4}]
            )
        return _FakeResponse(
            [
                {
                    "symbol": "BTCUSDT",
                    "lastFundingRate": "0.001",
                    "nextFundingTime": int(next_time.timestamp() * 1000),
                }
            ]
        )

    monkeypatch.setattr(funding_ranker_module.requests, "get", fake_get)
    ranker = funding_ranker_module.FundingRanker(["BTCUSDT"])

    import asyncio

    asyncio.run(ranker.refresh())

    # Reporting always uses the canonical raw-rate-times-1095 convention.
    # The four-hour exchange interval is retained solely for cashflow timing.
    assert ranker.get_rate("BTCUSDT") == pytest.approx(0.001 * 1095)
    assert ranker.get_raw_rate("BTCUSDT") == pytest.approx(0.001)
    assert ranker.calendar.interval_hours("BTCUSDT") == 4
    assert ranker.calendar.settlements_between(
        "BTCUSDT",
        datetime(2026, 7, 18, 8, 0, tzinfo=UTC),
        datetime(2026, 7, 18, 20, 0, tzinfo=UTC),
    ) == [
        datetime(2026, 7, 18, 12, 0, tzinfo=UTC),
        datetime(2026, 7, 18, 16, 0, tzinfo=UTC),
        datetime(2026, 7, 18, 20, 0, tzinfo=UTC),
    ]
