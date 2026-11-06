"""Коррекция по доверенной истории; будущие точки доступны только в offline-режиме."""

from dataclasses import dataclass
from typing import Protocol

import numpy as np

from .config import Feature
from .detectors import Point


@dataclass(frozen=True)
class Correction:
    method: str
    value: float | None
    score: float
    explanation: str


class Corrector(Protocol):
    name: str

    def correct(
        self,
        point: Point,
        history: list[Point],
        feature: Feature,
        future: list[Point] | None = None,
        offline: bool = False,
    ) -> Correction: ...


class BaselineCorrector:
    def __init__(self, name):
        self.name = name

    def correct(self, point, history, feature, future=None, offline=False):
        past = [p for p in history if p.value is not None and p.timestamp < point.timestamp][-12:]
        right = [p for p in (future or []) if p.value is not None and p.timestamp > point.timestamp]
        empty = Correction(self.name, None, 0, "No admissible neighbors or gap exceeds five periods")
        if not past and not (offline and right and self.name == "backward_fill"):
            return empty
        age = ((point.timestamp - past[-1].timestamp).total_seconds() / feature.period_seconds) if past else 1
        if age > 5:
            return empty
        if self.name in ("linear", "time", "backward_fill"):
            if not offline:
                return Correction(self.name, None, 0, "Requires offline=True and a future neighbor")
            if not right:
                return empty
            nxt = min(right, key=lambda p: p.timestamp)
            if (nxt.timestamp - point.timestamp).total_seconds() > feature.period_seconds * 5:
                return empty
            if self.name == "backward_fill":
                return Correction(self.name, nxt.value, 0.5, "Offline future fill")
            prev = past[-1]
            left_steps = (point.timestamp - prev.timestamp).total_seconds() / feature.period_seconds
            right_steps = (nxt.timestamp - point.timestamp).total_seconds() / feature.period_seconds
            if self.name == "linear" and not all(
                np.isclose(steps, round(steps)) for steps in (left_steps, right_steps)
            ):
                return Correction(
                    self.name,
                    None,
                    0,
                    "Linear interpolation requires regular cadence; use time for irregular data",
                )
            # Position within the whole gap matters when several samples are missing.
            weight = left_steps / (left_steps + right_steps)
            return Correction(
                self.name,
                float(prev.value + weight * (nxt.value - prev.value)),
                0.75,
                "Offline interpolation; heuristic score",
            )
        xs = np.array([p.value for p in past])
        # Доверенный статус не гарантирует истинность: начало залипания может ещё быть normal.
        if self.name == "forward_fill":
            value = float(xs[-1])
            score = 0.55 / max(1, age)
        else:
            value = float(np.median(xs) if self.name == "median" else np.mean(xs))
            dispersion = float(np.std(xs))
            score = min(0.95, len(xs) / 12) / (1 + dispersion / (feature.noise_floor * 8))
            score /= max(1, age)
        if (feature.min is not None and value < feature.min) or (
            feature.max is not None and value > feature.max
        ):
            return Correction(self.name, None, 0, "Estimate violates physical bounds")
        return Correction(
            self.name, value, float(score), "Trusted past window; score penalizes dispersion and gaps"
        )


CORRECTORS = {
    name: (lambda name=name: BaselineCorrector(name))
    for name in ("median", "mean", "forward_fill", "backward_fill", "linear", "time")
}
