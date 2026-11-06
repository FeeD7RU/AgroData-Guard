"""Приём и ручная проверка в транзакциях; числовой анализ вынесен в QualityEngine."""

import hashlib
import json
import threading
from dataclasses import asdict
from datetime import datetime

from sqlalchemy import select, update

from .config import Feature
from .database import Analysis, Measurement, Node, Review, Sensor
from .detectors import Point
from .engine import ALGORITHM_VERSION, QualityEngine


class Conflict(Exception):
    pass


class NotFound(Exception):
    pass


def canonical_time(timestamp):
    return timestamp.isoformat(timespec="microseconds")


def serialize(measurement, analysis):
    data = {c.name: getattr(analysis, c.name) for c in Analysis.__table__.columns}
    data.update(
        sensor_id=measurement.sensor_id,
        timestamp=measurement.timestamp,
        raw_value=measurement.raw_value,
        received_at=measurement.received_at,
        payload=measurement.payload,
    )
    data["effective_value"] = (
        analysis.accepted_value
        if analysis.decision in ("approved", "auto_approved")
        else measurement.raw_value
    )
    return data


class Guard:
    def __init__(self, sessions, settings):
        self.sessions, self.settings = sessions, settings
        self.lock = threading.RLock()
        self.engine = QualityEngine(settings)

    def ingest(self, measurements):
        # Блокировка защищает историю и обработку дублей в пределах одного процесса.
        with self.lock, self.sessions.begin() as session:
            results = [None] * len(measurements)
            for index, m in sorted(
                enumerate(measurements), key=lambda item: (item[1].timestamp, item[1].sensor_id)
            ):
                payload = m.model_dump(mode="json")
                digest = hashlib.sha256(
                    json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
                ).hexdigest()
                old = session.get(Measurement, m.measurement_id)
                if old:
                    if old.payload_hash != digest:
                        raise Conflict("measurement_id already exists with different payload")
                    results[index] = {"id": m.measurement_id, "duplicate": True}
                    continue
                ts = canonical_time(m.timestamp)
                same_time = session.scalar(
                    select(Measurement.id).where(
                        Measurement.sensor_id == m.sensor_id, Measurement.timestamp == ts
                    )
                )
                if same_time:
                    raise Conflict("sensor_id + timestamp already exists with another measurement_id")
                sensor = session.get(Sensor, m.sensor_id)
                if sensor and (
                    sensor.feature != m.feature or sensor.unit != m.unit or sensor.node_id != m.node_id
                ):
                    raise Conflict("Sensor feature/unit/node is immutable; use a new sensor_id")
                if not sensor:
                    if m.node_id and not session.get(Node, m.node_id):
                        session.add(Node(id=m.node_id))
                        session.flush()
                    session.add(
                        Sensor(
                            id=m.sensor_id,
                            node_id=m.node_id,
                            feature=m.feature,
                            unit=m.unit,
                            location_id=m.location_id,
                        )
                    )
                    session.flush()
                feature = self.settings.features.get(m.feature, Feature())
                if feature.unit and m.unit != feature.unit:
                    raise Conflict(f"Expected unit {feature.unit}; normalize at gateway")
                past = session.execute(
                    select(Measurement, Analysis)
                    .join(Analysis)
                    .where(Measurement.sensor_id == m.sensor_id, Measurement.timestamp < ts)
                    .order_by(Measurement.timestamp.desc())
                    .limit(self.settings.window)
                ).all()[::-1]
                history = [Point(datetime.fromisoformat(p.timestamp), p.raw_value) for p, a in past]
                trusted = [
                    Point(
                        datetime.fromisoformat(p.timestamp),
                        a.accepted_value if a.decision in ("approved", "auto_approved") else p.raw_value,
                    )
                    for p, a in past
                    if a.decision in ("normal", "approved", "auto_approved")
                ]
                late = (
                    session.scalar(
                        select(Measurement.id)
                        .where(Measurement.sensor_id == m.sensor_id, Measurement.timestamp > ts)
                        .limit(1)
                    )
                    is not None
                )
                point = Point(m.timestamp, m.value)
                assessment = self.engine.analyze(point, history, trusted, feature, m.quality)
                anomaly, kind, score = (
                    assessment.is_anomaly,
                    assessment.anomaly_type,
                    assessment.anomaly_confidence,
                )
                correction, decision, evidence = (
                    assessment.correction,
                    assessment.decision,
                    assessment.evidence,
                )
                session.add(
                    Measurement(
                        id=m.measurement_id,
                        sensor_id=m.sensor_id,
                        timestamp=ts,
                        raw_value=m.value,
                        payload=payload,
                        payload_hash=digest,
                    )
                )
                session.flush()
                session.add(
                    Analysis(
                        id=m.measurement_id,
                        is_anomaly=anomaly,
                        anomaly_type=kind,
                        anomaly_confidence=score,
                        correction_confidence=correction.score,
                        detector=",".join(d.method for d in evidence if d.flagged),
                        corrector=correction.method if anomaly else None,
                        suggested_value=correction.value,
                        accepted_value=correction.value if decision == "auto_approved" else None,
                        decision=decision,
                        evidence=[asdict(d) for d in evidence] + [{"correction": correction.explanation}],
                        policy={
                            **self.settings.policy.model_dump(),
                            "analysis_config": {
                                "window": self.settings.window,
                                "min_history": self.settings.min_history,
                                "detectors": self.settings.detectors,
                                "corrector": self.settings.corrector,
                                "detection": self.settings.detection.model_dump(),
                                "feature": feature.model_dump(),
                            },
                        },
                        is_late=late,
                        algorithm_version=ALGORITHM_VERSION,
                    )
                )
                session.flush()
                results[index] = {"id": m.measurement_id, "duplicate": False}
            return results

    def review(self, ident, request):
        with self.lock, self.sessions.begin() as session:
            analysis = session.get(Analysis, ident)
            if not analysis:
                raise NotFound("Analysis not found")
            if not analysis.is_anomaly:
                raise Conflict("Normal observations do not require anomaly review")
            if analysis.version != request.expected_version:
                raise Conflict("Stale review version; refresh the item")
            if request.value is not None and request.action != "approve":
                raise Conflict("Manual value is only allowed for approve")
            accepted = None
            if request.action == "approve":
                accepted = request.value if request.value is not None else analysis.suggested_value
                if accepted is None:
                    raise Conflict("No suggestion; provide an explicit manual value")
            decision = {
                "approve": "approved",
                "reject": "rejected",
                "ignore": "ignored",
                "manual_review": "manual_review",
            }[request.action]
            previous, version = analysis.decision, analysis.version + 1
            result = session.execute(
                update(Analysis)
                .where(Analysis.id == ident, Analysis.version == request.expected_version)
                .values(decision=decision, accepted_value=accepted, version=version)
            )
            if result.rowcount != 1:
                raise Conflict("Concurrent review; refresh the item")
            session.add(
                Review(
                    analysis_id=ident,
                    reviewer=request.reviewer,
                    action=request.action,
                    note=request.note,
                    accepted_value=accepted,
                    previous_decision=previous,
                    version=version,
                )
            )
            session.flush()
            session.refresh(analysis)
            return serialize(session.get(Measurement, ident), analysis)
