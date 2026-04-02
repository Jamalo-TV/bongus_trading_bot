"""Shadow-mode exit model loader and scorer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class ShadowExitModel:
    def __init__(self, model_path: str | Path):
        self.path = Path(model_path)
        self.model: dict[str, Any] | None = None
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self.model = None
            return
        with self.path.open(encoding="utf-8") as handle:
            self.model = json.load(handle)

    def is_ready(self) -> bool:
        return self.model is not None

    def score(self, features: dict[str, Any]) -> tuple[float, float]:
        if self.model is None:
            return 0.0, 0.0

        bias = float(self.model.get("bias", 0.0))
        hold_weights = self.model.get("hold_weights", {})
        exit_weights = self.model.get("exit_weights", {})
        hold_score = bias + sum(float(hold_weights.get(name, 0.0)) * float(features.get(name, 0.0)) for name in hold_weights)
        exit_score = -bias + sum(float(exit_weights.get(name, 0.0)) * float(features.get(name, 0.0)) for name in exit_weights)
        return hold_score, exit_score
