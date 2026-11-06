"""Replaceable adapters yield the canonical model; no detector imports here."""

import csv
import logging
import os
import threading
import time
from pathlib import Path
from typing import Iterable, Protocol

import httpx
from paho.mqtt import client as mqtt

from .schemas import SensorMeasurement


class Source(Protocol):
    def read(self) -> Iterable[SensorMeasurement]: ...


class CSVSource:
    def __init__(self, path, mapping, defaults=None):
        self.path, self.mapping, self.defaults = path, mapping, defaults or {}

    def read(self):
        with open(self.path, encoding="utf-8-sig", newline="") as stream:
            for line, row in enumerate(csv.DictReader(stream), 2):
                payload = dict(self.defaults)
                for canonical, column in self.mapping.items():
                    if column not in row:
                        raise ValueError(f"CSV line {line}: missing column {column}")
                    payload[canonical] = row[column]
                try:
                    payload["value"] = (
                        float(payload["value"]) if payload.get("value") not in ("", None) else None
                    )
                    yield SensorMeasurement.model_validate(payload)
                except (ValueError, TypeError) as exc:
                    raise ValueError(f"CSV line {line}: {exc}") from exc


class HTTPTransport:
    def __init__(self, url, api_key=None):
        self.client = httpx.Client(
            base_url=url, timeout=10, headers={"X-API-Key": api_key or os.getenv("API_KEY", "")}
        )

    def send(self, measurements, stop_event=None):
        payload = [m.model_dump(mode="json") for m in measurements]
        for attempt in range(5):
            if stop_event and stop_event.is_set():
                raise InterruptedError("Transport stopped")
            try:
                response = self.client.post("/api/v1/measurements/batch", json=payload)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code < 500 or attempt == 4:
                    raise
            except httpx.TransportError:
                if attempt == 4:
                    raise
            if stop_event:
                if stop_event.wait(min(2**attempt, 8)):
                    raise InterruptedError("Transport stopped")
            else:
                time.sleep(min(2**attempt, 8))

    def close(self):
        self.client.close()


def mqtt_bridge(host, port, topic, url):
    """MQTT QoS1 → HTTP. Disk spool survives HTTP downtime and process restarts."""
    log = logging.getLogger("agroguard")
    spool = Path("data/mqtt-spool")
    spool.mkdir(parents=True, exist_ok=True)
    transport = HTTPTransport(url)
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=os.getenv("MQTT_CLIENT_ID", "agroguard-gateway"),
        clean_session=False,
        manual_ack=True,
    )
    if os.getenv("MQTT_USERNAME"):
        client.username_pw_set(os.environ["MQTT_USERNAME"], os.getenv("MQTT_PASSWORD"))
    if os.getenv("MQTT_TLS", "0") == "1":
        client.tls_set()
    client.reconnect_delay_set(1, 30)

    def on_connect(client, userdata, flags, reason_code, properties):
        if not reason_code.is_failure:
            client.subscribe(topic, qos=1)
        log.info("mqtt_connected %s", reason_code)

    def on_message(client, userdata, message):
        import hashlib

        # ACK only after fsync. A unique digest makes broker redeliveries harmless.
        digest = hashlib.sha256(message.payload).hexdigest()
        target = spool / f"{digest}.json"
        temporary = spool / f"{digest}.tmp"
        with temporary.open("wb") as stream:
            stream.write(message.payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(target)
        client.ack(message.mid, message.qos)

    client.on_connect, client.on_message = on_connect, on_message
    stop = threading.Event()

    def deliver():
        while not stop.wait(1):
            for path in spool.glob("*.json"):
                try:
                    raw = path.read_bytes()
                    if len(raw) > 1048576:
                        raise ValueError("MQTT payload too large")
                    m = SensorMeasurement.model_validate_json(raw)
                    transport.send([m], stop_event=stop)
                    path.unlink()
                except (ValueError, httpx.HTTPStatusError) as exc:
                    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500:
                        log.warning("mqtt_http_retry")
                        break
                    # Quarantine invalid messages; retain payload as a file for inspection.
                    path.replace(path.with_suffix(".rejected"))
                    log.error("mqtt_quarantined %s %s", path.name, type(exc).__name__)
                except httpx.TransportError:
                    log.warning("mqtt_http_unavailable")
                    break
                except InterruptedError:
                    return

    worker = threading.Thread(target=deliver, daemon=True)
    worker.start()
    client.connect_async(host, port, keepalive=60)
    try:
        client.loop_forever(retry_first_connection=True)
    except KeyboardInterrupt:
        pass
    finally:
        client.disconnect()
        stop.set()
        worker.join(timeout=15)
        transport.close()
