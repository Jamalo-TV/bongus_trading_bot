from bongus.portfolio.portfolio_allocator import PortfolioAllocator, RankedCandidate


def test_allocator_respects_top_n_and_cluster_caps():
    allocator = PortfolioAllocator(
        {
            "scanner_min_depth_multiplier": 2.0,
            "per_symbol_notional_cap_usd": 5_000.0,
            "max_gross_exposure_usd": 10_000.0,
            "target_concurrent_positions": 2,
            "min_top_n": 1,
            "max_top_n": 2,
            "per_cluster_notional_cap_usd": 5_000.0,
            "min_expected_edge_bps": 5.0,
            "min_incremental_portfolio_edge_bps": 1.0,
        }
    )
    ranked = [
        RankedCandidate("BTCUSDT", "MAJORS", 1, 0.9, 12.0, 1_000_000.0, 0.01),
        RankedCandidate("ETHUSDT", "MAJORS", 2, 0.8, 11.0, 1_000_000.0, 0.02),
        RankedCandidate("SOLUSDT", "L1", 3, 0.7, 9.0, 800_000.0, 0.02),
    ]

    decision = allocator.decide(ranked, {"DOGEUSDT": 1000.0})

    assert [row["symbol"] for row in decision.selected] == ["BTCUSDT", "SOLUSDT"]
    assert decision.exits == ["DOGEUSDT"]
    assert "ETHUSDT" in decision.rejected
    assert "cluster_cap" in decision.rejected["ETHUSDT"]
