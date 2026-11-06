"""Versioned REST API and local dashboard."""

import json
import logging
import os
import secrets
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from .benchmark import run_benchmark
from .agent import AgentBusy, AgentRequest, AgentUnavailable, Investigator
from .config import Feature, load_settings
from .database import Analysis, Experiment, Measurement, Review, Sensor, make_database
from .demo import DemoController
from .schemas import FaultRequest, ReviewRequest, SensorMeasurement
from .service import Conflict, Guard, NotFound, serialize

log = logging.getLogger("agroguard")


class JsonFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps(
            {
                "time": datetime.now(timezone.utc).isoformat(),
                "level": record.levelname,
                "event": record.getMessage(),
                "exception": self.formatException(record.exc_info) if record.exc_info else None,
                **getattr(record, "context", {}),
            }
        )


class Boundary:
    """Bound streamed bodies before parsing; protect API with optional shared key."""

    def __init__(self, app, limit, api_key):
        self.app, self.limit, self.api_key = app, limit, api_key

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        path = scope["path"]
        if self.api_key and path.startswith("/api/") and path != "/api/v1/health":
            supplied = headers.get(b"x-api-key", b"").decode("utf-8", errors="replace")
            if not secrets.compare_digest(supplied, self.api_key):
                return await JSONResponse({"detail": "API key required"}, 401)(scope, receive, send)
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > self.limit:
                return await JSONResponse({"detail": "Request body too large"}, 413)(scope, receive, send)
            if not message.get("more_body", False):
                break
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        start = time.monotonic()
        await self.app(scope, replay, send)
        log.info(
            "http_request",
            extra={
                "context": {
                    "request": scope["method"],
                    "path": path,
                    "duration_ms": round((time.monotonic() - start) * 1000, 1),
                }
            },
        )


class ExperimentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seed: int = Field(default=42, ge=0, le=2147483647)
    size: int = Field(default=300, ge=200, le=1500)


def create_app(database_url=None, settings=None, auto_demo=None, api_key=None):
    settings = settings or load_settings()
    engine, sessions = make_database(database_url or os.getenv("DATABASE_URL", "sqlite:///data/agroguard.db"))
    guard = Guard(sessions, settings)
    demo = DemoController(guard)
    investigator = Investigator(sessions, settings)
    experiment_lock = threading.Lock()
    auto_demo = os.getenv("AGRO_DEMO", "0") == "1" if auto_demo is None else auto_demo

    @asynccontextmanager
    async def lifespan(app):
        if auto_demo:
            demo.start()
        yield
        demo.stop()
        engine.dispose()

    app = FastAPI(
        title="AgroData Guard",
        version="0.1.0",
        lifespan=lifespan,
        description="Sensor quality API. All confidence fields are heuristic scores, not probabilities.",
    )
    app.state.guard, app.state.demo, app.state.sessions = guard, demo, sessions
    app.state.investigator = investigator
    app.add_middleware(
        Boundary,
        limit=settings.max_body_bytes,
        api_key=os.getenv("API_KEY", "") if api_key is None else api_key,
    )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Do not echo NaN/Infinity or untrusted payloads into JSON error responses.
        return JSONResponse(
            {"detail": [{"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in exc.errors()]}, 422
        )

    @app.exception_handler(Conflict)
    async def conflict_error(request, exc):
        return JSONResponse({"detail": str(exc)}, 409)

    @app.exception_handler(NotFound)
    async def not_found(request, exc):
        return JSONResponse({"detail": str(exc)}, 404)

    @app.exception_handler(SQLAlchemyError)
    async def db_error(request, exc):
        log.exception("database_error")
        return JSONResponse(
            {"detail": "Storage conflict" if isinstance(exc, IntegrityError) else "Storage unavailable"},
            409 if isinstance(exc, IntegrityError) else 503,
        )

    router = APIRouter(prefix="/api/v1")

    @router.get("/agent/status")
    def agent_status():
        return investigator.status()

    @router.get("/agent/runs")
    def agent_history():
        return {"items": investigator.history()}

    @router.get("/agent/runs/{ident}")
    def agent_result(ident: str):
        result = investigator.get(ident)
        if result is None:
            raise NotFound("Agent run not found")
        return result

    @router.post("/agent/runs", status_code=201)
    def agent_run(request: AgentRequest):
        try:
            return investigator.run(request)
        except AgentUnavailable as exc:
            raise HTTPException(503, str(exc)) from None
        except AgentBusy as exc:
            raise HTTPException(409, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @router.get("/health")
    def health():
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"status": "ok", "database": "ok", "version": "0.1.0"}

    @router.post("/measurements", status_code=201)
    def ingest(measurement: SensorMeasurement):
        return guard.ingest([measurement])[0]

    @router.post("/measurements/batch")
    def ingest_batch(measurements: list[SensorMeasurement]):
        if not 1 <= len(measurements) <= settings.max_batch:
            raise HTTPException(422, f"Batch must contain 1..{settings.max_batch} measurements")
        return {"items": guard.ingest(measurements)}

    def query_measurements(
        sensor_id=None, anomaly=False, decision=None, limit=100, offset=0, order="timestamp"
    ):
        stmt = select(Measurement, Analysis).join(Analysis)
        if sensor_id:
            stmt = stmt.where(Measurement.sensor_id == sensor_id)
        if anomaly:
            stmt = stmt.where(Analysis.is_anomaly.is_(True))
        if decision:
            stmt = stmt.where(Analysis.decision == decision)
        with sessions() as session:
            total = session.scalar(select(func.count()).select_from(stmt.subquery()))
            ordering = Measurement.received_at if order == "received_at" else Measurement.timestamp
            rows = session.execute(stmt.order_by(ordering.desc(), Measurement.id).limit(limit).offset(offset))
            return {"total": total, "items": [serialize(m, a) for m, a in rows]}

    @router.get("/measurements")
    def measurements(
        sensor_id: str | None = None,
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
        order: Literal["timestamp", "received_at"] = "timestamp",
    ):
        return query_measurements(sensor_id, limit=limit, offset=offset, order=order)

    @router.get("/anomalies")
    def anomalies(
        sensor_id: str | None = None,
        decision: str | None = None,
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
        order: Literal["timestamp", "received_at"] = "received_at",
    ):
        return query_measurements(sensor_id, True, decision, limit, offset, order)

    @router.get("/anomalies/{ident}")
    def detail(ident: str):
        with sessions() as session:
            m, a = session.get(Measurement, ident), session.get(Analysis, ident)
            if not m or not a or not a.is_anomaly:
                raise NotFound("Anomaly not found")
            before = session.execute(
                select(Measurement, Analysis)
                .join(Analysis)
                .where(Measurement.sensor_id == m.sensor_id, Measurement.timestamp <= m.timestamp)
                .order_by(Measurement.timestamp.desc())
                .limit(30)
            ).all()[::-1]
            after = session.execute(
                select(Measurement, Analysis)
                .join(Analysis)
                .where(Measurement.sensor_id == m.sensor_id, Measurement.timestamp > m.timestamp)
                .order_by(Measurement.timestamp)
                .limit(30)
            ).all()
            reviews = session.scalars(
                select(Review).where(Review.analysis_id == ident).order_by(Review.id)
            ).all()
            return {
                "analysis": serialize(m, a),
                "context": [serialize(x, y) for x, y in before + after],
                "reviews": [{c.name: getattr(r, c.name) for c in Review.__table__.columns} for r in reviews],
            }

    @router.post("/anomalies/{ident}/reviews")
    def review(ident: str, request: ReviewRequest):
        return guard.review(ident, request)

    @router.get("/sensors")
    def sensors():
        result = []
        demo_sensors = set(demo.status()["sensor_ids"])
        with sessions() as session:
            for sensor in session.scalars(select(Sensor).order_by(Sensor.id)):
                row = session.execute(
                    select(Measurement, Analysis)
                    .join(Analysis)
                    .where(Measurement.sensor_id == sensor.id)
                    .order_by(Measurement.timestamp.desc())
                    .limit(1)
                ).first()
                feature = settings.features.get(sensor.feature, Feature())
                latest = serialize(*row) if row else None
                # Physical liveness follows receive time, not a potentially skewed device clock.
                last_received = session.scalar(
                    select(func.max(Measurement.received_at)).where(Measurement.sensor_id == sensor.id)
                )
                age = (
                    (datetime.now(timezone.utc) - datetime.fromisoformat(last_received)).total_seconds()
                    if last_received
                    else None
                )
                cadence = feature.period_seconds / (30 if sensor.id in demo_sensors else 1)
                result.append(
                    {
                        "id": sensor.id,
                        "node_id": sensor.node_id,
                        "feature": sensor.feature,
                        "unit": sensor.unit,
                        "location_id": sensor.location_id,
                        "latest": latest,
                        "last_received_at": last_received,
                        "status": "active" if age is not None and age <= max(10, cadence * 3) else "offline",
                    }
                )
        return {"items": result}

    @router.get("/status")
    def status():
        with sessions() as session:
            counts = dict(
                session.execute(select(Analysis.decision, func.count()).group_by(Analysis.decision)).all()
            )
            total = session.scalar(select(func.count()).select_from(Measurement))
        sensor_rows = sensors()["items"]
        return {
            "health": "ok",
            "measurements": total,
            "sensors": len(sensor_rows),
            "active_sensors": sum(s["status"] == "active" for s in sensor_rows),
            "anomalies": total - counts.get("normal", 0),
            "manual_review": counts.get("manual_review", 0) + counts.get("suggested_correction", 0),
            "decisions": counts,
            "demo": demo.status(),
            "policy": settings.policy.model_dump(),
            "confidence_kind": "heuristic_score",
        }

    @router.post("/demo/start")
    def start_demo():
        return demo.start()

    @router.post("/demo/stop")
    def stop_demo():
        return demo.stop()

    @router.post("/demo/faults")
    def inject_fault(request: FaultRequest):
        try:
            return demo.inject(request)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.get("/experiments")
    def experiments(limit: int = Query(20, ge=1, le=100)):
        with sessions() as session:
            rows = session.scalars(select(Experiment).order_by(Experiment.created_at.desc()).limit(limit))
            return {
                "items": [
                    {
                        "id": e.id,
                        "created_at": e.created_at,
                        "report": {k: v for k, v in e.report.items() if k != "ground_truth"},
                    }
                    for e in rows
                ]
            }

    @router.get("/experiments/{ident}")
    def experiment(ident: str):
        with sessions() as session:
            e = session.get(Experiment, ident)
            if not e:
                raise NotFound("Experiment not found")
            return e.report

    @router.post("/experiments", status_code=201)
    def benchmark(request: ExperimentRequest):
        if not experiment_lock.acquire(blocking=False):
            raise HTTPException(409, "An experiment is already running")
        try:
            report = run_benchmark(settings, request.seed, request.size)
            with sessions.begin() as session:
                session.add(Experiment(id=report["id"], report=report))
            return report
        finally:
            experiment_lock.release()

    app.include_router(router)
    static = Path(__file__).with_name("static")
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(static / "index.html")

    return app
