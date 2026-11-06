from datetime import datetime, timezone

import numpy as np
import pytest

from agroguard.benchmark import detection_metrics, run_benchmark
from agroguard.simulator import Fault, Simulator, corrupt
from agroguard.sources import CSVSource
from agroguard.schemas import SensorMeasurement


@pytest.mark.parametrize("kind", ["missing", "spike", "noise", "stuck", "bias", "drift"])
def test_faults(kind):
    fault = Fault(kind, 3, 10)
    rng = np.random.default_rng(42)
    values = [corrupt(20 + i, fault, rng) for i in range(3)]
    assert fault.age == 3
    if kind == "missing":
        assert values == [None] * 3
    elif kind == "stuck":
        assert values == [20] * 3
    elif kind == "drift":
        assert values[-1] - 22 > values[0] - 20
    else:
        assert values != [20, 21, 22]


def test_simulator_reproducible_and_configurable(settings):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    a, b = Simulator(settings, start), Simulator(settings, start)
    assert a.step(600) == b.step(600)
    settings.simulation.features["soil_ph"] = settings.simulation.features["temperature"].model_copy(
        update={"base": 6.5}
    )
    assert any(m.feature == "soil_ph" for m, truth in Simulator(settings, start).step(60))


def test_clean_signals_independent_of_batch_size(settings):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    a, b = Simulator(settings, start), Simulator(settings, start)
    whole = a.step(600)
    chunks = [row for _ in range(10) for row in b.step(60)]
    assert whole == sorted(chunks, key=lambda row: (row[0].timestamp, row[0].sensor_id))


def test_csv_mapping(tmp_path):
    path = tmp_path / "data.csv"
    path.write_text(
        "when,channel,reading\n2026-01-01T00:00:00Z,ph,6.2\n2026-01-01T00:01:00Z,ph,\n", encoding="utf-8"
    )
    rows = list(
        CSVSource(
            path, {"timestamp": "when", "sensor_id": "channel", "value": "reading"}, {"feature": "soil_ph"}
        ).read()
    )
    assert rows[0].value == 6.2 and rows[1].value is None
    with pytest.raises(ValueError):
        list(CSVSource(path, {"value": "no_such_column"}).read())


def test_metric_denominators():
    scores = detection_metrics([True, True, False, False], [True, False, True, False])
    assert scores["precision"] == scores["recall"] == scores["f1"] == scores["false_positive_rate"] == 0.5


def test_benchmark_reproducible_temporal_and_saved(settings, tmp_path):
    a = run_benchmark(settings, size=200, output=str(tmp_path))
    b = run_benchmark(settings, size=200)
    assert a["detectors"] == b["detectors"]
    assert a["correctors"] == b["correctors"]
    assert a["pipeline"] == b["pipeline"]
    assert a["ground_truth"] == b["ground_truth"]
    assert all(p["corruption_type"] == "normal" for p in a["ground_truth"][: a["train_size"]])
    assert len(a["detectors"]) == 9 and len(a["correctors"]) == 6
    assert len(list(tmp_path.glob("*.json"))) == 1
    assert all(0 <= c["coverage"] <= 1 for c in a["correctors"])


def test_benchmark_api(client):
    response = client.post("/api/v1/experiments", json={"seed": 42, "size": 200})
    assert response.status_code == 201
    ident = response.json()["id"]
    assert response.json()["pipeline"]["algorithm_version"] == "baseline-v2"
    assert client.get(f"/api/v1/experiments/{ident}").json()["ground_truth"]
    assert client.get("/api/v1/experiments").json()["items"][0]["id"] == ident


def test_historical_benchmark_is_generic(settings):
    from datetime import timedelta

    rows = [
        SensorMeasurement(
            sensor_id="ph",
            feature="soil_ph",
            unit="pH",
            value=6.5 + 0.01 * np.sin(i),
            timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=i * 2),
        )
        for i in range(200)
    ]
    report = run_benchmark(settings, clean_measurements=rows[::-1])
    assert report["dataset"]["feature"] == "soil_ph"
    assert report["ground_truth"][0]["clean_value"] == 6.5
    assert report["train_size"] == 80
    assert report["ground_truth"][-1]["timestamp"] > report["ground_truth"][0]["timestamp"]
