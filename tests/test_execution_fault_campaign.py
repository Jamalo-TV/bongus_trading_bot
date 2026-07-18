from bongus.testing.execution_fault_campaign import run_execution_fault_campaign


def test_seeded_fault_campaign_covers_fault_classes_without_invariant_failure() -> None:
    result = run_execution_fault_campaign(traces=2_000, seed=7)
    assert result.passed, result.first_failure
    assert result.duplicate_deliveries > 0
    assert result.duplicate_exchange_effects == 0
    assert result.stale_deliveries > 0
    assert result.dropped_deliveries > 0
    assert result.crash_restarts > 0
    assert result.event_id_collisions > 0
    assert result.cancel_fill_ambiguities > 0
    assert result.safe_completions + result.blocked_ambiguous_completions == 2_000


def test_fault_campaign_is_reproducible_except_for_elapsed_time() -> None:
    first = run_execution_fault_campaign(traces=100, seed=11).to_dict()
    second = run_execution_fault_campaign(traces=100, seed=11).to_dict()
    first.pop("elapsed_seconds")
    second.pop("elapsed_seconds")
    assert first == second
