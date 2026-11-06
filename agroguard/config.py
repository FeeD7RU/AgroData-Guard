"""Validated, versionable configuration; sensor features are open-ended."""

import json
import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    anomaly_threshold: float = Field(default=0.8, ge=0, le=1)
    correction_threshold: float = Field(default=0.8, ge=0, le=1)
    auto_correct: bool = False


class Feature(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    min: float | None = None
    max: float | None = None
    max_rate: float | None = Field(default=None, gt=0, description="Units per minute")
    noise_floor: float = Field(default=0.1, gt=0)
    unit: str | None = None
    period_seconds: int = Field(default=60, ge=1)

    @model_validator(mode="after")
    def bounds(self):
        if self.min is not None and self.max is not None and self.min >= self.max:
            raise ValueError("feature min must be less than max")
        return self


class Signal(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    base: float
    amplitude: float = 1
    noise: float = Field(default=0.1, ge=0)
    phase: float = 0


class Simulation(BaseModel):
    seed: int = 42
    nodes: int = Field(default=2, ge=1, le=100)
    features: dict[str, Signal]


class DetectionParameters(BaseModel):
    """Heuristic thresholds; changing them requires a new benchmark, not blind tuning."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    zscore_threshold: float = Field(default=3.5, gt=0)
    robust_z_threshold: float = Field(default=4.0, gt=0)
    iqr_multiplier: float = Field(default=1.5, gt=0)
    max_gap_periods: float | None = Field(default=3, ge=1)
    rate_recovery_guard: bool = True
    recovery_band: float = Field(default=2.5, gt=0)
    recovery_reference_fraction: float = Field(default=0.8, ge=0.5, le=1)
    recovery_stable_samples: int = Field(default=3, ge=1, le=100)
    trend_require_change: bool = True
    trend_samples: int = Field(default=12, ge=4, le=100)
    trend_direction: float = Field(default=0.85, ge=0.5, le=1)
    trend_change_scale: float = Field(default=8, gt=0)

    @model_validator(mode="after")
    def recovery_bounds(self):
        if self.recovery_band >= self.robust_z_threshold:
            raise ValueError("recovery_band must be smaller than robust_z_threshold")
        return self


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    window: int = Field(default=48, ge=12, le=1000)
    min_history: int = Field(default=12, ge=4)
    max_batch: int = Field(default=500, ge=1, le=5000)
    max_body_bytes: int = Field(default=1048576, ge=1024)
    detectors: list[str]
    corrector: str = "median"
    detection: DetectionParameters = Field(default_factory=DetectionParameters)
    policy: Policy = Field(default_factory=Policy)
    features: dict[str, Feature]
    simulation: Simulation

    @model_validator(mode="after")
    def history(self):
        if self.min_history > self.window:
            raise ValueError("min_history exceeds window")
        if "trend" in self.detectors and self.detection.trend_require_change:
            if self.window < self.min_history + self.detection.trend_samples - 1:
                raise ValueError("window must fit both trend reference and recent samples")
        return self


def load_settings(path: str | None = None) -> Settings:
    path = path or os.getenv("AGRO_CONFIG")
    content = Path(path) if path else Path(__file__).with_name("defaults.json")
    return Settings.model_validate(json.loads(content.read_text(encoding="utf-8")))
