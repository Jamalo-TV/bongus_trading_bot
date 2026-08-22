from __future__ import annotations

from typing import Any, cast

from scripts.live_trader_v2 import LiveTraderV2


def _entry(
    intent_id: str,
    *,
    status: str = "FILLED",
    leg_status: str = "FILLED",
) -> dict[str, Any]:
    return {
        "intent_id": intent_id,
        "cycle_id": f"cycle-{intent_id}",
        "status": status,
        "last_fill_time": "2026-01-01T00:01:00+00:00",
        "legs": [
            {
                "market": market,
                "status": leg_status,
                "submitted_quantity": "1",
                "gross_filled_quantity": "1",
            }
            for market in ("spot", "perp")
        ],
    }


class _Reader:
    def __init__(
        self,
        entries: list[dict[str, Any]],
        closed_at: dict[str, str] | None = None,
    ) -> None:
        self.entries = entries
        self.closed_at = closed_at or {}

    def get_execution_tca(self, **_kwargs: Any) -> list[dict[str, Any]]:
        return self.entries

    def get_opportunity_funnel_events(
        self,
        *,
        intent_id: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        del limit
        closed = self.closed_at.get(intent_id)
        return (
            [{"stage": "closed", "reached": True, "event_time": closed}]
            if closed
            else []
        )


def _trader(reader: _Reader) -> LiveTraderV2:
    trader = object.__new__(LiveTraderV2)
    cast(Any, trader).state_reader = reader
    return trader


def test_funding_attribution_requires_one_fully_filled_open_entry() -> None:
    filled = _entry("filled")
    incomplete = _entry("incomplete", leg_status="PARTIALLY_FILLED")
    trader = _trader(_Reader([incomplete, filled]))

    attributed = trader._entry_tca_covering_event(
        "BTCUSDT", "2026-01-01T08:00:00+00:00"
    )

    assert attributed is filled


def test_funding_attribution_fails_closed_for_overlap_or_closed_window() -> None:
    first = _entry("first")
    second = _entry("second")
    overlapping = _trader(_Reader([first, second]))
    closed = _trader(
        _Reader([first], closed_at={"first": "2026-01-01T08:00:00+00:00"})
    )

    assert (
        overlapping._entry_tca_covering_event(
            "BTCUSDT", "2026-01-01T08:00:00+00:00"
        )
        is None
    )
    assert (
        closed._entry_tca_covering_event(
            "BTCUSDT", "2026-01-01T08:00:00+00:00"
        )
        is None
    )


def test_funding_attribution_withholds_malformed_fill_measurements() -> None:
    malformed = _entry("malformed")
    malformed["legs"][0]["submitted_quantity"] = "not-a-decimal"
    trader = _trader(_Reader([malformed]))

    assert (
        trader._entry_tca_covering_event(
            "BTCUSDT", "2026-01-01T08:00:00+00:00"
        )
        is None
    )


def test_funding_attribution_withholds_equal_entry_and_settlement_times() -> None:
    trader = _trader(_Reader([_entry("same-time")]))

    assert (
        trader._entry_tca_covering_event(
            "BTCUSDT", "2026-01-01T00:01:00+00:00"
        )
        is None
    )
