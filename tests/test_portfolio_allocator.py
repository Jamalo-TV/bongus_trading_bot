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


def test_legacy_allocator_applies_notional_overrides_to_entry_and_rotation():
    class _EntryDepth:
        def get_entry_depth(self, symbol):
            return 1_000_000.0

    class _RotationDepth:
        def get_entry_depth(self, symbol):
            return 0.0

    class _Funding:
        def get_ranked(self):
            return [("BTCUSDT", 0.20), ("ETHUSDT", 0.15)]

    allocator = PortfolioAllocator(_EntryDepth(), _Funding(), capital_per_slot_usd=2_500.0)
    decision = allocator.decide(
        [],
        blocked_symbols=set(),
        notional_scale=1.0,
        notional_overrides={"BTCUSDT": 3_250.0, "ETHUSDT": 2_100.0},
    )

    assert decision.enter[0] == ("BTCUSDT", 3_250.0)

    rotation_allocator = PortfolioAllocator(_RotationDepth(), _Funding(), capital_per_slot_usd=2_500.0)
    rotation = rotation_allocator.decide(
        [type("OpenPositionLike", (), {"symbol": "SOLUSDT", "ann_funding": 0.01})()],
        blocked_symbols=set(),
        notional_scale=1.0,
        rotation_min_gap_ann=0.05,
        notional_overrides={"BTCUSDT": 3_250.0},
    )

    assert rotation.rotation_targets["SOLUSDT"] == "BTCUSDT"
    assert rotation.rotation_notionals["SOLUSDT"] == 3_250.0


def test_legacy_allocator_uses_explicit_ranked_override_without_refetching_universe():
    class _Depth:
        def get_entry_depth(self, symbol):
            return 1_000_000.0

    class _Funding:
        calls = 0

        def get_ranked(self):
            self.calls += 1
            return [("UNENRICHEDUSDT", 9.99)]

    funding = _Funding()
    allocator = PortfolioAllocator(_Depth(), funding, capital_per_slot_usd=2_500.0)

    decision = allocator.decide(
        [],
        blocked_symbols=set(),
        ranked_candidates=[("BTCUSDT", 0.20), ("ETHUSDT", 0.15)],
    )

    assert funding.calls == 0
    assert [symbol for symbol, _notional in decision.enter] == [
        "BTCUSDT",
        "ETHUSDT",
    ]
    assert "UNENRICHEDUSDT" not in decision.rejected


def test_legacy_allocator_without_override_keeps_funding_ranker_behavior():
    class _Depth:
        def get_entry_depth(self, symbol):
            return 1_000_000.0

    class _Funding:
        calls = 0

        def get_ranked(self):
            self.calls += 1
            return [("BTCUSDT", 0.20)]

    funding = _Funding()
    allocator = PortfolioAllocator(_Depth(), funding, capital_per_slot_usd=2_500.0)

    decision = allocator.decide([], blocked_symbols=set())

    assert funding.calls == 1
    assert decision.enter == [("BTCUSDT", 2_500.0)]
