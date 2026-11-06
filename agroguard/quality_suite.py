"""Controlled regression scenarios, not a claim about accuracy on real sensor networks."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from .config import Feature
from .detectors import Point
from .evaluation import score_pipeline

DEFAULT_SEEDS = (11, 29, 47, 83, 101)


def previous_rules(settings):
    reference = settings.model_copy(deep=True)
    reference.detection = settings.detection.model_copy(
        update={
            "max_gap_periods": None,
            "rate_recovery_guard": False,
            "trend_require_change": False,
        }
    )
    return reference


def scenarios(settings, seed):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    feature = settings.features.get("temperature", Feature())
    for index, name in enumerate(
        (
            "healthy_noise",
            "established_warming",
            "daily_cycle",
            "spikes",
            "noise",
            "stuck",
            "bias",
            "drift",
            "unobserved_outage",
            "explicit_missing",
        )
    ):
        rng = np.random.default_rng(np.random.SeedSequence([seed, index]))
        size = 360 if name == "daily_cycle" else 160
        spec = feature.model_copy(update={"period_seconds": 600}) if name == "daily_cycle" else feature
        clean = 20 + rng.normal(0, 0.15, size)
        times = [start + timedelta(seconds=i * spec.period_seconds) for i in range(size)]
        labels = ["normal"] * size
        if name == "established_warming":
            clean = 15 + np.arange(size) * 0.25 + rng.normal(0, 0.03, size)
        elif name == "daily_cycle":
            clean = 23 + 4 * np.sin(np.arange(size) * 2 * np.pi / 144) + rng.normal(0, 0.15, size)
        elif name == "unobserved_outage":
            clean[70:] += 5
            times[70:] = [t + timedelta(seconds=30 * spec.period_seconds) for t in times[70:]]
        elif name == "explicit_missing":
            clean[80:] += 5
        observed = clean.tolist()
        if name == "spikes":
            for i, magnitude in ((70, 25), (110, -25), (140, 25)):
                observed[i] += magnitude
                labels[i] = "spike"
        elif name in ("noise", "stuck", "bias", "drift"):
            for i in range(70, 95):
                observed[i] = {
                    "noise": clean[i] + float(rng.normal(0, 8)),
                    "stuck": clean[70],
                    "bias": clean[i] + 8,
                    "drift": clean[i] + (i - 69) * 0.2,
                }[name]
                labels[i] = name
        elif name == "explicit_missing":
            for i in range(70, 80):
                observed[i], labels[i] = None, "missing"
        yield name, [Point(t, v) for t, v in zip(times, observed)], clean, labels, spec


def compare_quality(settings, seeds=DEFAULT_SEEDS, output=None):
    if not seeds or any(seed < 0 for seed in seeds):
        raise ValueError("At least one nonnegative seed is required")
    reference = previous_rules(settings)
    rows, ground_truth = [], []
    for seed in seeds:
        for name, points, clean, labels, feature in scenarios(settings, seed):
            for label, config in (("previous_rules", reference), ("current", settings)):
                metrics = score_pipeline(points, clean, labels, settings.window, config, feature)
                rows.append({"seed": seed, "scenario": name, "variant": label, "metrics": metrics})
            ground_truth.append(
                {
                    "seed": seed,
                    "scenario": name,
                    "feature": feature.model_dump(),
                    "points": [
                        {
                            "timestamp": p.timestamp.isoformat(),
                            "value": p.value,
                            "clean_value": float(c),
                            "corruption_type": kind,
                        }
                        for p, c, kind in zip(points, clean, labels)
                    ],
                }
            )
    totals = {}
    for variant in ("previous_rules", "current"):
        entries = [r["metrics"] for r in rows if r["variant"] == variant]
        totals[variant] = {key: sum(m["detection"][key] for m in entries) for key in ("tp", "fp", "fn", "tn")}
        totals[variant].update(
            healthy_suggestions=sum(m["correction"]["healthy_suggestions"] for m in entries),
            review_items=sum(m["review_items"] for m in entries),
        )
    report = {
        "suite": "quality-regression-v1",
        "seeds": list(seeds),
        "settings": settings.model_dump(),
        "reference_settings": reference.model_dump(),
        "totals": totals,
        "results": rows,
        "scope": "Synthetic targeted regression suite; thresholds fixed before evaluation; not real-world validation",
        "ground_truth": ground_truth,
    }
    if output:
        target = Path(output)
        target.mkdir(parents=True, exist_ok=True)
        (target / "comparison.json").write_text(
            json.dumps(report, indent=2, allow_nan=False), encoding="utf-8"
        )
        lines = [
            "# Сравнение точности AgroData Guard",
            "",
            f"Seeds: {list(seeds)}.",
            "Один и тот же production engine, walk-forward; ground truth не участвует в решениях.",
            "",
            "| Сценарий | FP до | FP после | TP до | TP после | FN до | FN после |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
        for name in dict.fromkeys(r["scenario"] for r in rows):
            counts = {}
            for variant in totals:
                selected = [
                    r["metrics"]["detection"]
                    for r in rows
                    if r["scenario"] == name and r["variant"] == variant
                ]
                counts[variant] = {k: sum(m[k] for m in selected) for k in ("fp", "tp", "fn")}
            a, b = counts["previous_rules"], counts["current"]
            lines.append(
                f"| {name} | {a['fp']} | {b['fp']} | {a['tp']} | {b['tp']} | {a['fn']} | {b['fn']} |"
            )
        lines += [
            "",
            "Итоги:",
            "```json",
            json.dumps(totals, indent=2),
            "```",
            "",
            "Это специально подобранные синтетические regression-сценарии, не оценка реальной сети.",
            "В JSON сохранены параметры, ground truth, coverage, ошибки коррекции, нагрузка на review и метрики по seed.",
        ]
        (target / "comparison.md").write_text("\n".join(lines), encoding="utf-8")
    return report
