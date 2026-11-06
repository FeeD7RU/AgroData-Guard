"""Встроенное демо передаёт измерения в тот же сервис приёма, что и HTTP API."""

import logging
import threading
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .database import SyntheticTruth
from .simulator import Simulator

log = logging.getLogger("agroguard")


class DemoController:
    def __init__(self, guard):
        self.guard = guard
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        self.simulator = None
        self.error = None

    def persist(self, rows):
        # Эталон сохраняется отдельно и доступен только для последующей оценки качества.
        for i in range(0, len(rows), self.guard.settings.max_batch):
            chunk = rows[i : i + self.guard.settings.max_batch]
            self.guard.ingest([m for m, truth in chunk])
            with self.guard.sessions.begin() as session:
                for m, truth in chunk:
                    if session.get(SyntheticTruth, m.measurement_id) is None:
                        session.add(SyntheticTruth(id=m.measurement_id, details=truth))

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return self.status()
            self.error = None
            self.stop_event.clear()
            self.simulator = Simulator(
                self.guard.settings,
                start=datetime.now(timezone.utc) - timedelta(hours=1),
                namespace=f"demo{uuid4().hex[:6]}",
            )
            self.persist(self.simulator.step(3600))
            self.thread = threading.Thread(target=self.run, daemon=True, name="synthetic-source")
            self.thread.start()
            return self.status()

    def run(self):
        try:
            # Одна реальная секунда соответствует 30 секундам модельного времени.
            while not self.stop_event.wait(1):
                with self.lock:
                    self.persist(self.simulator.step(30))
        except Exception as exc:
            log.exception("demo_failed")
            self.error = type(exc).__name__

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=10)
        return self.status()

    def status(self):
        with self.lock:
            return {
                "running": bool(self.thread and self.thread.is_alive() and not self.stop_event.is_set()),
                "clock": self.simulator.clock.isoformat() if self.simulator else None,
                "sensor_ids": list(self.simulator.channels) if self.simulator else [],
                "faults": {sid: f.kind for sid, f in self.simulator.faults.items()} if self.simulator else {},
                "error": self.error,
                "speed": 30,
            }

    def inject(self, request):
        with self.lock:
            if not self.simulator or self.stop_event.is_set():
                raise ValueError("Start the simulator first")
            self.simulator.inject(request.sensor_id, request.kind, request.duration, request.magnitude)
            return self.status()
