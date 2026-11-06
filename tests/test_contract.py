from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from agroguard.schemas import SensorMeasurement


def payload(**changes):
    return {"sensor_id": "sensor.1", "timestamp": "2026-01-01T00:00:00Z", "value": 12, **changes}


@pytest.mark.parametrize(
    "change",
    [
        {"timestamp": "2026-01-01T00:00:00"},
        {"timestamp": "broken"},
        {"timestamp": 123},
        {"value": float("nan")},
        {"value": float("inf")},
        {"value": True},
        {"value": "12"},
        {"sensor_id": ""},
        {"schema_version": "2"},
        {"unexpected": 1},
        {"sequence_number": -1},
        {"metadata": {"v": float("nan")}},
    ],
)
def test_invalid_contract(change):
    with pytest.raises(ValidationError):
        SensorMeasurement.model_validate(payload(**change))


def test_missing_value_field():
    with pytest.raises(ValidationError):
        SensorMeasurement(sensor_id="s", timestamp=datetime.now(timezone.utc))


def test_timezone_and_deterministic_id():
    utc = SensorMeasurement.model_validate(payload())
    offset = SensorMeasurement.model_validate(payload(timestamp="2026-01-01T03:00:00+03:00"))
    assert utc.timestamp == offset.timestamp
    assert utc.measurement_id == offset.measurement_id


def test_unknown_feature_and_explicit_null():
    m = SensorMeasurement.model_validate(payload(feature="soil_ph", value=None))
    assert m.value is None and m.feature == "soil_ph"
