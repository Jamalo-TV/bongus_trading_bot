import importlib
import sys


def test_dashboard_import_does_not_construct_state_writer(monkeypatch):
    module_name = "bongus.monitoring.web_dashboard"
    original_module = sys.modules.get(module_name)
    monitoring_pkg = importlib.import_module("bongus.monitoring")
    original_pkg_attr = getattr(monitoring_pkg, "web_dashboard", None)
    state_store = importlib.import_module("bongus.engine.state_store")

    class DummyReader:
        def close(self) -> None:
            pass

    def fail_writer(*args, **kwargs):
        raise AssertionError("StateWriter should not be constructed during dashboard import")

    monkeypatch.setattr(state_store, "StateReader", lambda *args, **kwargs: DummyReader())
    monkeypatch.setattr(state_store, "StateWriter", fail_writer)
    sys.modules.pop(module_name, None)

    try:
        module = importlib.import_module(module_name)
        assert module.reader is not None
    finally:
        sys.modules.pop(module_name, None)
        if original_module is not None:
            sys.modules[module_name] = original_module
            setattr(monitoring_pkg, "web_dashboard", original_module)
        elif original_pkg_attr is not None:
            setattr(monitoring_pkg, "web_dashboard", original_pkg_attr)
