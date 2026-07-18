import asyncio
import base64
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.routing import APIRoute

from bongus.monitoring.web_dashboard import (
    EXPLAIN_HTML,
    HTML_CONTENT,
    _admin_auth_configured,
    _admin_password_matches,
    _normalize_candidate_snapshot,
    _viewer_auth_configured,
    _viewer_credentials_match,
    _websocket_viewer_authorized,
    api_admin_flatten_all,
    api_exchange_statements,
    api_risk,
    app,
)


def test_kill_switch_route_is_not_exposed():
    paths = {route.path for route in app.routes if isinstance(route, APIRoute)}
    assert "/api/kill-switch" not in paths


def test_validation_route_is_exposed():
    paths = {route.path for route in app.routes if isinstance(route, APIRoute)}
    assert "/api/validation" in paths


def test_exchange_statement_route_is_exposed_and_queries_latest_first():
    paths = {route.path for route in app.routes if isinstance(route, APIRoute)}
    assert "/api/exchange-statements" in paths

    expected = [{"statement_type": "FUNDING_FEE", "amount": "1.25"}]
    with patch(
        "bongus.monitoring.web_dashboard.reader.get_exchange_statement_entries",
        return_value=expected,
    ) as get_entries:
        result = asyncio.run(
            api_exchange_statements(
                limit=25,
                statement_type="FUNDING_FEE",
                reconciliation_status="LEDGERED",
            )
        )

    assert result == expected
    get_entries.assert_called_once_with(
        statement_type="FUNDING_FEE",
        reconciliation_status="LEDGERED",
        limit=25,
        descending=True,
    )


def test_explain_route_is_exposed():
    paths = {route.path for route in app.routes if isinstance(route, APIRoute)}
    assert "/explain" in paths


def test_admin_route_is_exposed():
    paths = {route.path for route in app.routes if isinstance(route, APIRoute)}
    assert "/admin" in paths
    assert "/api/admin/flatten-all" in paths


def test_dashboard_uses_top_funding_stats_for_live_funding_card():
    assert "stats.top_funding_rate" in HTML_CONTENT
    assert "stats.top_funding_symbol" in HTML_CONTENT


def test_dashboard_surfaces_live_unrealized_pnl_and_admin_console():
    assert "Live Unrealized PnL" in HTML_CONTENT
    assert "Admin console" in HTML_CONTENT
    assert "stats.current_unrealized_pnl" in HTML_CONTENT


def test_dashboard_distinguishes_entry_policy_from_validation_board():
    assert "Validation blockers below do not stop entries." in HTML_CONTENT
    assert "This board is for promotion and readiness." in HTML_CONTENT
    assert "CAN TRADE" in HTML_CONTENT


def test_dashboard_handles_runtime_offline_and_preflight_bridge_states():
    assert "persisted state only" in HTML_CONTENT
    assert '"Offline"' in HTML_CONTENT
    assert '"Blocked"' in HTML_CONTENT
    assert "runtime_freshness_seconds" in HTML_CONTENT


def test_dashboard_surfaces_startup_manual_review_and_recovery_state():
    assert "startup_reconciliation_manual_review" in HTML_CONTENT
    assert "portfolio guard:" in HTML_CONTENT
    assert "symbol guard:" in HTML_CONTENT
    assert "spot hedge gap" in HTML_CONTENT
    assert "Recovery" in HTML_CONTENT


def test_dashboard_headline_copy_distinguishes_safe_mode_from_symbol_blocks():
    assert "portfolio-wide safety guard is active" in HTML_CONTENT
    assert "only the flagged symbol(s) are blocked. Other trading continues." in HTML_CONTENT


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


def test_admin_auth_helpers_support_plaintext_passwords():
    with patch.dict(
        "os.environ",
        {
            "BONGUS_ADMIN_USERNAME": "operator",
            "BONGUS_ADMIN_PASSWORD": "swordfish",
        },
        clear=False,
    ):
        assert _admin_auth_configured() is True
        assert _admin_password_matches("swordfish") is True
        assert _admin_password_matches("badpass") is False


def test_viewer_auth_is_default_deny_and_accepts_separate_or_admin_credentials():
    with patch.dict(
        "os.environ",
        {
            "BONGUS_VIEWER_USERNAME": "observer",
            "BONGUS_VIEWER_PASSWORD": "read-only",
            "BONGUS_ADMIN_USERNAME": "operator",
            "BONGUS_ADMIN_PASSWORD": "admin-secret",
        },
        clear=True,
    ):
        assert _viewer_auth_configured() is True
        assert _viewer_credentials_match("observer", "read-only") is True
        assert _viewer_credentials_match("operator", "admin-secret") is True
        assert _viewer_credentials_match("observer", "wrong") is False

    with patch.dict("os.environ", {}, clear=True):
        assert _viewer_auth_configured() is False


def test_websocket_auth_rejects_missing_header_and_accepts_basic_credentials():
    class _Socket:
        def __init__(self, authorization: str = "") -> None:
            self.headers = {"authorization": authorization}

    encoded = base64.b64encode(b"observer:read-only").decode("ascii")
    with patch.dict(
        "os.environ",
        {
            "BONGUS_VIEWER_USERNAME": "observer",
            "BONGUS_VIEWER_PASSWORD": "read-only",
        },
        clear=True,
    ):
        assert _websocket_viewer_authorized(
            _Socket()  # pyright: ignore[reportArgumentType]
        ) is False
        assert _websocket_viewer_authorized(
            _Socket(f"Basic {encoded}")  # pyright: ignore[reportArgumentType]
        ) is True
        assert _websocket_viewer_authorized(
            _Socket("Basic not-base64")  # pyright: ignore[reportArgumentType]
        ) is False


def test_admin_flatten_all_writes_request_via_config():
    with (
        patch(
            "bongus.monitoring.web_dashboard.reader.get_positions_for_current_mode",
            return_value=[{"symbol": "BTCUSDT"}],
        ),
        patch("bongus.monitoring.web_dashboard.config_manager.apply_updates") as apply_updates,
    ):
        result = asyncio.run(api_admin_flatten_all(admin_user="operator"))

    apply_updates.assert_called_once()
    update_payload = apply_updates.call_args.args[0]
    assert update_payload["pause_new_entries"] is True
    assert update_payload["operator_flatten_all_requested_by"] == "operator"
    assert update_payload["operator_flatten_all_request_id"]
    assert update_payload["operator_flatten_all_requested_at"]
    assert result["status"] == "requested"
    assert result["open_position_count"] == 1
    assert result["requested_at"]


def test_api_risk_derives_wall_clock_freshness_from_trader_heartbeat():
    stale_heartbeat = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()

    with (
        patch(
            "bongus.monitoring.web_dashboard.reader.get_risk",
            return_value={
                "loop_last_alive_at": stale_heartbeat,
                "telemetry_connected": True,
                "execution_bridge_healthy": True,
                "runtime_ready": True,
                "allow_new_risk": True,
                "telemetry_staleness_seconds": 0.0,
            },
        ),
        patch("bongus.monitoring.web_dashboard.config_manager.get", return_value=5.0),
    ):
        risk = asyncio.run(api_risk())

    assert risk["runtime_offline"] is True
    assert risk["runtime_freshness_seconds"] >= 590.0
    assert risk["telemetry_connected"] is False
    assert risk["execution_bridge_healthy"] is False
    assert risk["runtime_ready"] is False
    assert risk["allow_new_risk"] is False
    assert str(risk["entry_block_reason"]).startswith("runtime offline")
