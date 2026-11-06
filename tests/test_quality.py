from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from pydantic import ValidationError

from agroguard.config import DetectionParameters, Feature, Settings
from agroguard.detectors import DETECTORS, Point
from agroguard.engine import ALGORITHM_VERSION
from agroguard.evaluation import replay_pipeline, score_pipeline
from agroguard.quality_suite import compare_quality, previous_rules

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def points(values, seconds=60):
    return [
        Point(START + timedelta(seconds=i * seconds), float(v) if v is not None else None)
        for i, v in enumerate(values)
    ]


def test_isolated_spike_does_not_alarm_on_recovery(settings):
    samples = points([20 + 0.1 * np.sin(i) for i in range(40)] + [45, 20.1])
    feature = settings.features["temperature"]
    current = replay_pipeline(samples, settings, feature)
    previous = replay_pipeline(samples, previous_rules(settings), feature)
    assert current[-2].is_anomaly and previous[-2].is_anomaly
    assert previous[-1].is_anomaly and not current[-1].is_anomaly
    assert any(d.reason_code == "rate_recovery" for d in current[-1].evidence)


@pytest.mark.parametrize("suffix", [[45, -10], [13, 42, 20.3]])
def test_recovery_guard_preserves_repeated_faults(settings, suffix):
    samples = points([20 + 0.1 * np.sin(i) for i in range(40)] + suffix)
    outcomes = replay_pipeline(samples, settings, settings.features["temperature"])
    assert outcomes[-1].is_anomaly
    assert not any(d.reason_code == "rate_recovery" for d in outcomes[-1].evidence)


def test_recovery_guard_abstains_without_reference():
    samples = points([20, 45, 20])
    result = DETECTORS["rate"]().detect(samples[-1], samples[:-1], Feature(max_rate=12))
    assert result.flagged


def test_established_trend_is_not_drift():
    samples = points([20 + 0.25 * i for i in range(49)])
    result = DETECTORS["trend"]().detect(samples[-1], samples[:-1], Feature())
    assert not result.flagged and result.reason_code == "trend_consistent_with_past"


@pytest.mark.parametrize("period", [30, 60, 120])
def test_new_trend_detected_using_cadence(period):
    samples = points([20 + 0.02 * (i % 2) for i in range(36)] + [20 + 0.5 * i for i in range(1, 13)], period)
    result = DETECTORS["trend"]().detect(samples[-1], samples[:-1], Feature(period_seconds=period))
    assert result.flagged and result.hint == "drift"


@pytest.mark.parametrize("name", ["robust_z", "zscore", "iqr", "stuck", "trend", "rolling", "rate"])
def test_old_context_not_reused_across_outage(name):
    history = points([20] * 48)
    after_outage = Point(history[-1].timestamp + timedelta(hours=1), 40 if name != "stuck" else 20)
    result = DETECTORS[name]().detect(after_outage, history, Feature(max_rate=0.1))
    assert not result.flagged


def test_nulls_cannot_bridge_long_outage(settings):
    samples = points([20 + 0.1 * np.sin(i) for i in range(40)] + [None] * 10 + [25, 25.1])
    results = replay_pipeline(samples, settings, settings.features["temperature"])
    assert all(r.anomaly_type == "missing" for r in results[40:50])
    assert not results[-2].is_anomaly and not results[-1].is_anomaly


def test_range_rule_still_applies_without_recent_context():
    history = points([20] * 48)
    p = Point(START + timedelta(days=1), 95)
    assert DETECTORS["range"]().detect(p, history, Feature(max=70)).flagged


def test_future_values_never_enter_detector_context():
    history = points([20 + 0.1 * np.sin(i) for i in range(40)])
    p = Point(START + timedelta(minutes=40), 20.1)
    detector = DETECTORS["robust_z"]()
    assert detector.detect(p, history, Feature()) == detector.detect(
        p, history + [Point(START + timedelta(minutes=41), 999)], Feature()
    )


@pytest.mark.parametrize(
    "change",
    [
        {"max_gap_periods": 0},
        {"recovery_band": 5},
        {"robust_z_threshold": float("nan")},
        {"trend_samples": 2},
    ],
)
def test_detection_config_validation(change):
    with pytest.raises(ValidationError):
        DetectionParameters(**change)


def test_insufficient_trend_window_rejected(settings):
    payload = settings.model_dump()
    payload["window"] = 12
    with pytest.raises(ValidationError):
        Settings.model_validate(payload)


def test_replay_matches_persisted_production_decisions(client, settings):
    samples = points([20 + 0.1 * np.sin(i) for i in range(40)] + [45, 20.1, None, 20.2])
    outcomes = replay_pipeline(samples, settings, settings.features["temperature"])
    payloads = [
        {
            "sensor_id": "parity",
            "feature": "temperature",
            "unit": "°C",
            "timestamp": p.timestamp.isoformat(),
            "value": p.value,
        }
        for p in samples
    ]
    assert client.post("/api/v1/measurements/batch", json=payloads).status_code == 200
    rows = client.get("/api/v1/measurements?sensor_id=parity&limit=100").json()["items"][::-1]
    for stored, predicted in zip(rows, outcomes):
        assert stored["algorithm_version"] == ALGORITHM_VERSION
        assert stored["policy"]["analysis_config"]["detection"] == settings.detection.model_dump()
        assert stored["is_anomaly"] == predicted.is_anomaly
        assert stored["decision"] == predicted.decision
        assert stored["suggested_value"] == predicted.correction.value
        assert stored["correction_confidence"] == predicted.correction.score
    anomaly = rows[40]
    assert (
        client.post(
            f"/api/v1/anomalies/{anomaly['id']}/reviews",
            json={"action": "approve", "reviewer": "test", "expected_version": 1},
        ).status_code
        == 200
    )


def test_evaluation_reports_real_healthy_suggestions(settings):
    samples = points([20 + 0.1 * np.sin(i) for i in range(40)] + [45, 20.1])
    clean = [p.value for p in samples]
    labels = ["normal"] * len(samples)
    scores = score_pipeline(samples, clean, labels, 12, settings, settings.features["temperature"])
    assert scores["detection"]["fp"] == 1
    assert scores["correction"]["healthy_suggestions"] == 1
    assert scores["correction"]["coverage"] is None
    relabeled = score_pipeline(
        samples, clean, labels[:-2] + ["spike", "normal"], 12, settings, settings.features["temperature"]
    )
    assert relabeled["detection"]["tp"] == 1
    assert relabeled["decisions"] == scores["decisions"]  # changing truth cannot change the agent


def test_quality_suite_has_no_recall_regression_on_holdout_seed(settings, tmp_path):
    report = compare_quality(settings, seeds=[137], output=tmp_path)
    old, new = report["totals"]["previous_rules"], report["totals"]["current"]
    assert new["fp"] < old["fp"]
    assert new["tp"] >= old["tp"]
    assert new["healthy_suggestions"] < old["healthy_suggestions"]
    assert (tmp_path / "comparison.json").exists()
