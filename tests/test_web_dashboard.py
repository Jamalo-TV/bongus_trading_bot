from bongus.monitoring.web_dashboard import app


def test_kill_switch_route_is_not_exposed():
    paths = {route.path for route in app.routes}
    assert "/api/kill-switch" not in paths
