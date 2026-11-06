"""Воспроизводимые сигналы и неисправности; эталон не передаётся детекторам."""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np

from .config import Feature
from .schemas import SensorMeasurement


@dataclass
class Fault:
    kind: str
    duration: int
    magnitude: float
    age: int = 0
    held_value: float | None = None


def corrupt(clean: float, fault: Fault, rng) -> float | None:
    """Применить выбранный сбой к одной точке и увеличить возраст неисправности."""
    if fault.kind == "missing":
        value = None
    elif fault.kind == "spike":
        value = clean + fault.magnitude
    elif fault.kind == "noise":
        value = clean + float(rng.normal(0, fault.magnitude))
    elif fault.kind == "stuck":
        if fault.held_value is None:
            fault.held_value = clean
        value = fault.held_value
    elif fault.kind == "bias":
        value = clean + fault.magnitude
    elif fault.kind == "drift":
        value = clean + fault.magnitude * (fault.age + 1) / fault.duration
    else:
        raise ValueError(f"Unsupported fault {fault.kind}")
    fault.age += 1
    return value


class Simulator:
    def __init__(self, settings, start=None, seed=None, namespace="demo"):
        self.settings = settings
        self.start = start or datetime.now(timezone.utc).replace(microsecond=0)
        self.clock = self.start
        signal_seed = settings.simulation.seed if seed is None else seed
        self.fault_rng = np.random.default_rng((settings.simulation.seed if seed is None else seed) + 1)
        self.namespace = namespace
        self.faults = {}
        self.channels = {}
        for node in range(settings.simulation.nodes):
            for channel_index, feature in enumerate(settings.simulation.features):
                sid = f"{namespace}.node{node + 1}.{feature}"
                self.channels[sid] = {
                    "node": node,
                    "feature": feature,
                    "next": self.start,
                    "sequence": 0,
                    "rng": np.random.default_rng(np.random.SeedSequence([signal_seed, node, channel_index])),
                }

    def inject(self, sensor_id, kind, duration=12, magnitude=25):
        if sensor_id not in self.channels:
            raise ValueError("Unknown simulator sensor")
        if sensor_id in self.faults:
            raise ValueError("Sensor already has an active fault")
        self.faults[sensor_id] = Fault(kind, duration, magnitude)

    def step(self, seconds=30):
        end = self.clock + timedelta(seconds=seconds)
        output = []
        for sid, channel in self.channels.items():
            feature = channel["feature"]
            spec = self.settings.features.get(feature, Feature())
            signal = self.settings.simulation.features[feature]
            while channel["next"] < end:
                ts = channel["next"]
                day = (ts - self.start).total_seconds() / 86400
                clean = (
                    signal.base
                    + 0.2 * channel["node"]
                    + signal.amplitude * math.sin(2 * math.pi * day + signal.phase)
                    + float(channel["rng"].normal(0, signal.noise))
                )
                # Суточный синус и шум имитируют сигнал, но не моделируют реальную погоду.
                fault = self.faults.get(sid)
                params = (
                    {"duration": fault.duration, "magnitude": fault.magnitude, "age": fault.age}
                    if fault
                    else {}
                )
                value = corrupt(clean, fault, self.fault_rng) if fault else clean
                m = SensorMeasurement(
                    timestamp=ts,
                    sensor_id=sid,
                    node_id=f"{self.namespace}.node{channel['node'] + 1}",
                    feature=feature,
                    value=value,
                    unit=spec.unit,
                    source="synthetic",
                    location_id=f"plot{channel['node'] + 1}",
                    sequence_number=channel["sequence"],
                )
                truth = {
                    "measurement_id": m.measurement_id,
                    "timestamp": ts.isoformat(),
                    "sensor_id": sid,
                    "clean_value": clean,
                    "corrupted_value": value,
                    "corruption_type": fault.kind if fault else "normal",
                    "parameters": params,
                }
                output.append((m, truth))
                if fault and fault.age >= fault.duration:
                    del self.faults[sid]
                channel["sequence"] += 1
                channel["next"] += timedelta(seconds=spec.period_seconds)
        self.clock = end
        return sorted(output, key=lambda row: (row[0].timestamp, row[0].sensor_id))
