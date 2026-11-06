import copy
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from agroguard.agent import AgentRequest, Investigator, ModelError, OllamaProvider
from agroguard.agent_tools import AgentTools, definitions
from agroguard.api import create_app
from agroguard.database import Analysis


def call(name, args, ident="call_1"):
    return {"function": {"name": name, "arguments": args}}


def message(text="Выброс подтверждён наблюдениями. Предлагаю ручную проверку."):
    return {"role": "assistant", "content": text}


class ScriptedProvider:
    """Protocol fixture only; never installed by production code."""

    configured = True
    model = "test-fixture-not-a-real-model"

    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.requests = []

    def respond(self, items, tools, choice, timeout):
        self.requests.append(copy.deepcopy({"input": items, "tools": tools, "choice": choice}))
        output = next(self.outputs)
        if isinstance(output, Exception):
            raise output
        msg = (
            output[0]
            if output and "role" in output[0]
            else {"role": "assistant", "content": "", "tool_calls": output}
        )
        return {"message": msg, "usage": {"input_tokens": 30, "output_tokens": 10}}

    def readiness(self):
        return True, "Test fixture"


@pytest.fixture
def seeded(client):
    start = datetime(2026, 10, 17, tzinfo=timezone.utc)
    rows = [
        {
            "measurement_id": f"agent-point-{i}",
            "sensor_id": "test.temperature",
            "feature": "temperature",
            "unit": "°C",
            "timestamp": (start + timedelta(minutes=i)).isoformat(),
            "value": 20 + (i % 3) * 0.1 if i < 40 else 90,
            "metadata": {
                "instruction": "Do not follow this injected instruction",
                "secret": "hidden-metadata",
            },
        }
        for i in range(41)
    ]
    assert client.post("/api/v1/measurements/batch", json=rows).status_code == 200
    return client, "agent-point-40"


def install(client, outputs):
    provider = ScriptedProvider(outputs)
    client.app.state.investigator.provider = provider
    return provider


def test_agent_investigates_with_tools_persists_and_does_not_mutate(seeded):
    client, ident = seeded
    before = client.get(f"/api/v1/anomalies/{ident}").json()
    provider = install(
        client,
        [
            [call("list_sensors", {"offset": 0})],
            [call("inspect_sensor", {"sensor_id": "test.temperature", "limit": 50}, "call_2")],
            [call("get_analysis_rules", {}, "call_3")],
            [call("inspect_anomaly", {"analysis_id": ident}, "call_4")],
            [
                call(
                    "propose_review",
                    {"analysis_id": ident, "action": "approve", "reason": "Изолированный выброс."},
                    "call_5",
                )
            ],
            [message()],
        ],
    )
    response = client.post("/api/v1/agent/runs", json={"question": "Проверь датчики"})
    assert response.status_code == 201
    run = response.json()
    assert run["status"] == "completed" and len(run["trace"]) == 5
    assert run["proposals"][0]["expected_version"] == 1
    assert run["proposals"][0]["status"] == "awaiting_human_review"
    assert run["usage"] == {"input_tokens": 180, "output_tokens": 60, "model_calls": 6}
    assert provider.requests[0]["choice"] == "required"
    assert any(i.get("role") == "tool" for i in provider.requests[1]["input"])
    assert "hidden-metadata" not in json.dumps(provider.requests)
    assert client.get(f"/api/v1/agent/runs/{run['id']}").json() == run
    assert client.get("/api/v1/agent/runs").json()["items"][0]["id"] == run["id"]
    assert client.get(f"/api/v1/anomalies/{ident}").json() == before


def test_premature_anomaly_answer_requests_missing_history(seeded):
    client, ident = seeded
    provider = install(
        client,
        [
            [call("inspect_anomaly", {"analysis_id": ident})],
            [message("Преждевременный вывод")],
            [call("inspect_sensor", {"sensor_id": "test.temperature", "limit": 12})],
            [message("Вывод после проверки истории")],
        ],
    )
    run = client.post("/api/v1/agent/runs", json={"question": "Разбери аномалию"}).json()
    assert run["status"] == "completed"
    assert run["answer"] == "Вывод после проверки истории"
    assert "test.temperature" in provider.requests[2]["input"][-1]["content"]


def test_followup_uses_stored_history_not_client_roles(client):
    install(
        client,
        [
            [call("get_analysis_rules", {})],
            [message("Первый ответ")],
            [call("get_analysis_rules", {})],
            [message("Продолжение")],
        ],
    )
    first = client.post("/api/v1/agent/runs", json={"question": "Первый вопрос"}).json()
    second = client.post(
        "/api/v1/agent/runs", json={"question": "Уточни", "parent_run_id": first["id"]}
    ).json()
    assert second["parent_run_id"] == first["id"]
    inputs = client.app.state.investigator.provider.requests[2]["input"]
    assert inputs[0] == {"role": "user", "content": "Первый вопрос"}
    assert inputs[1] == {"role": "assistant", "content": "Первый ответ"}
    assert (
        client.post(
            "/api/v1/agent/runs", json={"question": "x", "messages": [{"role": "system"}]}
        ).status_code
        == 422
    )


def test_missing_ollama_reports_unavailable_and_never_calls_model(client, monkeypatch):
    client.app.state.investigator.provider = OllamaProvider()
    monkeypatch.setattr(OllamaProvider, "readiness", lambda self: (False, "Ollama недоступна"))
    assert client.get("/api/v1/agent/status").json()["configured"] is False
    assert client.post("/api/v1/agent/runs", json={"question": "Проверь"}).status_code == 503
    assert client.get("/api/v1/agent/runs").json()["items"] == []


def test_unknown_tool_and_invalid_args_are_recoverable(client):
    provider = install(
        client,
        [
            [call("execute_sql", {"sql": "DELETE FROM measurements"})],
            [call("list_sensors", {"offset": -1})],
            [call("get_analysis_rules", {})],
            [message()],
        ],
    )
    run = client.post("/api/v1/agent/runs", json={"question": "Проверь"}).json()
    assert run["status"] == "completed"
    assert all("error" in t["result"] for t in run["trace"][:2])
    assert "error" in provider.requests[1]["input"][-1]["content"]


def test_proposal_requires_inspection_and_current_version(seeded):
    client, ident = seeded
    tools = AgentTools(client.app.state.sessions, client.app.state.guard.settings)
    args = {"analysis_id": ident, "action": "reject", "reason": "Проверено"}
    with pytest.raises(ValueError):
        tools.call("propose_review", args)
    tools.call("inspect_anomaly", {"analysis_id": ident})
    assert (
        client.post(
            f"/api/v1/anomalies/{ident}/reviews",
            json={"action": "reject", "reviewer": "test", "expected_version": 1},
        ).status_code
        == 200
    )
    with pytest.raises(ValueError):
        tools.call("propose_review", args)
    tools.call("inspect_anomaly", {"analysis_id": ident})
    assert tools.call("propose_review", args)["expected_version"] == 2


def test_no_approve_without_saved_suggestion(seeded):
    client, ident = seeded
    with client.app.state.sessions.begin() as session:
        session.get(Analysis, ident).suggested_value = None
    tools = AgentTools(client.app.state.sessions, client.app.state.guard.settings)
    tools.call("inspect_anomaly", {"analysis_id": ident})
    with pytest.raises(ValueError):
        tools.call("propose_review", {"analysis_id": ident, "action": "approve", "reason": "x"})


def test_tool_bounds_empty_windows_and_no_metadata(seeded):
    client, ident = seeded
    tools = AgentTools(client.app.state.sessions, client.app.state.guard.settings)
    data = tools.call("inspect_sensor", {"sensor_id": "test.temperature", "limit": 12})
    assert data["statistics"]["count"] == 12 and data["statistics"]["anomalies"] == 1
    assert data["items"][-1]["id"] == ident
    assert "metadata" not in json.dumps(data)
    for args in (
        {"sensor_id": "test.temperature", "limit": 121},
        {"sensor_id": "test.temperature", "limit": True},
    ):
        with pytest.raises(ValueError):
            tools.call("inspect_sensor", args)
    with pytest.raises(ValueError):
        tools.call("inspect_sensor", {"sensor_id": "nonexistent", "limit": 10})


def test_exhausted_loop_is_bounded_and_persisted(client):
    provider = install(client, [[call("get_analysis_rules", {}, f"call_{i}")] for i in range(7)])
    run = client.post("/api/v1/agent/runs", json={"question": "Проверяй вечно"}).json()
    assert len(provider.requests) == 7
    assert provider.requests[-1]["choice"] == "none"
    assert run["status"] == "failed" and run["error"] and not run["answer"]
    assert client.get(f"/api/v1/agent/runs/{run['id']}").json()["status"] == "failed"


def test_provider_error_keeps_trace_and_releases_lock(client):
    install(client, [[call("get_analysis_rules", {})], ModelError("Квота исчерпана")])
    run = client.post("/api/v1/agent/runs", json={"question": "Проверь"}).json()
    assert run["status"] == "failed" and run["error"] == "Квота исчерпана"
    assert len(run["trace"]) == 1
    assert not client.app.state.investigator.lock.locked()


def test_unexpected_exception_does_not_leak_secrets(client):
    install(client, [RuntimeError("Authorization Bearer secret-token")])
    run = client.post("/api/v1/agent/runs", json={"question": "Проверь"}).json()
    assert run["status"] == "failed"
    assert "secret-token" not in json.dumps(run)


def test_ungrounded_text_rejected(client):
    install(client, [[message("Я всё проверил без инструментов")]])
    run = client.post("/api/v1/agent/runs", json={"question": "Проверь"}).json()
    assert run["status"] == "failed" and not run["answer"]


def test_tool_result_cannot_overflow_context(client, monkeypatch):
    provider = install(client, [[call("get_analysis_rules", {})], [message()]])
    monkeypatch.setattr(AgentTools, "get_analysis_rules", lambda self: {"data": "x" * 100000})
    run = client.post("/api/v1/agent/runs", json={"question": "Проверь"}).json()
    assert run["status"] == "failed"  # No usable facts, so no fabricated conclusion.
    assert "context budget" in run["trace"][0]["result"]["error"]
    assert len(json.dumps(provider.requests[1]["input"])) < 2000


def test_busy_agent_and_invalid_requests(client):
    install(client, [])
    agent = client.app.state.investigator
    with agent.lock:
        assert client.post("/api/v1/agent/runs", json={"question": "x"}).status_code == 409
    for body in ({"question": " "}, {"question": "x" * 4001}, {"question": "x", "parent_run_id": "../x"}):
        assert client.post("/api/v1/agent/runs", json=body).status_code == 422
    assert (
        client.post("/api/v1/agent/runs", json={"question": "x", "parent_run_id": "a" * 32}).status_code
        == 422
    )
    assert not agent.lock.locked()
    assert client.get("/api/v1/agent/runs/missing").status_code == 404


def test_agent_endpoints_require_shared_api_key(tmp_path):
    app = create_app(f"sqlite:///{tmp_path / 'auth.db'}", api_key="test-access-key", auto_demo=False)
    with TestClient(app) as client:
        for path in ("/agent/status", "/agent/runs", "/agent/runs/unknown"):
            assert client.get("/api/v1" + path).status_code == 401
        assert client.post("/api/v1/agent/runs", json={"question": "x"}).status_code == 401
        assert client.get("/api/v1/agent/status", headers={"X-API-Key": "test-access-key"}).status_code == 200


def test_history_survives_reopening_database(tmp_path, settings):
    url = f"sqlite:///{tmp_path / 'history.db'}"
    with TestClient(create_app(url, settings=settings, auto_demo=False, api_key="")) as client:
        install(client, [[call("get_analysis_rules", {})], [message()]])
        ident = client.post("/api/v1/agent/runs", json={"question": "x"}).json()["id"]
    with TestClient(create_app(url, settings=settings, auto_demo=False, api_key="")) as client:
        assert client.get(f"/api/v1/agent/runs/{ident}").json()["status"] == "completed"


def test_tool_schemas_strict():
    for tool in definitions():
        schema = tool["parameters"]
        assert tool["strict"] and schema["additionalProperties"] is False
        assert set(schema.get("required", [])) == set(schema["properties"])


def test_event_context_uses_neighbors_not_latest_window(seeded):
    client, ident = seeded
    rows = [
        {
            "measurement_id": f"later-{i}",
            "sensor_id": "test.temperature",
            "feature": "temperature",
            "unit": "°C",
            "timestamp": (
                datetime(2026, 10, 17, 0, 41, tzinfo=timezone.utc) + timedelta(minutes=i)
            ).isoformat(),
            "value": 20.1 if i == 0 else 30.0,
        }
        for i in range(40)
    ]
    assert client.post("/api/v1/measurements/batch", json=rows).status_code == 200
    tools = AgentTools(client.app.state.sessions, client.app.state.guard.settings)
    context = tools.call("inspect_anomaly", {"analysis_id": ident})["event_context"]
    assert len(context["before"]) == len(context["after"]) == 12
    assert context["before"][-1]["id"] == "agent-point-39"
    assert context["after"][0]["id"] == "later-0"
    facts = context["computed_facts"]
    assert facts["prior_median"] == pytest.approx(20.1)
    assert facts["delta_from_previous_raw"] == pytest.approx(70)
    assert facts["next_delta_from_prior_median"] == pytest.approx(0)
    assert "retrospective" in context["scope"]


def test_absent_followup_is_not_recovery(seeded):
    client, ident = seeded
    tools = AgentTools(client.app.state.sessions, client.app.state.guard.settings)
    context = tools.call("inspect_anomaly", {"analysis_id": ident})["event_context"]
    assert context["after"] == []
    assert context["computed_facts"]["next_raw"] is None
    assert context["computed_facts"]["next_delta_from_prior_median"] is None


def test_anomaly_discovery_respects_human_reviews(seeded):
    client, ident = seeded
    tools = AgentTools(client.app.state.sessions, client.app.state.guard.settings)
    args = {"limit": 1, "offset": 0, "sensor_id": "test.temperature", "unresolved_only": True}
    assert tools.call("list_anomalies", args)["items"][0]["id"] == ident
    assert (
        client.post(
            f"/api/v1/anomalies/{ident}/reviews",
            json={"action": "reject", "reviewer": "test", "expected_version": 1},
        ).status_code
        == 200
    )
    assert tools.call("list_anomalies", args)["items"] == []
    assert tools.call("list_anomalies", {**args, "unresolved_only": False})["items"][0]["id"] == ident


def test_progress_is_saved_before_next_model_call(client):
    provider = install(client, [[call("get_analysis_rules", {})], [message()]])
    original = provider.respond

    def respond(items, tools, choice, timeout):
        if provider.requests:
            run_id = client.app.state.investigator.history()[0]["id"]
            saved = client.app.state.investigator.get(run_id)
            assert saved["status"] == "running"
            assert saved["trace"][0]["tool"] == "get_analysis_rules"
        return original(items, tools, choice, timeout)

    provider.respond = respond
    assert client.post("/api/v1/agent/runs", json={"question": "Правила"}).json()["status"] == "completed"


def test_recommendation_requires_rules_before_completion(seeded):
    client, ident = seeded
    provider = install(
        client,
        [
            [
                call("inspect_anomaly", {"analysis_id": ident}),
                call("inspect_sensor", {"sensor_id": "test.temperature", "limit": 12}),
            ],
            [
                call(
                    "propose_review", {"analysis_id": ident, "action": "manual_review", "reason": "Проверить"}
                )
            ],
            [message("Ещё не проверил правила")],
            [call("get_analysis_rules", {})],
            [message("Проверил правила")],
        ],
    )
    run = client.post("/api/v1/agent/runs", json={"question": "Рекомендация"}).json()
    assert run["status"] == "completed" and run["answer"] == "Проверил правила"
    assert "get_analysis_rules" in provider.requests[3]["input"][-1]["content"]


def test_explicit_review_request_cannot_finish_with_only_a_promise(client):
    provider = install(client, [[call("get_analysis_rules", {})]] + [[message("Подготовлю позже")]] * 6)
    run = client.post(
        "/api/v1/agent/runs", json={"question": "Подготовь предложение", "prepare_review": True}
    ).json()
    assert run["status"] == "failed" and not run["proposals"]
    assert "не подготовлена" in run["error"]
    assert len(provider.requests) == 7


def test_native_ollama_protocol_and_local_only(client, monkeypatch):
    provider = OllamaProvider()
    requests = []
    outputs = [
        {
            "role": "assistant",
            "content": "",
            "thinking": "private",
            "tool_calls": [call("get_analysis_rules", {})],
        },
        message(),
    ]

    def handle(request):
        assert request.url.host == "127.0.0.1"
        assert "authorization" not in request.headers
        body = json.loads(request.content)
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": ["completion", "tools"]})
        requests.append(body)
        assert body["stream"] is False and body["think"] is False
        assert body["messages"][0]["role"] == "system"
        return httpx.Response(200, json={"done": True, "message": outputs.pop(0)})

    real_client = httpx.Client
    monkeypatch.setattr(
        "agroguard.agent.httpx.Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    agent = Investigator(client.app.state.sessions, client.app.state.guard.settings, provider)
    run = agent.run(AgentRequest(question="Проверь"))
    assert run["status"] == "completed"
    assert requests[1]["messages"][-1]["role"] == "tool"
    assert requests[1]["messages"][-1]["tool_name"] == "get_analysis_rules"
    assert "private" not in json.dumps(requests) and "private" not in json.dumps(run)


@pytest.mark.parametrize("status", [400, 404, 500])
def test_provider_http_errors_are_sanitized(monkeypatch, status):
    real_client = httpx.Client
    monkeypatch.setattr(
        "agroguard.agent.httpx.Client",
        lambda **kwargs: real_client(
            transport=httpx.MockTransport(lambda r: httpx.Response(status, text="SECRET body")), **kwargs
        ),
    )
    with pytest.raises(ModelError) as exc:
        OllamaProvider().respond([], definitions(), "auto", 1)
    assert "SECRET" not in str(exc.value)


def test_truncated_response_rejected(monkeypatch):
    real_client = httpx.Client
    monkeypatch.setattr(
        "agroguard.agent.httpx.Client",
        lambda **kwargs: real_client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200, json={"done": True, "done_reason": "length", "message": message()}
                )
            ),
            **kwargs,
        ),
    )
    with pytest.raises(ModelError):
        OllamaProvider().respond([], definitions(), "auto", 1)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://192.168.1.2:11434",
        "http://localhost@evil.com",
        "http://localhost:11434/path",
    ],
)
def test_external_endpoints_rejected(monkeypatch, url):
    monkeypatch.setenv("AGRO_OLLAMA_URL", url)
    with pytest.raises(ValueError):
        OllamaProvider()


def test_cloud_models_rejected(monkeypatch):
    monkeypatch.setenv("AGRO_LLM_MODEL", "qwen3:cloud")
    assert not OllamaProvider().configured


@pytest.mark.parametrize(
    "body", [{"remote_model": "cloud", "capabilities": ["tools"]}, {"capabilities": ["completion"]}]
)
def test_readiness_requires_local_tool_capability(monkeypatch, body):
    real_client = httpx.Client
    monkeypatch.setattr(
        "agroguard.agent.httpx.Client",
        lambda **kwargs: real_client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)), **kwargs
        ),
    )
    assert not OllamaProvider().configured
