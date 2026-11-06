"""Chronological reproducible experiment with explicit online/offline boundaries."""

import json
import platform
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import numpy as np
import sklearn

from .config import Feature
from .correctors import CORRECTORS
from .detectors import DETECTORS, Point
from .evaluation import score_pipeline
from .metrics import detection_metrics
from .simulator import Fault, corrupt


def run_benchmark(settings, seed=42, size=300, output: str | None = None, clean_measurements=None):
    rng = np.random.default_rng(seed)
    fault_rng = np.random.default_rng(seed + 1)
    if clean_measurements is not None:
        if not 200 <= len(clean_measurements) <= 1500:
            raise ValueError("Historical benchmark requires 200..1500 clean observations")
        identities = {(m.sensor_id, m.feature, m.unit) for m in clean_measurements}
        if len(identities) != 1 or any(m.value is None for m in clean_measurements):
            raise ValueError("Select one sensor with a consistent feature/unit and no missing clean values")
        ordered = sorted(clean_measurements, key=lambda m: m.timestamp)
        times = [m.timestamp for m in ordered]
        if len(set(times)) != len(times):
            raise ValueError("Duplicate timestamps in clean dataset")
        clean = np.array([m.value for m in ordered], dtype=float)
        sensor_id, feature_name, unit = next(iter(identities))
        size = len(clean)
    else:
        times = [datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=i) for i in range(size)]
        clean = 23 + 0.7 * np.sin(np.arange(size) * 2 * np.pi / 1440) + rng.normal(0, 0.15, size)
        sensor_id, feature_name, unit = "benchmark.temperature", "temperature", "°C"
    split = int(size * 0.4)
    observed = clean.tolist()
    labels = ["normal"] * size
    feature = settings.features.get(feature_name, Feature())
    amplitude = max(float(np.std(clean[:split])) * 8, feature.noise_floor * 20)
    # Disjoint episodes retain healthy intervals and all six fault families.
    episodes = []
    spacing = (size - split) // 7
    for n, kind in enumerate(("spike", "missing", "noise", "stuck", "bias", "drift")):
        start = split + (n + 1) * spacing
        duration = 1 if kind == "spike" else min(12, spacing - 2)
        magnitude = (
            (amplitude if clean_measurements is not None else 8)
            if kind != "drift"
            else (amplitude * 0.625 if clean_measurements is not None else 5)
        )
        fault = Fault(kind, duration, magnitude)
        episodes.append({"kind": kind, "start": start, "length": duration, "magnitude": magnitude})
        for i in range(start, start + duration):
            observed[i] = corrupt(float(clean[i]), fault, fault_rng)
            labels[i] = kind
    points = [Point(t, v) for t, v in zip(times, observed)]
    truth = [label != "normal" for label in labels[split:]]
    detectors = []
    for name, factory in DETECTORS.items():
        detector = factory(settings.min_history, settings.detection)
        predictions = []
        for i in range(split, size):
            result = detector.detect(points[i], points[max(0, i - settings.window) : i], feature)
            predictions.append(result.flagged)
        metrics = detection_metrics(truth, predictions)
        per_type = {}
        for kind in ("spike", "missing", "noise", "stuck", "bias", "drift"):
            indexes = [i for i, label in enumerate(labels[split:]) if label == kind]
            per_type[kind] = sum(predictions[i] for i in indexes) / len(indexes)
        delays = []
        for episode in episodes:
            if episode["kind"] == "drift":
                detected = next(
                    (j for j in range(episode["length"]) if predictions[episode["start"] - split + j]), None
                )
                delays.append(
                    (times[episode["start"] + detected] - times[episode["start"]]).total_seconds()
                    if detected is not None
                    else None
                )
        detectors.append(
            {"method": name, **metrics, "recall_by_type": per_type, "drift_delay_seconds": delays}
        )
    corrections = []
    # Conditional correction benchmark: fault locations are supplied (oracle mask).
    # This isolates reconstruction quality, and is NOT an end-to-end result.
    for name, factory in CORRECTORS.items():
        offline = name in ("linear", "time", "backward_fill")
        errors = []
        targets = [i for i in range(split, size) if labels[i] != "normal"]
        for i in targets:
            past = [points[j] for j in range(max(0, i - settings.window), i) if labels[j] == "normal"]
            future = (
                [points[j] for j in range(i + 1, min(size, i + 6)) if labels[j] == "normal"]
                if offline
                else []
            )
            c = factory().correct(points[i], past, feature, future=future, offline=offline)
            if c.value is not None:
                errors.append(c.value - clean[i])
        corrections.append(
            {
                "method": name,
                "mode": "offline" if offline else "online",
                "mae": float(np.mean(np.abs(errors))) if errors else None,
                "rmse": float(np.sqrt(np.mean(np.square(errors)))) if errors else None,
                "coverage": len(errors) / len(targets),
                "reconstructed": len(errors),
                "targets": len(targets),
            }
        )
    report = {
        "id": uuid4().hex,
        "seed": seed,
        "size": size,
        "train_size": split,
        "test_size": size - split,
        "split": "temporal_prefix",
        "confidence_kind": "heuristic_score",
        "detector_protocol": "walk_forward_raw_past_window; IsolationForest refit on past only",
        "correction_protocol": "conditional_on_oracle_fault_mask; not end-to-end; offline separated",
        "versions": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "sklearn": sklearn.__version__,
        },
        "dataset": {
            "kind": "historical_clean" if clean_measurements is not None else "synthetic",
            "sensor_id": sensor_id,
            "feature": feature_name,
            "unit": unit,
        },
        "settings": settings.model_dump(),
        "episodes": episodes,
        "detectors": detectors,
        "correctors": corrections,
        "pipeline": score_pipeline(points, clean, labels, split, settings, feature),
        "ground_truth": [
            {
                "measurement_id": f"experiment-{seed}-{i}",
                "timestamp": times[i].isoformat(),
                "sensor_id": sensor_id,
                "clean_value": float(clean[i]),
                "corrupted_value": observed[i],
                "corruption_type": labels[i],
                "parameters": next(
                    (
                        {"magnitude": e["magnitude"], "duration": e["length"], "age": i - e["start"]}
                        for e in episodes
                        if e["start"] <= i < e["start"] + e["length"]
                    ),
                    {},
                ),
            }
            for i in range(size)
        ],
    }
    if output:
        target = Path(output)
        target.mkdir(parents=True, exist_ok=True)
        (target / f"{report['id']}.json").write_text(
            json.dumps(report, indent=2, allow_nan=False), encoding="utf-8"
        )
        lines = [
            "# AgroData Guard benchmark",
            f"Seed: {seed}; temporal split {split}/{size - split}.",
            "",
            "| Detector | Precision | Recall | F1 | FPR |",
            "|---|---:|---:|---:|---:|",
        ]
        for row in detectors:
            lines.append(
                f"| {row['method']} | {row['precision']:.3f} | {row['recall']:.3f} | {row['f1']:.3f} | {row['false_positive_rate']:.3f} |"
            )
        lines += [
            "",
            "## Production pipeline (causal, no oracle fault mask)",
            json.dumps(report["pipeline"], indent=2),
            "",
            "Correction uses an oracle fault mask; offline methods use future observations.",
            "See JSON for correction coverage, errors, drift delays, ground truth and configuration.",
        ]
        (target / f"{report['id']}.md").write_text("\n".join(lines), encoding="utf-8")
    return report
