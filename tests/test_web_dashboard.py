from fastapi.routing import APIRoute

from bongus.monitoring.web_dashboard import EXPLAIN_HTML, HTML_CONTENT, app


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


def test_explain_page_contains_bongus_explained_heading():
    assert "Bongus Explained" in EXPLAIN_HTML
