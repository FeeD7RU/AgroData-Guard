"""Opt-in real Qwen integration check, against a running LOCAL application.

Run: python tests/smoke_local_agent.py
Creates a clearly named synthetic sensor and an agent conversation; does not review records.
Not collected by pytest. No mocked provider and no external network endpoint.
"""

import json
import math
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx


def main():
    with httpx.Client(base_url="http://127.0.0.1:8000", timeout=360, trust_env=False) as client:
        state = client.get("/api/v1/agent/status").raise_for_status().json()
        if not state["configured"]:
            raise RuntimeError(state["message"])
        sensor = "llm-smoke." + uuid.uuid4().hex[:10]
        start = datetime.now(timezone.utc) - timedelta(minutes=44)
        values = [20 + 0.1 * math.sin(i) for i in range(40)] + [45, 20.1]
        rows = [
            {
                "measurement_id": f"{sensor}.{i}",
                "sensor_id": sensor,
                "feature": "temperature",
                "unit": "°C",
                "source": "llm-smoke-synthetic",
                "timestamp": (start + timedelta(minutes=i)).isoformat(),
                "value": value,
            }
            for i, value in enumerate(values)
        ]
        client.post("/api/v1/measurements/batch", json=rows).raise_for_status()
        ident = f"{sensor}.40"
        before = client.get(f"/api/v1/anomalies/{ident}").raise_for_status().json()
        question = (
            f"Разбери аномалию {ident} тестового датчика {sensor}. Изучи его историю и правила, "
            "объясни вероятную причину. Подготовь предложение для ручной проверки через инструмент. "
            "Не применяй исправления."
        )
        print(f"Investigating {sensor} with real local {state['model']}...", flush=True)
        result = (
            client.post("/api/v1/agent/runs", json={"question": question, "prepare_review": True})
            .raise_for_status()
            .json()
        )
        target = Path("artifacts/local-agent")
        target.mkdir(parents=True, exist_ok=True)
        (target / "live-smoke.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        assert result["status"] == "completed", result["error"]
        names = {step["tool"] for step in result["trace"] if "error" not in step["result"]}
        assert {"inspect_anomaly", "inspect_sensor", "get_analysis_rules", "propose_review"} <= names, names
        assert result["proposals"] and result["proposals"][0]["analysis_id"] == ident
        inspected = next(step["result"] for step in result["trace"] if step["tool"] == "inspect_anomaly")
        assert inspected["event_context"]["computed_facts"]["next_raw"] == 20.1
        assert client.get(f"/api/v1/anomalies/{ident}").json() == before, (
            "Agent mutated the measurement/review"
        )
        print(
            json.dumps(
                {
                    "run_id": result["id"],
                    "status": result["status"],
                    "tools": sorted(names),
                    "model": result["model"],
                    "usage": result["usage"],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        print(result["answer"], flush=True)
        followup = (
            client.post(
                "/api/v1/agent/runs",
                json={
                    "parent_run_id": result["id"],
                    "question": f"Найди незакрытые аномалии датчика {sensor} через list_anomalies. Назови ID из списка. Новое предложение не создавай.",
                },
            )
            .raise_for_status()
            .json()
        )
        (target / "live-discovery.json").write_text(
            json.dumps(followup, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        assert followup["status"] == "completed", followup["error"]
        assert any(
            step["tool"] == "list_anomalies"
            and any(item["id"] == ident for item in step["result"].get("items", []))
            for step in followup["trace"]
        )
        assert ident in followup["answer"]
        assert not followup["proposals"]
        print("Real-model discovery and conversation follow-up passed.", flush=True)


if __name__ == "__main__":
    main()
