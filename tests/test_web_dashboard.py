from fastapi.routing import APIRoute

from bongus.monitoring.web_dashboard import app


def test_kill_switch_route_is_not_exposed():
    paths = {route.path for route in app.routes if isinstance(route, APIRoute)}
    assert "/api/kill-switch" not in paths


def test_validation_route_is_exposed():
    paths = {route.path for route in app.routes if isinstance(route, APIRoute)}
    assert "/api/validation" in paths
