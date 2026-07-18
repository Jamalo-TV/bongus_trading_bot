import asyncio
from datetime import datetime, timedelta, timezone

from bongus.market_data import funding_ranker as funding_ranker_module
from bongus.market_data.funding_ranker import MarketCandidate, evaluate_candidate, rank_candidates


def _config() -> dict:
    return {
        "scanner_require_spot_and_perp": True,
        "scanner_min_depth_usd": 100_000.0,
        "scanner_min_depth_multiplier": 2.0,
        "notional_per_trade": 10_000.0,
        "scanner_max_spread_bps": 10.0,
        "scanner_min_listing_age_days": 30,
        "scanner_max_data_stale_seconds": 30,
        "scanner_max_toxic_spread_bps": 25.0,
        "ranker_weights": {
            "net_edge": 0.35,
            "depth": 0.20,
            "spread": 0.15,
            "volatility": 0.10,
            "basis_stability": 0.10,
            "regime_health": 0.10,
        },
    }


def test_evaluate_candidate_collects_rejection_reasons():
    candidate = MarketCandidate(
        symbol="NEWUSDT",
        annualized_funding=0.20,
        basis_pct=0.001,
        spread_bps=15.0,
        depth_usd=20_000.0,
        realized_volatility=0.02,
        basis_stability=0.8,
        regime_health=0.8,
        listing_age_days=2,
        data_staleness_seconds=120.0,
        has_spot=False,
        is_delist_risk=True,
        toxicity_bps=30.0,
        cluster="OTHER",
    )

    snapshot = evaluate_candidate(candidate, _config())

    assert not snapshot.accepted
    assert "missing_spot_pair" in snapshot.rejection_reasons
    assert "low_depth" in snapshot.rejection_reasons
    assert "wide_spread" in snapshot.rejection_reasons
    assert "new_listing" in snapshot.rejection_reasons
    assert "stale_data" in snapshot.rejection_reasons
    assert "delist_risk" in snapshot.rejection_reasons
    assert "structural_toxicity" in snapshot.rejection_reasons


def test_rank_candidates_orders_best_edge_first():
    candidates = [
        MarketCandidate(
            symbol="BTCUSDT",
            annualized_funding=0.30,
            basis_pct=0.001,
            spread_bps=2.0,
            depth_usd=2_000_000.0,
            realized_volatility=0.01,
            basis_stability=0.9,
            regime_health=0.95,
            listing_age_days=300,
            data_staleness_seconds=2.0,
            cluster="MAJORS",
        ),
        MarketCandidate(
            symbol="ETHUSDT",
            annualized_funding=0.16,
            basis_pct=0.001,
            spread_bps=3.0,
            depth_usd=800_000.0,
            realized_volatility=0.02,
            basis_stability=0.85,
            regime_health=0.9,
            listing_age_days=200,
            data_staleness_seconds=3.0,
            cluster="MAJORS",
        ),
    ]

    snapshots, scores = rank_candidates("cycle-1", candidates, _config())

    assert len(snapshots) == 2
    assert len(scores) == 2
    assert scores[0].symbol == "BTCUSDT"
    assert scores[0].rank == 1
    assert all(0.0 <= value <= 1.0 for value in scores[0].component_scores.values())


def test_dynamic_ranker_tracks_full_allowed_universe(monkeypatch):
    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return [
                {"symbol": "BTCUSDT", "lastFundingRate": "0.0010"},
                {"symbol": "ETHUSDT", "lastFundingRate": "0.0008"},
                {"symbol": "SOLUSDT", "lastFundingRate": "0.0006"},
            ]

    monkeypatch.setattr(funding_ranker_module, "MAX_FUNDING_SCAN_SYMBOLS", 0)
    monkeypatch.setattr(
        funding_ranker_module.requests,
        "get",
        lambda *args, **kwargs: _FakeResponse(),
    )

    ranker = funding_ranker_module.FundingRanker(["BTCUSDT"], dynamic=True)
    ranker.set_allowed_symbols({"BTCUSDT", "ETHUSDT", "SOLUSDT"})

    asyncio.run(ranker.refresh())

    assert [symbol for symbol, _ in ranker.get_ranked()] == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


def test_dynamic_ranker_ignores_symbols_outside_allowed_universe(monkeypatch):
    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return [
                {"symbol": "BTCUSDT", "lastFundingRate": "0.0010"},
                {"symbol": "ETHUSDT", "lastFundingRate": "0.0008"},
            ]

    monkeypatch.setattr(
        funding_ranker_module.requests,
        "get",
        lambda *args, **kwargs: _FakeResponse(),
    )

    ranker = funding_ranker_module.FundingRanker(dynamic=True)
    ranker.set_allowed_symbols({"BTCUSDT"})

    asyncio.run(ranker.refresh())

    assert ranker.has_symbol("BTCUSDT")
    assert not ranker.has_symbol("ETHUSDT")
    assert [symbol for symbol, _ in ranker.get_ranked()] == ["BTCUSDT"]


def test_symbol_freshness_is_independent_and_excludes_only_stale_symbol():
    ranker = funding_ranker_module.FundingRanker(["BTCUSDT", "ETHUSDT"])
    ranker.update_rate("BTCUSDT", 0.0010)
    ranker.update_rate("ETHUSDT", 0.0008)
    ranker._last_update_by_symbol["ETHUSDT"] = datetime.now(timezone.utc) - timedelta(hours=9)
    # A fresh BTC update must not make the stale ETH observation tradable.
    ranker.update_rate("BTCUSDT", 0.0011)

    assert ranker.get_ranked() == [("BTCUSDT", 0.0011 * 1095)]
    assert ranker.get_rate("ETHUSDT") == 0.0
    status = ranker.status_snapshot()
    assert status["funding_fresh_symbol_count"] == 1
    assert status["funding_stale_symbol_count"] == 1
    assert status["funding_stale_symbols"] == ["ETHUSDT"]
