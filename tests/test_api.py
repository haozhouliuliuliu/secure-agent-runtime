"""API 认证、授权、任务隔离与健康检查。"""

from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from team_agent.api import create_app
from team_agent.config import ApiKeyConfig
from team_agent.service import TaskService

from .support import build_env, business_findings, plan, tool_call

KEYS = (
    ApiKeyConfig("alice-secret-key", "alice", "executor"),
    ApiKeyConfig("bob-secret-key", "bob", "executor"),
    ApiKeyConfig("admin-secret-key", "root", "admin"),
)


def headers(key: str = "alice-secret-key") -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def make_app(tmp_path: Path, **overrides):
    options = {
        "api_keys": KEYS,
        "max_concurrent_tasks": 2,
        "max_requests_per_minute": 100,
        "max_input_chars": 200,
    }
    options.update(overrides)
    env = build_env(tmp_path, **options)
    service = TaskService(env.settings, system=env.system, repository=env.repository)
    app = create_app(env.settings, service=service)
    return env, service, app


def queue_simple(env) -> None:
    env.fake.queue("planner", plan([("business", "查库存")], audit=False))
    env.fake.queue("business", tool_call("query_inventory", {}), business_findings())


def wait_terminal(client: TestClient, task_id: str, key: str = "alice-secret-key") -> dict:
    for _ in range(200):
        response = client.get(f"/v1/tasks/{task_id}", headers=headers(key))
        assert response.status_code == 200
        payload = response.json()
        if payload["status"] in {"completed", "partial", "failed"}:
            return payload
        time.sleep(0.02)
    raise AssertionError("task did not finish")


def test_missing_and_invalid_token_return_401(tmp_path: Path) -> None:
    _, _, app = make_app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/v1/tasks").status_code == 401
        assert client.get("/v1/tasks", headers={"Authorization": "Bearer wrong-key"}).status_code == 401


def test_executor_creates_and_reads_own_task(tmp_path: Path) -> None:
    env, _, app = make_app(tmp_path)
    with TestClient(app) as client:
        queue_simple(env)
        created = client.post("/v1/tasks", json={"request": "库存多少"}, headers=headers())
        assert created.status_code == 202
        task_id = created.json()["task_id"]
        result = wait_terminal(client, task_id)
        assert result["owner"] == "alice"
        assert result["status"] == "completed"


def test_cross_user_access_returns_404(tmp_path: Path) -> None:
    env, _, app = make_app(tmp_path)
    with TestClient(app) as client:
        queue_simple(env)
        task_id = client.post("/v1/tasks", json={"request": "库存多少"}, headers=headers()).json()["task_id"]
        wait_terminal(client, task_id)
        assert client.get(f"/v1/tasks/{task_id}", headers=headers("bob-secret-key")).status_code == 404
        assert client.get("/v1/tasks/not-exist", headers=headers()).status_code == 404


def test_admin_can_read_any_task_and_executor_gets_403_on_admin_api(tmp_path: Path) -> None:
    env, _, app = make_app(tmp_path)
    with TestClient(app) as client:
        queue_simple(env)
        task_id = client.post("/v1/tasks", json={"request": "库存多少"}, headers=headers()).json()["task_id"]
        wait_terminal(client, task_id)
        assert client.get(f"/v1/tasks/{task_id}", headers=headers("admin-secret-key")).status_code == 200
        assert client.get("/v1/admin/tasks", headers=headers()).status_code == 403
        assert client.get("/v1/admin/tasks", headers=headers("admin-secret-key")).status_code == 200


def test_task_list_is_owner_scoped(tmp_path: Path) -> None:
    env, _, app = make_app(tmp_path)
    with TestClient(app) as client:
        queue_simple(env)
        alice_id = client.post("/v1/tasks", json={"request": "库存多少"}, headers=headers()).json()["task_id"]
        wait_terminal(client, alice_id)
        queue_simple(env)
        bob_id = client.post("/v1/tasks", json={"request": "库存多少"}, headers=headers("bob-secret-key")).json()[
            "task_id"
        ]
        wait_terminal(client, bob_id, "bob-secret-key")
        alice_list = client.get("/v1/tasks", headers=headers()).json()
        bob_list = client.get("/v1/tasks", headers=headers("bob-secret-key")).json()
        admin_list = client.get("/v1/admin/tasks", headers=headers("admin-secret-key")).json()
        assert alice_list["count"] == 1 and alice_list["items"][0]["task_id"] == alice_id
        assert bob_list["count"] == 1 and bob_list["items"][0]["task_id"] == bob_id
        assert admin_list["count"] == 2


def test_health_endpoints_and_readiness(tmp_path: Path) -> None:
    _env, _service, app = make_app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/health/live").status_code == 200
        ready = client.get("/health/ready")
        assert ready.status_code == 200
        assert ready.json()["status"] == "ready"

    no_keys = build_env(tmp_path / "nokeys", api_keys=())
    no_keys_service = TaskService(no_keys.settings, system=no_keys.system, repository=no_keys.repository)
    no_keys_app = create_app(no_keys.settings, service=no_keys_service)
    with TestClient(no_keys_app) as client:
        assert client.get("/health/ready").status_code == 503


def test_oversized_input_is_rejected(tmp_path: Path) -> None:
    _, _, app = make_app(tmp_path)
    with TestClient(app) as client:
        response = client.post("/v1/tasks", json={"request": "x" * 500}, headers=headers())
        assert response.status_code == 413
        assert response.json()["error"] == "INPUT_TOO_LARGE"


def test_rate_limit_returns_429(tmp_path: Path) -> None:
    env, _, app = make_app(tmp_path, max_requests_per_minute=1)
    with TestClient(app) as client:
        queue_simple(env)
        assert client.post("/v1/tasks", json={"request": "库存"}, headers=headers()).status_code == 202
        assert client.post("/v1/tasks", json={"request": "库存"}, headers=headers()).status_code == 429


def test_overload_returns_503(tmp_path: Path) -> None:
    import threading

    env, _, app = make_app(tmp_path, max_concurrent_tasks=1)
    started = threading.Event()
    release = threading.Event()

    def blocking_planner(messages):
        started.set()
        release.wait(timeout=5)
        return plan([("business", "查库存")], audit=False)

    env.fake.queue("planner", blocking_planner)
    env.fake.queue("business", tool_call("query_inventory", {}), business_findings())
    with TestClient(app) as client:
        first = client.post("/v1/tasks", json={"request": "慢任务"}, headers=headers())
        assert first.status_code == 202
        assert started.wait(timeout=5)
        second = client.post("/v1/tasks", json={"request": "第二个"}, headers=headers())
        assert second.status_code == 503
        release.set()
        wait_terminal(client, first.json()["task_id"])


def test_model_error_is_sanitized(tmp_path: Path) -> None:
    from team_agent.errors import ModelError

    env, _, app = make_app(tmp_path)
    with TestClient(app) as client:
        env.fake.queue("planner", plan([("business", "查库存")], audit=False))
        env.fake.queue(
            "business",
            ModelError("HTTP_401", "provider secret sk-abcdef123456789", retryable=False),
        )
        created = client.post("/v1/tasks", json={"request": "库存"}, headers=headers())
        task_id = created.json()["task_id"]
        result = wait_terminal(client, task_id)
        assert result["status"] == "failed"
        assert result["error_code"] == "HTTP_401"
        body = str(result)
        assert "sk-abcdef" not in body
        assert "provider secret" not in body


def test_admin_can_delete_task_and_executor_cannot(tmp_path: Path) -> None:
    env, _, app = make_app(tmp_path)
    with TestClient(app) as client:
        queue_simple(env)
        task_id = client.post("/v1/tasks", json={"request": "库存"}, headers=headers()).json()["task_id"]
        wait_terminal(client, task_id)
        assert client.delete(f"/v1/tasks/{task_id}", headers=headers()).status_code == 403
        assert client.delete(f"/v1/tasks/{task_id}", headers=headers("admin-secret-key")).status_code == 204
        assert client.get(f"/v1/tasks/{task_id}", headers=headers("admin-secret-key")).status_code == 404
        assert client.delete("/v1/tasks/T7-missing", headers=headers("admin-secret-key")).status_code == 404


def test_authorization_header_is_not_logged(tmp_path: Path, capsys) -> None:
    env, _, app = make_app(tmp_path)
    with TestClient(app) as client:
        queue_simple(env)
        task_id = client.post("/v1/tasks", json={"request": "库存"}, headers=headers()).json()["task_id"]
        wait_terminal(client, task_id)
    output = capsys.readouterr().out
    assert "alice-secret-key" not in output
    assert "Authorization" not in output


def test_invalid_model_json_fails_without_fake_success(tmp_path: Path) -> None:
    from .support import answer

    env, _, app = make_app(tmp_path)
    with TestClient(app) as client:
        env.fake.queue("planner", plan([("business", "查库存")], audit=False))
        env.fake.queue("business", answer("这不是 JSON"), answer("仍然不是 JSON"))
        task_id = client.post("/v1/tasks", json={"request": "库存"}, headers=headers()).json()["task_id"]
        result = wait_terminal(client, task_id)
        assert result["status"] == "failed"
        assert result["error_code"] == "INVALIDSTRUCTUREDOUTPUT"
        assert result["result"]["business_summary"] is None


def test_per_task_model_budget_is_enforced(tmp_path: Path) -> None:
    env, service, app = make_app(tmp_path, max_calls_per_task=2, max_total_calls=50)
    with TestClient(app) as client:
        env.fake.queue("planner", plan([("business", "查库存")], audit=False))
        env.fake.queue("business", tool_call("query_inventory", {}), business_findings())
        task_id = client.post("/v1/tasks", json={"request": "库存"}, headers=headers()).json()["task_id"]
        result = wait_terminal(client, task_id)
        assert result["status"] == "failed"
        assert result["error_code"] == "BUDGETEXCEEDED"
    assert service.system.budget.total_calls == 2
