from datetime import datetime, timedelta, timezone

import polars as pl

from bongus.engine.data_quality import validate_market_data


def test_validate_market_data_reports_numeric_max_timestamp_gap() -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    report = validate_market_data(
        pl.DataFrame(
            {
                "timestamp": [start, start + timedelta(minutes=2)],
                "spot_close": [100.0, 101.0],
                "perp_close": [100.1, 101.1],
                "funding_rate": [0.0001, 0.0001],
                "funding_snapshot": [True, False],
            }
        ),
        max_allowed_gap_minutes=1,
    )

    assert report.max_gap_minutes == 2.0
    assert "max timestamp gap 2.00m exceeds 1.00m" in report.issues
