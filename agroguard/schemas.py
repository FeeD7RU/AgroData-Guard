"""Stable v1 contract shared by gateways, HTTP and historical adapters."""

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SensorMeasurement(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    schema_version: Literal["1"] = "1"
    measurement_id: str | None = Field(default=None, min_length=1, max_length=128)
    timestamp: datetime
    sensor_id: str = Field(min_length=1, max_length=128, pattern=r"^[\w.:-]+$")
    value: float | None
    node_id: str | None = Field(default=None, max_length=128)
    feature: str = Field(default="unknown", min_length=1, max_length=80)
    unit: str | None = Field(default=None, max_length=32)
    location_id: str | None = Field(default=None, max_length=128)
    quality: Literal["good", "uncertain", "bad"] = "good"
    source: str = Field(default="unspecified", max_length=128)
    metadata: dict[str, Any] = Field(default_factory=dict)
    sequence_number: int | None = Field(default=None, ge=0)

    @field_validator("value", mode="before")
    @classmethod
    def numeric(cls, v):
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float))):
            raise ValueError("value must be a JSON number or null")
        return v

    @field_validator("timestamp", mode="before")
    @classmethod
    def time_format(cls, v):
        if not isinstance(v, (str, datetime)):
            raise ValueError("timestamp must be ISO 8601 with timezone")
        return v

    @field_validator("timestamp")
    @classmethod
    def utc(cls, v):
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("timestamp requires explicit timezone")
        return v.astimezone(timezone.utc)

    @field_validator("metadata")
    @classmethod
    def json_metadata(cls, v):
        import json

        if len(json.dumps(v, allow_nan=False).encode()) > 16384:
            raise ValueError("metadata exceeds 16 KiB")
        return v

    @model_validator(mode="after")
    def identity(self):
        if self.measurement_id is None:
            self.measurement_id = str(
                uuid5(NAMESPACE_URL, f"agroguard:{self.sensor_id}:{self.timestamp.isoformat()}")
            )
        return self


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    action: Literal["approve", "reject", "ignore", "manual_review"]
    reviewer: str = Field(min_length=1, max_length=120)
    note: str = Field(default="", max_length=2000)
    value: float | None = None
    expected_version: int = Field(ge=1)


class FaultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    sensor_id: str
    kind: Literal["missing", "spike", "noise", "stuck", "bias", "drift"]
    duration: int = Field(default=12, ge=1, le=120)
    magnitude: float = Field(default=25, ge=0, le=1000000)
