import pytest

from bongus.portfolio import auto_rebalance


def test_main_refuses_to_sweep_without_explicit_opt_in(monkeypatch):
    monkeypatch.delenv(auto_rebalance.ENABLE_ENV_VAR, raising=False)
    swept = False

    def fake_run_sweeper():
        nonlocal swept
        swept = True

    monkeypatch.setattr(auto_rebalance, "run_sweeper", fake_run_sweeper)

    assert auto_rebalance.main() == 2
    assert swept is False


def test_explicit_opt_in_is_diagnostic_only_and_cannot_run(monkeypatch):
    monkeypatch.setenv(auto_rebalance.ENABLE_ENV_VAR, "true")

    assert auto_rebalance.dust_sweeper_enabled()
    assert auto_rebalance.main() == 2
    with pytest.raises(RuntimeError, match="sweeper is retired"):
        auto_rebalance.run_sweeper()
