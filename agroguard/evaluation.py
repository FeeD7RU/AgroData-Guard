"""Replay the production engine without ground truth in its inputs."""

from collections import Counter

import numpy as np

from .detectors import Point
from .engine import ALGORITHM_VERSION, QualityEngine
from .metrics import detection_metrics


def replay_pipeline(points, settings, feature):
    engine = QualityEngine(settings)
    trusted = {}
    assessments = []
    for i, point in enumerate(points):
        if i and point.timestamp <= points[i - 1].timestamp:
            raise ValueError("Replay requires strictly increasing timestamps")
        start = max(0, i - settings.window)
        past = points[start:i]
        accepted_past = [trusted[j] for j in range(start, i) if j in trusted]
        result = engine.analyze(point, past, accepted_past, feature)
        assessments.append(result)
        if result.decision == "normal":
            trusted[i] = point
        elif result.decision == "auto_approved":
            trusted[i] = Point(point.timestamp, result.correction.value)
        trusted.pop(start - 1, None)
    return assessments


def score_pipeline(points, clean, labels, split, settings, feature):
    if not (len(points) == len(clean) == len(labels)) or not 0 <= split < len(points):
        raise ValueError("Invalid evaluation lengths/split")
    # Truth is inspected only AFTER replay. No oracle mask, clean history or future correction.
    outcomes = replay_pipeline(points, settings, feature)
    tested = outcomes[split:]
    truth = [kind != "normal" for kind in labels[split:]]
    scores = detection_metrics(truth, [r.is_anomaly for r in tested])
    errors = [
        r.correction.value - clean[i]
        for i, r in enumerate(outcomes)
        if i >= split and labels[i] != "normal" and r.correction.value is not None
    ]
    per_type = {}
    for kind in sorted(set(labels[split:]) - {"normal"}):
        indexes = [i for i in range(split, len(points)) if labels[i] == kind]
        per_type[kind] = {
            "points": len(indexes),
            "detection_recall": sum(outcomes[i].is_anomaly for i in indexes) / len(indexes),
            "diagnosis_recall": sum(
                outcomes[i].is_anomaly and outcomes[i].anomaly_type == kind for i in indexes
            )
            / len(indexes),
        }
    delays = []
    for start in range(split, len(points)):
        if labels[start] != "drift" or (start > split and labels[start - 1] == "drift"):
            continue
        end = start + 1
        while end < len(points) and labels[end] == "drift":
            end += 1
        detected = next((i for i in range(start, end) if outcomes[i].is_anomaly), None)
        delays.append(
            (points[detected].timestamp - points[start].timestamp).total_seconds()
            if detected is not None
            else None
        )
    return {
        "algorithm_version": ALGORITHM_VERSION,
        "protocol": "production_engine_walk_forward; no_truth_in_analysis; causal_correction; no_human_reviews",
        "detection": scores,
        "recall_by_type": per_type,
        "drift_delay_seconds": delays,
        "decisions": dict(Counter(r.decision for r in tested)),
        "review_items": sum(r.decision in ("manual_review", "suggested_correction") for r in tested),
        "correction": {
            "fault_targets": sum(truth),
            "fault_suggestions": len(errors),
            "coverage": len(errors) / sum(truth) if sum(truth) else None,
            "mae": float(np.mean(np.abs(errors))) if errors else None,
            "rmse": float(np.sqrt(np.mean(np.square(errors)))) if errors else None,
            "healthy_suggestions": sum(
                not t and r.correction.value is not None for t, r in zip(truth, tested)
            ),
            "healthy_auto_approvals": sum(
                not t and r.decision == "auto_approved" for t, r in zip(truth, tested)
            ),
        },
        "rate_recoveries": sum(any(d.reason_code == "rate_recovery" for d in r.evidence) for r in tested),
    }
