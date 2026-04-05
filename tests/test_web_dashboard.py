from fastapi.routing import APIRoute

from bongus.monitoring.web_dashboard import (
    EXPLAIN_HTML,
    HTML_CONTENT,
    _normalize_candidate_snapshot,
    app,
)


def test_kill_switch_route_is_not_exposed():
    paths = {route.path for route in app.routes if isinstance(route, APIRoute)}
    assert "/api/kill-switch" not in paths


def test_validation_route_is_exposed():
    paths = {route.path for route in app.routes if isinstance(route, APIRoute)}
    assert "/api/validation" in paths


def test_explain_route_is_exposed():
    paths = {route.path for route in app.routes if isinstance(route, APIRoute)}
    assert "/explain" in paths


def test_dashboard_uses_top_funding_stats_for_live_funding_card():
    assert "stats.top_funding_rate" in HTML_CONTENT
    assert "stats.top_funding_symbol" in HTML_CONTENT


def test_dashboard_distinguishes_entry_policy_from_validation_board():
    assert "Validation blockers below do not stop entries." in HTML_CONTENT
    assert "This board is for promotion and readiness." in HTML_CONTENT
    assert "CAN TRADE" in HTML_CONTENT


def test_dashboard_handles_offline_telemetry_and_preflight_bridge_states():
    assert "No live telemetry received in this runtime yet." in HTML_CONTENT
    assert '"Offline"' in HTML_CONTENT
    assert '"Blocked"' in HTML_CONTENT


def test_dashboard_candidate_card_renders_unavailable_bps_as_na():
    assert "function formatCandidateBps(value)" in HTML_CONTENT
    assert 'return "n/a";' in HTML_CONTENT


def test_candidate_snapshot_normalization_hides_sentinel_bps_without_depth():
    normalized = _normalize_candidate_snapshot(
        {
            "symbol": "1000000MOGUSDT",
            "metrics": {
                "depth_usd": 0.0,
                "spread_bps": 10_000.0,
                "toxicity_bps": 10_000.0,
            },
        }
    )

    assert normalized["metrics"]["spread_bps"] is None
    assert normalized["metrics"]["toxicity_bps"] is None


def test_candidate_snapshot_normalization_preserves_real_bps_with_depth():
    normalized = _normalize_candidate_snapshot(
        {
            "symbol": "BTCUSDT",
            "metrics": {
                "depth_usd": 125_000.0,
                "spread_bps": 4.25,
                "toxicity_bps": 6.5,
            },
        }
    )

    assert normalized["metrics"]["spread_bps"] == 4.25
    assert normalized["metrics"]["toxicity_bps"] == 6.5


def test_explain_page_contains_bongus_explained_heading():
    assert "Bongus Explained" in EXPLAIN_HTML
