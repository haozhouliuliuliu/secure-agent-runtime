"""并发预算、任务隔离、恢复与备份。"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from team_agent.backup import backup_database, restore_database, verify_database
from team_agent.budget import BudgetManager
from team_agent.errors import BudgetExceeded
from team_agent.service import SlidingWindowLimiter

from .support import answer, build_env, business_findings, plan, tool_call
from .test_api import headers, make_app, queue_simple, wait_terminal


def test_global_budget_is_concurrency_safe() -> None:
    manager = BudgetManager(global_limit=5, per_task_limit=10)
    successes = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        try:
            manager.reserve(f"task-{index}")
        except BudgetExceeded:
            return
        with lock:
            successes.append(index)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(successes) == 5
    assert manager.total_calls == 5


def test_per_task_budget_is_isolated() -> None:
    manager = BudgetManager(global_limit=100, per_task_limit=2)
    first = manager.for_task("a")
    second = manager.for_task("b")
    first.reserve()
    first.reserve()
    with pytest.raises(BudgetExceeded):
        first.reserve()
    second.reserve()
    assert first.task_calls == 2
    assert second.task_calls == 1


def test_sliding_window_rate_limiter() -> None:
    limiter = SlidingWindowLimiter(2, window_seconds=60)
    assert limiter.allow("alice")
    assert limiter.allow("alice")
    assert not limiter.allow("alice")
    assert limiter.allow("bob")


def test_ten_concurrent_tasks_do_not_cross_talk(tmp_path: Path) -> None:
    env, service, app = make_app(tmp_path, max_concurrent_tasks=10, max_requests_per_minute=100)

    def planner_handler(messages):
        text = messages[-1]["content"]
        marker = re.search(r"REQ-\d+", text).group(0)
        return plan([("business", f"查库存 {marker}")], audit=False)

    def findings_handler(messages):
        text = json.dumps(messages, ensure_ascii=False)
        marker = re.search(r"REQ-\d+", text).group(0)
        evidence_id = "EV-MISSING"
        for message in reversed(messages):
            if message.get("role") == "tool":
                payload = json.loads(message["content"])
                if payload.get("evidence_id"):
                    evidence_id = payload["evidence_id"]
                    break
        return answer(
            json.dumps(
                {
                    "summary": f"结果 {marker}",
                    "claims": [{"claim": f"库存可用 {marker}", "evidence_ids": [evidence_id]}],
                    "data_gaps": [],
                    "confidence": 0.9,
                },
                ensure_ascii=False,
            )
        )

    def business_router(messages):
        if any(message.get("role") == "tool" for message in messages):
            return findings_handler(messages)
        return tool_call("query_inventory", {})

    env.fake.set_default("planner", planner_handler)
    env.fake.set_default("business", business_router)

    with TestClient(app) as client:
        created = []
        for index in range(10):
            response = client.post("/v1/tasks", json={"request": f"REQ-{index} 库存"}, headers=headers())
            assert response.status_code == 202, response.text
            created.append(response.json()["task_id"])
        for index, task_id in enumerate(created):
            result = wait_terminal(client, task_id)
            assert result["status"] == "completed"
            assert result["owner"] == "alice"
            assert f"REQ-{index}" in result["result"]["business_summary"]
    assert service.system.budget.total_calls == 30


def test_global_budget_stops_new_work_without_fake_success(tmp_path: Path) -> None:
    env, service, app = make_app(tmp_path, max_total_calls=4, max_calls_per_task=4, max_concurrent_tasks=1)
    with TestClient(app) as client:
        queue_simple(env)
        first = client.post("/v1/tasks", json={"request": "第一个"}, headers=headers()).json()["task_id"]
        first_result = wait_terminal(client, first)
        assert first_result["status"] == "completed"
        time.sleep(0.05)

        env.fake.queue("planner", plan([("business", "查库存")], audit=False))
        env.fake.queue("business", tool_call("query_inventory", {}), business_findings())
        second = client.post("/v1/tasks", json={"request": "第二个"}, headers=headers()).json()["task_id"]
        second_result = wait_terminal(client, second)
        assert second_result["status"] == "failed"
        assert second_result["error_code"] == "BUDGETEXCEEDED"
    assert service.system.budget.total_calls == 4


def test_recovery_marks_running_tasks_interrupted(tmp_path: Path) -> None:
    _env, service, _ = make_app(tmp_path)
    task_id = service.repository.create_task("运行中任务", owner="alice")
    service.repository.update_task(task_id, status="running")
    recovered = service.recover_interrupted()
    assert recovered == 1
    task = service.repository.get_task(task_id)
    assert task["status"] == "failed"
    assert task["error"] == "INTERRUPTED"
    events = service.repository.list_events(task_id)
    assert any(event["type"] == "task_interrupted" for event in events)


def test_backup_restore_roundtrip(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    env.fake.queue("planner", plan([("business", "查库存")], audit=False))
    env.fake.queue("business", tool_call("query_inventory", {}), business_findings())
    task_id = env.system.run("库存")
    backup = backup_database(env.settings.db_path, tmp_path / "backup.db")
    assert verify_database(backup)["collab_tasks"] == 1
    env.repository.delete_task(task_id)
    assert verify_database(env.settings.db_path)["collab_tasks"] == 0
    restore_database(backup, env.settings.db_path)
    assert verify_database(env.settings.db_path)["collab_tasks"] == 1
    assert env.repository.get_task(task_id)["id"] == task_id


def test_purge_old_tasks(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    task_id = env.repository.create_task("旧任务", owner="alice")
    connection = sqlite3.connect(env.settings.db_path)
    try:
        connection.execute("UPDATE collab_tasks SET created_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (task_id,))
        connection.commit()
    finally:
        connection.close()
    deleted = env.repository.purge_tasks("2020-01-01T00:00:00+00:00")
    assert deleted == 1
    assert env.repository.list_tasks(owner="alice") == []
