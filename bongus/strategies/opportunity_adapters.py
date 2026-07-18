"""Thin transport adapters for the canonical opportunity kernel."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import polars as pl

from bongus.strategies.opportunity_kernel import (
    OpportunityEvaluation,
    OpportunityEvaluationInput,
    evaluate_opportunity,
)


OpportunitySurface = Literal["replay", "shadow", "paper", "live"]


@dataclass(frozen=True, slots=True)
class OpportunityKernelAdapter:
    """A surface label around the same pure equations.

    The label is intentionally not passed to the kernel, so a runtime mode
    cannot change economics.  Only data/latency/exchange-response adapters may
    differ between surfaces.
    """

    surface: OpportunitySurface

    def evaluate(self, inputs: OpportunityEvaluationInput) -> OpportunityEvaluation:
        return evaluate_opportunity(inputs)


REPLAY_OPPORTUNITY_ADAPTER = OpportunityKernelAdapter("replay")
SHADOW_OPPORTUNITY_ADAPTER = OpportunityKernelAdapter("shadow")
PAPER_OPPORTUNITY_ADAPTER = OpportunityKernelAdapter("paper")
LIVE_OPPORTUNITY_ADAPTER = OpportunityKernelAdapter("live")


def apply_replay_settlement_cashflows(
    frame: pl.DataFrame,
    *,
    direction: Literal["long_spot_short_perp", "short_spot_long_perp"] = (
        "long_spot_short_perp"
    ),
) -> pl.DataFrame:
    """Annotate causal replay rows with exact, observed settlement returns.

    ``funding_rate`` is a per-settlement rate.  It is never prorated by elapsed
    time, and long-spot/short-perp does not invent a spot-borrow charge.  The
    inverse direction reverses funding sign; its time-based spot borrow belongs
    in the separate financing ledger rather than in settlement cash flow.
    """

    required = {"funding_eligible", "funding_snapshot", "funding_rate"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"replay settlement data missing columns: {sorted(missing)}")
    direction_sign = 1.0 if direction == "long_spot_short_perp" else -1.0
    return frame.with_columns(
        pl.when(pl.col("funding_eligible") & pl.col("funding_snapshot"))
        .then(pl.col("funding_rate") * direction_sign)
        .otherwise(0.0)
        .alias("_funding_accrual")
    )
