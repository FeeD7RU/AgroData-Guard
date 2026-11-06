from datetime import datetime, timedelta, timezone
import math

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from agroguard.api import create_app
from agroguard.database import Measurement
from agroguard.simulator import Simulator
from fastapi.testclient import TestClient


def measurement(i=0, **changes):
    return {
        "sensor_id": "t1",
        "timestamp": (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=i)).isoformat(),
        "value": 20,
        "feature": "temperature",
        "unit": "°C",
        **changes,
    }


def seed_anomaly(client):
    assert (
        client.post(
            "/api/v1/measurements/batch",
            json=[measurement(i, value=20 + 0.1 * math.sin(i)) for i in range(20)],
        ).status_code
        == 200
    )
    response = client.post("/api/v1/measurements", json=measurement(20, value=90))
    assert response.status_code == 201
    return response.json()["id"]


def test_duplicate_timezone_conflict(client):
    first = client.post("/api/v1/measurements", json=measurement())
    repeated = client.post("/api/v1/measurements", json=measurement(timestamp="2026-01-01T03:00:00+03:00"))
    assert repeated.json()["duplicate"]
    assert first.json()["id"] == repeated.json()["id"]
    assert client.post("/api/v1/measurements", json=measurement(value=30)).status_code == 409
    assert client.post("/api/v1/measurements", json=measurement(measurement_id="other")).status_code == 409
    assert client.get("/api/v1/status").json()["measurements"] == 1


def test_atomic_batch_rollback(client):
    client.post("/api/v1/measurements", json=measurement())
    assert (
        client.post("/api/v1/measurements/batch", json=[measurement(1), measurement(value=31)]).status_code
        == 409
    )
    assert client.get("/api/v1/measurements").json()["total"] == 1


def test_duplicate_inside_batch_preserves_order(client):
    response = client.post(
        "/api/v1/measurements/batch", json=[measurement(2), measurement(1), measurement(2)]
    )
    assert [r["duplicate"] for r in response.json()["items"]] == [False, False, True]


def test_invalid_batch_and_nonfinite_json(client):
    assert client.post("/api/v1/measurements/batch", json=[]).status_code == 422
    assert (
        client.post("/api/v1/measurements/batch", json=[measurement(i) for i in range(501)]).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/measurements",
            content='{"sensor_id":"s","value":NaN,"timestamp":"2026-01-01T00:00:00Z"}',
            headers={"Content-Type": "application/json"},
        ).status_code
        == 422
    )
    assert client.post("/api/v1/measurements", content="x" * 1048577).status_code == 413
    assert client.post("/api/v1/measurements", content="{").status_code == 422


def test_out_of_order_is_causal(client):
    client.post("/api/v1/measurements", json=measurement(100, value=70))
    response = client.post("/api/v1/measurements", json=measurement(0, value=None))
    detail = client.get("/api/v1/anomalies/" + response.json()["id"]).json()["analysis"]
    assert detail["is_late"] is True
    assert detail["suggested_value"] is None
    assert detail["correction_confidence"] == 0
    received = client.get("/api/v1/measurements?order=received_at").json()["items"]
    event = client.get("/api/v1/measurements?order=timestamp").json()["items"]
    assert received[0]["raw_value"] is None
    assert event[0]["raw_value"] == 70


def test_sensor_metadata_conflict_and_unknown_feature(client):
    client.post("/api/v1/measurements", json=measurement())
    assert client.post("/api/v1/measurements", json=measurement(1, unit="F")).status_code == 409
    assert (
        client.post(
            "/api/v1/measurements", json=measurement(sensor_id="ph", feature="soil_ph", unit="pH", value=6.8)
        ).status_code
        == 201
    )


def test_review_audit_and_raw_immutable(client):
    ident = seed_anomaly(client)
    detail = client.get(f"/api/v1/anomalies/{ident}").json()["analysis"]
    assert detail["raw_value"] == 90 and 19.9 < detail["suggested_value"] < 20.1
    assert detail["anomaly_confidence"] != detail["correction_confidence"]
    for version, action in enumerate(["approve", "reject", "manual_review", "ignore"], 1):
        response = client.post(
            f"/api/v1/anomalies/{ident}/reviews",
            json={"action": action, "reviewer": "student", "expected_version": version, "note": "checked"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["raw_value"] == 90
        assert response.json()["effective_value"] == (
            detail["suggested_value"] if action == "approve" else 90
        )
    assert (
        client.post(
            f"/api/v1/anomalies/{ident}/reviews",
            json={"action": "approve", "reviewer": "student", "expected_version": 1},
        ).status_code
        == 409
    )
    assert len(client.get(f"/api/v1/anomalies/{ident}").json()["reviews"]) == 4
    with client.app.state.sessions.begin() as session:
        with pytest.raises(IntegrityError):
            session.execute(text("UPDATE measurements SET raw_value=0 WHERE id=:id"), {"id": ident})


def test_manual_approval_without_suggestion(client):
    result = client.post("/api/v1/measurements", json=measurement(value=None)).json()
    path = f"/api/v1/anomalies/{result['id']}/reviews"
    review = {"action": "approve", "reviewer": "operator", "expected_version": 1}
    assert client.post(path, json=review).status_code == 409
    assert client.post(path, json={**review, "value": 22}).json()["accepted_value"] == 22


def test_real_simulator_http_pipeline(client, settings):
    sim = Simulator(settings, datetime(2026, 1, 1, tzinfo=timezone.utc))
    warmup = sim.step(3600)
    assert (
        client.post(
            "/api/v1/measurements/batch", json=[m.model_dump(mode="json") for m, t in warmup]
        ).status_code
        == 200
    )
    sid = list(sim.channels)[0]
    sim.inject(sid, "spike", 1, 80)
    faulty = sim.step(60)
    client.post("/api/v1/measurements/batch", json=[m.model_dump(mode="json") for m, t in faulty])
    spike = next(m for m, t in faulty if t["corruption_type"] == "spike")
    row = client.get("/api/v1/anomalies/" + spike.measurement_id).json()["analysis"]
    assert row["is_anomaly"] and row["anomaly_type"] == "out_of_range"
    assert row["suggested_value"] is not None
    with client.app.state.sessions() as session:
        assert session.get(Measurement, spike.measurement_id).raw_value == spike.value
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert "/api/v1/measurements" in client.get("/openapi.json").json()["paths"]


def test_demo_multiple_faults_and_lifecycle(client):
    demo = client.app.state.demo
    started = demo.start()
    assert started["running"]
    demo.stop()
    sim = demo.simulator
    sids = list(sim.channels)
    for sid, kind in zip(sids, ["missing", "stuck", "drift"]):
        sim.inject(sid, kind, 18, 30)
    demo.persist(sim.step(2400))
    kinds = {a["anomaly_type"] for a in client.get("/api/v1/anomalies?limit=1000").json()["items"]}
    assert {"missing", "stuck", "drift"} <= kinds


def test_storage_survives_restart(tmp_path):
    url = f"sqlite:///{tmp_path / 'persist.db'}"
    with TestClient(create_app(url, auto_demo=False, api_key="")) as c:
        seed_anomaly(c)
    with TestClient(create_app(url, auto_demo=False, api_key="")) as c:
        assert c.get("/api/v1/status").json()["measurements"] == 21


def test_api_key(tmp_path):
    with TestClient(create_app(f"sqlite:///{tmp_path / 'key.db'}", api_key="secret", auto_demo=False)) as c:
        assert c.get("/api/v1/status").status_code == 401
        assert c.get("/api/v1/health").status_code == 200
        assert c.get("/api/v1/status", headers={"X-API-Key": "secret"}).status_code == 200


def test_concurrent_duplicate_delivery(client):
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(
            pool.map(lambda _: client.post("/api/v1/measurements", json=measurement()), range(8))
        )
    assert all(r.status_code == 201 for r in responses)
    assert sum(not r.json()["duplicate"] for r in responses) == 1
    assert client.get("/api/v1/status").json()["measurements"] == 1


def test_database_failure_is_safe_503(client, monkeypatch):
    from sqlalchemy.exc import OperationalError

    def fail(_):
        raise OperationalError("hidden database details", {}, Exception("storage unavailable"))

    monkeypatch.setattr(client.app.state.guard, "ingest", fail)
    response = client.post("/api/v1/measurements", json=measurement())
    assert response.status_code == 503
    assert "hidden database details" not in response.text
