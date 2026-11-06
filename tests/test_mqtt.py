"""Optional loopback integration: broker → disk spool → real HTTP → DB."""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from paho.mqtt.publish import single

from agroguard.api import create_app


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def eventually(check, timeout=35):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return
        time.sleep(0.1)
    raise AssertionError("Integration condition did not become true")


def test_mqtt_spool_http_recovery_and_quarantine(tmp_path):
    pytest.importorskip("amqtt")
    broker_port, api_port = free_port(), free_port()
    broker_script = str(Path(__file__).with_name("mqtt_broker.py"))
    logs = (tmp_path / "process.log").open("w", encoding="utf-8")
    env = dict(os.environ, API_KEY="", PYTHONUNBUFFERED="1")
    broker = subprocess.Popen([sys.executable, broker_script, str(broker_port)], stdout=logs, stderr=logs)
    gateway = None
    server = None
    thread = None
    try:

        def broker_ready():
            try:
                with socket.create_connection(("127.0.0.1", broker_port), timeout=0.1):
                    return True
            except OSError:
                return False

        eventually(broker_ready)
        gateway = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "agroguard.cli",
                "mqtt",
                "--host",
                "127.0.0.1",
                "--port",
                str(broker_port),
                "--url",
                f"http://127.0.0.1:{api_port}",
            ],
            cwd=tmp_path,
            env=env,
            stdout=logs,
            stderr=logs,
        )
        eventually(lambda: "mqtt_connected" in (tmp_path / "process.log").read_text(encoding="utf-8"))
        payload = json.dumps(
            {
                "sensor_id": "mqtt.ph",
                "feature": "soil_ph",
                "unit": "pH",
                "timestamp": "2026-01-01T00:00:00Z",
                "value": 6.5,
            }
        )
        single("agroguard/v1/measurements", payload, qos=1, hostname="127.0.0.1", port=broker_port)
        spool = tmp_path / "data" / "mqtt-spool"
        eventually(lambda: len(list(spool.glob("*.json"))) == 1)
        # HTTP was down at reception: the ACKed message remains on disk.
        app = create_app(f"sqlite:///{tmp_path / 'mqtt.db'}", auto_demo=False, api_key="")
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=api_port, log_level="error"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        eventually(lambda: server.started)
        with httpx.Client(base_url=f"http://127.0.0.1:{api_port}") as client:
            eventually(lambda: client.get("/api/v1/status").json()["measurements"] == 1)
            single("agroguard/v1/measurements", payload, qos=1, hostname="127.0.0.1", port=broker_port)
            single("agroguard/v1/measurements", "broken JSON", qos=1, hostname="127.0.0.1", port=broker_port)
            eventually(lambda: len(list(spool.glob("*.rejected"))) == 1)
            eventually(lambda: not list(spool.glob("*.json")))
            assert client.get("/api/v1/status").json()["measurements"] == 1
    finally:
        if gateway:
            gateway.terminate()
            gateway.wait(timeout=10)
        broker.terminate()
        broker.wait(timeout=10)
        if server:
            server.should_exit = True
        if thread:
            thread.join(timeout=10)
        logs.close()
