from datetime import datetime, timedelta, timezone

import pytest
import numpy as np

from agroguard.config import Feature, Policy
from agroguard.correctors import CORRECTORS, Correction
from agroguard.detectors import DETECTORS, Detection, Point, diagnose
from agroguard.policy import decide

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def series(values):
    return [Point(START + timedelta(minutes=i), v) for i, v in enumerate(values)]


@pytest.mark.parametrize("name", ["range", "rate", "zscore", "robust_z", "iqr", "isolation_forest"])
def test_detect_large_spike(name):
    history = series(np.random.default_rng(42).normal(20, 0.2, 48))
    point = Point(START + timedelta(minutes=48), 95)
    d = DETECTORS[name]().detect(point, history, Feature(min=-50, max=70, max_rate=12))
    assert d.flagged
    assert 0.8 <= d.score <= 1


def test_unknown_bounds_not_applied():
    assert not DETECTORS["range"]().detect(Point(START, 500), [], Feature()).flagged


def test_stuck_noise_drift():
    cases = [
        ("stuck", [20 + i % 2 for i in range(20)] + [21] * 8, 21),
        ("rolling", [20] * 20 + [17, 26, 13, 27, 12], 30),
        ("trend", [20 + 0.01 * (i % 2) for i in range(30)] + [20 + i for i in range(1, 12)], 32),
    ]
    for name, xs, v in cases:
        assert (
            DETECTORS[name]()
            .detect(Point(START + timedelta(minutes=len(xs)), v), series(xs), Feature())
            .flagged
        )


@pytest.mark.parametrize("name", list(DETECTORS))
def test_detector_missing_and_warmup(name):
    detector = DETECTORS[name]()
    assert detector.detect(Point(START, None), [], Feature()).hint == "missing"
    assert not detector.detect(Point(START, 20), [], Feature()).flagged


def test_diagnosis_separate_from_detection():
    assert diagnose([Detection("x", True, 0.83, "unknown", "deviation")]) == (True, "unknown", 0.83)
    assert diagnose([], "bad") == (True, "unknown", 0.8)


@pytest.mark.parametrize("name", ["mean", "median", "forward_fill"])
def test_causal_correction(name):
    past = series([20] * 12)
    result = CORRECTORS[name]().correct(Point(START + timedelta(minutes=12), 100), past, Feature())
    assert result.value == 20
    assert 0 < result.score < 1


@pytest.mark.parametrize("name", ["linear", "time", "backward_fill"])
def test_offline_requires_explicit_permission(name):
    point = Point(START + timedelta(minutes=1), None)
    past = [Point(START, 10)]
    future = [Point(START + timedelta(minutes=3), 40)]
    assert CORRECTORS[name]().correct(point, past, Feature(), future).value is None
    result = CORRECTORS[name]().correct(point, past, Feature(), future, True)
    assert result.value == {"linear": 20, "time": 20, "backward_fill": 40}[name]


def test_interpolation_irregular_timestamps():
    point = Point(START + timedelta(seconds=30), None)
    past = [Point(START, 10)]
    future = [Point(START + timedelta(minutes=3), 40)]
    assert CORRECTORS["linear"]().correct(point, past, Feature(), future, True).value is None
    assert CORRECTORS["time"]().correct(point, past, Feature(), future, True).value == 15


def test_gap_penalizes_and_eventually_abstains():
    corrector = CORRECTORS["median"]()
    past = series([20] * 12)
    near = corrector.correct(Point(START + timedelta(minutes=12), None), past, Feature())
    far = corrector.correct(Point(START + timedelta(minutes=14), None), past, Feature())
    assert far.score < near.score
    assert corrector.correct(Point(START + timedelta(minutes=20), None), past, Feature()).value is None


@pytest.mark.parametrize(
    "flag,score,correction,auto,expected",
    [
        (False, 0, 0.9, False, "normal"),
        (True, 0.4, 0.9, False, "observation"),
        (True, 0.95, 0.4, False, "manual_review"),
        (True, 0.95, 0.9, False, "suggested_correction"),
        (True, 0.95, 0.9, True, "auto_approved"),
    ],
)
def test_policy(flag, score, correction, auto, expected):
    assert (
        decide(flag, score, Correction("median", 20, correction, "test"), Policy(auto_correct=auto))
        == expected
    )
