"""SQLAlchemy storage. All updates to raw measurements are prohibited by the ORM."""

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import JSON, Float, ForeignKey, Integer, String, UniqueConstraint, create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool


def now():
    return datetime.now(timezone.utc).isoformat()


class Base(DeclarativeBase):
    pass


class SchemaRevision(Base):
    __tablename__ = "schema_revision"
    id: Mapped[int] = mapped_column(primary_key=True)
    revision: Mapped[int]


class Node(Base):
    __tablename__ = "sensor_nodes"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)


class Sensor(Base):
    __tablename__ = "sensors"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    node_id: Mapped[str | None] = mapped_column(ForeignKey("sensor_nodes.id"))
    feature: Mapped[str] = mapped_column(String(80))
    unit: Mapped[str | None] = mapped_column(String(32))
    location_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[str] = mapped_column(default=now)


class Measurement(Base):
    __tablename__ = "measurements"
    __table_args__ = (UniqueConstraint("sensor_id", "timestamp"),)
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    sensor_id: Mapped[str] = mapped_column(ForeignKey("sensors.id"), index=True)
    timestamp: Mapped[str] = mapped_column(String(40), index=True)
    raw_value: Mapped[float | None] = mapped_column(Float)
    payload: Mapped[dict] = mapped_column(JSON)
    payload_hash: Mapped[str] = mapped_column(String(64))
    received_at: Mapped[str] = mapped_column(default=now)


class Analysis(Base):
    __tablename__ = "analysis_results"
    id: Mapped[str] = mapped_column(ForeignKey("measurements.id"), primary_key=True)
    is_anomaly: Mapped[bool]
    anomaly_type: Mapped[str] = mapped_column(String(40))
    anomaly_confidence: Mapped[float]
    correction_confidence: Mapped[float]
    confidence_kind: Mapped[str] = mapped_column(default="heuristic_score")
    detector: Mapped[str]
    corrector: Mapped[str | None]
    suggested_value: Mapped[float | None]
    accepted_value: Mapped[float | None]
    decision: Mapped[str] = mapped_column(String(40), index=True)
    evidence: Mapped[list] = mapped_column(JSON)
    policy: Mapped[dict] = mapped_column(JSON)
    is_late: Mapped[bool]
    algorithm_version: Mapped[str] = mapped_column(default="baseline-v1")
    version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[str] = mapped_column(default=now)


class Review(Base):
    __tablename__ = "human_reviews"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    analysis_id: Mapped[str] = mapped_column(ForeignKey("analysis_results.id"), index=True)
    reviewer: Mapped[str]
    action: Mapped[str]
    note: Mapped[str]
    accepted_value: Mapped[float | None]
    previous_decision: Mapped[str]
    version: Mapped[int]
    created_at: Mapped[str] = mapped_column(default=now)


class SyntheticTruth(Base):
    __tablename__ = "synthetic_truth"
    id: Mapped[str] = mapped_column(ForeignKey("measurements.id"), primary_key=True)
    details: Mapped[dict] = mapped_column(JSON)


class Experiment(Base):
    __tablename__ = "experiments"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[str] = mapped_column(default=now)
    report: Mapped[dict] = mapped_column(JSON)


class AgentRun(Base):
    __tablename__ = "agent_runs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    created_at: Mapped[str] = mapped_column(default=now)
    parent_run_id: Mapped[str | None] = mapped_column(ForeignKey("agent_runs.id"))
    question: Mapped[str]
    model: Mapped[str]
    status: Mapped[str]
    answer: Mapped[str]
    trace: Mapped[list] = mapped_column(JSON)
    proposals: Mapped[list] = mapped_column(JSON)
    usage: Mapped[dict] = mapped_column(JSON)
    error: Mapped[str | None]


def immutable(mapper, connection, target):
    raise ValueError("Raw measurements and review history are immutable")


for model in (Measurement, Review, SyntheticTruth):
    event.listen(model, "before_update", immutable)
    event.listen(model, "before_delete", immutable)


def make_database(url: str):
    if url.startswith("sqlite:///") and ":memory:" not in url:
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    kwargs = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        if ":memory:" in url:
            kwargs["poolclass"] = StaticPool
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def pragmas(connection, record):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")

    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        revision = session.get(SchemaRevision, 1)
        if revision is None:
            session.add(SchemaRevision(id=1, revision=1))
        elif revision.revision != 1:
            raise RuntimeError("Unsupported database revision; back up and migrate before starting")
    if url.startswith("sqlite"):
        # Protect raw data even against accidental direct SQL changes.
        with engine.begin() as connection:
            for table in ("measurements", "human_reviews", "synthetic_truth"):
                for action in ("UPDATE", "DELETE"):
                    connection.execute(
                        text(
                            f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action} "
                            f"BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT, 'immutable record'); END"
                        )
                    )
    return engine, sessions
