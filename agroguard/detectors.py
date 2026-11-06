"""Causal baselines. Scores are engineering heuristics, never probabilities."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import numpy as np
from sklearn.ensemble import IsolationForest

from .config import DetectionParameters, Feature


@dataclass(frozen=True)
class Point:
    timestamp: datetime
    value: float | None


@dataclass(frozen=True)
class Detection:
    method: str
    flagged: bool
    score: float
    hint: str
    explanation: str
    reason_code: str = "threshold_check"


class Detector(Protocol):
    name: str

    def detect(self, point: Point, history: list[Point], feature: Feature) -> Detection: ...


def values(history):
    return np.array([p.value for p in history if p.value is not None], dtype=float)


def continuous_history(point, history, feature, max_gap_periods):
    """A gap between finite observations starts a new statistical context.

    Explicit nulls remain visible to streak detectors. They cannot bridge a long
    outage, and observations at/after the evaluated timestamp are never consumed.
    """
    past = sorted((p for p in history if p.timestamp < point.timestamp), key=lambda p: p.timestamp)
    if max_gap_periods is None:
        return past
    recent = []
    newer_time = point.timestamp
    for sample in reversed(past):
        if (newer_time - sample.timestamp).total_seconds() > feature.period_seconds * max_gap_periods:
            break
        recent.append(sample)
        if sample.value is not None:
            newer_time = sample.timestamp
    return recent[::-1]


def robust_center_scale(xs, floor):
    center = float(np.median(xs))
    scale = max(float(1.4826 * np.median(abs(xs - center))), floor)
    return center, scale


class Baseline:
    def __init__(self, name: str, min_history: int = 12, options: DetectionParameters | None = None):
        self.name, self.min_history = name, min_history
        self.options = options or DetectionParameters()

    def detect(self, point, history, feature):
        history = continuous_history(point, history, feature, self.options.max_gap_periods)
        v, xs = point.value, values(history)
        flag, score, hint, reason = False, 0.0, "unknown", "Insufficient history"
        if v is None:
            return Detection(self.name, True, 1.0, "missing", "Explicit null observation")
        if self.name == "range":
            flag = (feature.min is not None and v < feature.min) or (
                feature.max is not None and v > feature.max
            )
            score, hint, reason = float(flag), "out_of_range", "Configured physical bounds"
        elif self.name == "rate":
            prev = next((p for p in reversed(history) if p.value is not None), None)
            if prev and feature.max_rate:
                minutes = (point.timestamp - prev.timestamp).total_seconds() / 60
                rate = abs(v - prev.value) / max(minutes, 1e-6)
                flag = rate > feature.max_rate
                if flag and self.options.rate_recovery_guard and self.is_recovery(point, history, feature):
                    return Detection(
                        self.name,
                        False,
                        0,
                        "normal",
                        f"Rate {rate:.3g} units/min is a return to the established past baseline; previous value was an outlier",
                        "rate_recovery",
                    )
                score, hint, reason = (0.9 if flag else 0), "spike", f"Rate {rate:.3g} units/min"
        elif len(xs) >= self.min_history:
            floor = feature.noise_floor
            if self.name in ("zscore", "robust_z", "iqr"):
                if self.name == "zscore":
                    center, scale = np.mean(xs), max(np.std(xs), floor)
                    distance, threshold = abs(v - center) / scale, self.options.zscore_threshold
                elif self.name == "robust_z":
                    center, scale = robust_center_scale(xs, floor)
                    distance, threshold = abs(v - center) / scale, self.options.robust_z_threshold
                else:
                    q1, q3 = np.quantile(xs, [0.25, 0.75])
                    spread = max(q3 - q1, floor)
                    distance = max((q1 - v) / spread, (v - q3) / spread, 0)
                    threshold = self.options.iqr_multiplier
                flag = distance > threshold
                score = min(0.99, 0.6 + 0.2 * distance / threshold) if flag else 0.0
                hint, reason = "unknown", f"Standardized deviation {distance:.3g}; threshold {threshold}"
            elif self.name == "stuck":
                consecutive = [p.value for p in history[-7:]] + [v]
                flag = (
                    None not in consecutive and len(consecutive) == 8 and np.ptp(consecutive) < floor * 0.001
                )
                score, hint, reason = (0.96 if flag else 0), "stuck", "Eight identical consecutive samples"
            elif self.name == "trend":
                return self.detect_trend(point, history, feature)
            elif self.name == "isolation_forest":
                model = IsolationForest(n_estimators=40, contamination=0.05, random_state=42, n_jobs=1)
                model.fit(xs.reshape(-1, 1))
                margin = float(model.decision_function([[v]])[0])
                flag = margin < 0
                score, reason = (
                    (min(0.98, 0.8 + abs(margin)) if flag else 0),
                    f"Isolation Forest margin {margin:.3g}",
                )
            elif self.name == "rolling":
                recent = np.append(xs[-5:], v)
                reference = xs[:-5]
                scale = max(float(np.std(reference)), floor)
                ratio = float(np.std(recent)) / scale
                flag = ratio > 3
                score, hint, reason = (0.85 if flag else 0), "noise", f"Rolling noise ratio {ratio:.3g}"
        return Detection(
            self.name,
            bool(flag),
            float(score),
            hint,
            reason,
            "insufficient_history" if reason == "Insufficient history" else "threshold_check",
        )

    def is_recovery(self, point, history, feature):
        finite = [p for p in history if p.value is not None]
        if len(finite) < self.min_history + 1:
            return False
        reference = values(finite[:-1])
        center, scale = robust_center_scale(reference, feature.noise_floor)
        stable_fraction = float(np.mean(abs(reference - center) <= self.options.robust_z_threshold * scale))
        recent_reference = reference[-self.options.recovery_stable_samples :]
        isolated = len(recent_reference) == self.options.recovery_stable_samples and bool(
            np.all(abs(recent_reference - center) <= self.options.recovery_band * scale)
        )
        previous_is_outlier = abs(finite[-1].value - center) > self.options.robust_z_threshold * scale
        returns_to_band = abs(point.value - center) <= self.options.recovery_band * scale
        return (
            isolated
            and stable_fraction >= self.options.recovery_reference_fraction
            and previous_is_outlier
            and returns_to_band
        )

    def detect_trend(self, point, history, feature):
        n = self.options.trend_samples
        if not self.options.trend_require_change:
            # Reproduce v1 for explicit before/after experiments only.
            recent = np.append(values(history)[-(n - 1) :], point.value)
            flag = (
                abs(np.mean(np.sign(np.diff(recent)))) > self.options.trend_direction
                and abs(recent[-1] - recent[0]) > feature.noise_floor * self.options.trend_change_scale
            )
            return Detection(
                self.name, bool(flag), 0.84 if flag else 0, "drift", "Sustained monotonic trend (v1 rule)"
            )
        recent = history[-(n - 1) :] + [point]
        reference = [p for p in history[: -(n - 1)] if p.value is not None]
        if len(recent) < n or any(p.value is None for p in recent) or len(reference) < self.min_history:
            return Detection(
                self.name,
                False,
                0,
                "unknown",
                "Need a continuous recent trend and an earlier reference window",
                "insufficient_history",
            )
        origin = reference[0].timestamp

        def axis(samples):
            return np.array(
                [(p.timestamp - origin).total_seconds() / feature.period_seconds for p in samples]
            )

        ref_x, recent_x = axis(reference), axis(recent)
        ref_y, recent_y = values(reference), values(recent)
        old_slope, intercept = np.polyfit(ref_x, ref_y, 1)
        new_slope = float(np.polyfit(recent_x, recent_y, 1)[0])
        _, residual_scale = robust_center_scale(ref_y - (old_slope * ref_x + intercept), feature.noise_floor)
        displacement = abs(new_slope - old_slope) * (recent_x[-1] - recent_x[0])
        direction = abs(float(np.mean(np.sign(np.diff(recent_y)))))
        flag = (
            direction > self.options.trend_direction
            and abs(recent_y[-1] - recent_y[0]) > feature.noise_floor * self.options.trend_change_scale
            and displacement > residual_scale * self.options.trend_change_scale
        )
        return Detection(
            self.name,
            bool(flag),
            0.84 if flag else 0,
            "drift",
            f"Trend slope {new_slope:.3g} vs past {old_slope:.3g} units/period; excess displacement {displacement / residual_scale:.3g} scales. Real process changes remain possible.",
            "trend_change" if flag else "trend_consistent_with_past",
        )


DETECTOR_NAMES = (
    "range",
    "rate",
    "zscore",
    "robust_z",
    "iqr",
    "rolling",
    "stuck",
    "trend",
    "isolation_forest",
)
DETECTORS = {
    name: (lambda minimum=12, options=None, name=name: Baseline(name, minimum, options))
    for name in DETECTOR_NAMES
}


def diagnose(detections: list[Detection], quality: str = "good") -> tuple[bool, str, float]:
    flagged = [d for d in detections if d.flagged]
    if not flagged:
        return (True, "unknown", 0.8) if quality != "good" else (False, "normal", 0.0)
    # A point alone cannot distinguish bias from a legitimate persistent shift.
    for hint in ("missing", "out_of_range", "stuck", "drift", "spike", "noise"):
        match = [d for d in flagged if d.hint == hint]
        if match:
            return True, hint, max(d.score for d in flagged)
    return True, "unknown", max(d.score for d in flagged)
