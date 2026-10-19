"""协作流程测试：规划、并行、交接、审核、返工、异常与持久化。"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from team_agent.errors import ModelError
from team_agent.repository import Repository

from .support import (
    Env,
    answer,
    audit_result,
    build_env,
    business_findings,
    plan,
    policy_findings,
    run_simple,
    tool_call,
)


def _full_plan(env: Env, *, parallel: bool = True, audit: bool = True) -> None:
    env.fake.queue(
        "planner",
        plan([("business", "查询库存与工单"), ("policy", "检索制度")], parallel=parallel, audit=audit),
    )


def _full_agents(env: Env, *, policy_status: str = "现行", audit: str = "pass") -> None:
    env.fake.queue("business", tool_call("query_inventory", {}), business_findings())
    env.fake.queue("policy", tool_call("search_policies", {"query": "电脑领用"}), policy_findings(status=policy_status))
    env.fake.queue("auditor", audit_result(audit))


def test_simple_task_completes_without_audit(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    task = run_simple(env)
    assert task["status"] == "completed"
    subtasks = env.repository.list_subtasks(task["id"])
    assert [item["agent"] for item in subtasks] == ["business"]
    roles = [call["role"] for call in env.fake.calls]
    assert roles == ["planner", "business", "business"]
    final = json.loads(task["final_json"])
    assert final["audit_verdict"] is None
    assert final["business_summary"]


def test_policy_only_plan_adds_business_and_audit(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    env.fake.queue("planner", plan([("policy", "查制度")], audit=False))
    env.fake.queue("business", tool_call("query_inventory", {}), business_findings())
    env.fake.queue("policy", tool_call("search_policies", {"query": "电脑领用"}), policy_findings())
    env.fake.queue("auditor", audit_result("pass"))
    task = env.system.run("新员工电脑领用有什么制度要求？")
    state = env.repository.get_task(task)
    assert state["status"] == "completed"
    agents = [item["agent"] for item in env.repository.list_subtasks(task)]
    assert agents == ["business", "policy", "auditor"]


def test_duplicate_subtasks_are_deduplicated(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    env.fake.queue("planner", plan([("business", "查库存"), ("business", "再查库存"), ("policy", "查制度")]))
    _full_agents(env)
    task = env.system.run("综合判断")
    agents = [item["agent"] for item in env.repository.list_subtasks(task)]
    assert agents.count("business") == 1


def test_invalid_plan_json_is_repaired(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    env.fake.queue("planner", answer("这不是 JSON"), plan([("business", "查库存")], audit=False))
    env.fake.queue("business", tool_call("query_inventory", {}), business_findings())
    task = env.system.run("库存多少")
    assert env.repository.get_task(task)["status"] == "completed"
    planner_calls = [call for call in env.fake.calls if call["role"] == "planner"]
    assert len(planner_calls) == 2


def test_invalid_plan_twice_fails_task(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    env.fake.queue("planner", answer("坏输出"), answer("还是坏输出"))
    task = env.system.run("库存多少")
    state = env.repository.get_task(task)
    assert state["status"] == "failed"
    assert "规划失败" in state["error"]


def test_planner_auth_error_fails_fast(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    env.fake.queue("planner", ModelError("HTTP_401", "鉴权失败", retryable=False))
    task = env.system.run("库存多少")
    assert env.repository.get_task(task)["status"] == "failed"
    assert len(env.fake.calls) == 1


def test_business_timeout_retries_then_succeeds(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    env.fake.queue("planner", plan([("business", "查库存")], audit=False))
    env.fake.queue(
        "business",
        ModelError("TIMEOUT", "超时", retryable=True),
        tool_call("query_inventory", {}),
        business_findings(),
    )
    task = env.system.run("库存多少")
    assert env.repository.get_task(task)["status"] == "completed"
    failed_runs = [run for run in env.repository.list_runs(task) if run["status"] == "failed"]
    assert failed_runs and failed_runs[0]["error_code"] == "TIMEOUT"


def test_business_auth_error_no_retry(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    env.fake.queue("planner", plan([("business", "查库存")], audit=False))
    env.fake.queue("business", ModelError("HTTP_401", "鉴权失败", retryable=False))
    task = env.system.run("库存多少")
    assert env.repository.get_task(task)["status"] == "failed"
    business_calls = [call for call in env.fake.calls if call["role"] == "business"]
    assert len(business_calls) == 1


def test_audit_pass_and_handoffs(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    _full_plan(env)
    _full_agents(env)
    task = env.system.run("新员工电脑需求能否满足？")
    state = env.repository.get_task(task)
    assert state["status"] == "completed"
    audits = env.repository.list_audits(task)
    assert audits and audits[-1]["verdict"] == "pass"
    handoffs = env.repository.list_handoffs(task)
    assert {(item["from_agent"], item["to_agent"]) for item in handoffs} == {
        ("business", "auditor"),
        ("policy", "auditor"),
    }
    auditor_call = next(call for call in env.fake.calls if call["role"] == "auditor")
    prompt = json.dumps(auditor_call["messages"], ensure_ascii=False)
    assert "可用笔记本 4 台" in prompt
    assert "EV-" in prompt


def test_audit_conflict_makes_partial(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    _full_plan(env)
    env.fake.queue("business", tool_call("query_inventory", {}), business_findings())
    env.fake.queue("policy", tool_call("search_policies", {"query": "电脑领用"}), policy_findings("L14 只适用于职能岗"))
    env.fake.queue(
        "auditor",
        audit_result(
            "fail",
            summary="业务建议与制度限制冲突",
            conflicts=[{"description": "L14 不属于研发岗标准配置", "between": ["business", "policy"]}],
            findings=[
                {
                    "level": "error",
                    "type": "conflict",
                    "description": "库存有货但机型不符合研发岗制度",
                    "related_evidence": [],
                }
            ],
        ),
    )
    task = env.system.run("研发新员工配 L14 是否可行？")
    state = env.repository.get_task(task)
    assert state["status"] == "partial"
    final = json.loads(state["final_json"])
    assert final["audit_verdict"] == "fail"
    assert final["conflicts"]


def test_unsupported_claim_is_downgraded(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    _full_plan(env)

    def bad_business(messages):
        return answer(
            json.dumps(
                {
                    "summary": "库存充足",
                    "claims": [{"claim": "可用 4 台", "evidence_ids": ["EV-FAKE"], "value": "4"}],
                    "data_gaps": [],
                    "confidence": 0.9,
                },
                ensure_ascii=False,
            )
        )

    env.fake.queue("business", tool_call("query_inventory", {}), bad_business)
    env.fake.queue("policy", tool_call("search_policies", {"query": "电脑领用"}), policy_findings())
    env.fake.queue("auditor", audit_result("pass"))
    task = env.system.run("库存能否满足？")
    state = env.repository.get_task(task)
    assert state["status"] == "partial"
    findings = json.loads(state["final_json"])["issues"]
    assert any(item["type"] == "invalid_evidence" for item in findings)


def test_untrusted_ticket_instruction_is_flagged(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    _full_plan(env)
    env.fake.queue(
        "business",
        tool_call("get_ticket", {"ticket_id": "INC-2026-0017"}),
        business_findings(summary="工单描述包含异常指令"),
    )
    env.fake.queue("policy", tool_call("search_policies", {"query": "设备申请"}), policy_findings())
    env.fake.queue("auditor", audit_result("pass"))
    task = env.system.run("工单 INC-2026-0017 的要求可信吗？")
    state = env.repository.get_task(task)
    assert state["status"] == "partial"
    findings = json.loads(state["final_json"])["issues"]
    assert any(item["type"] == "untrusted_instruction" for item in findings)


def test_obsolete_policy_is_reported_to_auditor(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    _full_plan(env)
    _full_agents(env, policy_status="已废止")
    task = env.system.run("显示器审批需要谁？")
    assert env.repository.get_task(task)["status"] == "completed"
    auditor_call = next(call for call in env.fake.calls if call["role"] == "auditor")
    prompt = json.dumps(auditor_call["messages"], ensure_ascii=False)
    assert "非现行" in prompt


def test_revision_round_can_fix_evidence(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    _full_plan(env)
    env.fake.queue("business", tool_call("query_inventory", {}), business_findings())
    env.fake.queue(
        "policy",
        tool_call("search_policies", {"query": "电脑领用"}),
        policy_findings("旧条款"),
        tool_call("search_policies", {"query": "电脑领用 现行版本"}),
        policy_findings("现行条款"),
    )
    env.fake.queue(
        "auditor",
        audit_result("needs_evidence", summary="需要补充现行制度", followups=["补充制度版本依据"]),
        audit_result("pass"),
    )
    task = env.system.run("新员工电脑需求能否满足？")
    state = env.repository.get_task(task)
    assert state["status"] == "completed"
    assert len(env.repository.list_audits(task)) == 2
    policy_calls = [call for call in env.fake.calls if call["role"] == "policy"]
    assert len(policy_calls) == 4


def test_revision_disabled_returns_partial(tmp_path: Path) -> None:
    env = build_env(tmp_path, revision_limit=0)
    _full_plan(env)
    _full_agents(env, audit="needs_evidence")
    task = env.system.run("综合判断")
    assert env.repository.get_task(task)["status"] == "partial"
    assert len(env.repository.list_audits(task)) == 1


def test_parallel_investigators_run_concurrently(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    _full_plan(env, parallel=True)
    barrier = threading.Barrier(2, timeout=3)

    def business_first(messages):
        barrier.wait()
        return tool_call("query_inventory", {})

    def policy_first(messages):
        barrier.wait()
        return tool_call("search_policies", {"query": "电脑领用"})

    env.fake.queue("business", business_first, business_findings())
    env.fake.queue("policy", policy_first, policy_findings())
    env.fake.queue("auditor", audit_result("pass"))
    started = time.perf_counter()
    task = env.system.run("并行调查")
    elapsed = time.perf_counter() - started
    assert env.repository.get_task(task)["status"] == "completed"
    assert elapsed < 5


def test_budget_exceeded_stops_honestly(tmp_path: Path) -> None:
    env = build_env(tmp_path, max_total_calls=2, max_calls_per_task=2)
    env.fake.queue("planner", plan([("business", "查库存")], audit=False))
    env.fake.queue("business", tool_call("query_inventory", {}), business_findings())
    task = env.system.run("库存多少")
    state = env.repository.get_task(task)
    assert state["status"] == "failed"
    assert env.system.budget.total_calls == 2


def test_persistence_after_repository_reload(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    task = run_simple(env)
    fresh = Repository(env.settings.db_path)
    detail = {
        "task": fresh.get_task(task["id"]),
        "subtasks": fresh.list_subtasks(task["id"]),
        "runs": fresh.list_runs(task["id"]),
        "events": fresh.list_events(task["id"]),
    }
    assert detail["task"]["status"] == "completed"
    assert detail["subtasks"]
    assert len(detail["runs"]) >= 3
    assert any(event["type"] == "task_finished" for event in detail["events"])


def test_model_routing_per_agent(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    _full_plan(env)
    _full_agents(env)
    env.system.run("综合判断")
    models = {(call["role"], call["model"]) for call in env.fake.calls}
    assert ("planner", "deepseek-test") in models
    assert ("business", "qwen-test") in models
    assert ("policy", "qwen-policy-test") in models
    assert ("auditor", "kimi-auditor-test") in models


def test_task_ids_are_unique(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    first = run_simple(env, "库存?")
    env.fake.queue("planner", plan([("business", "查库存")], audit=False))
    env.fake.queue("business", tool_call("query_inventory", {}), business_findings())
    second = env.system.run("库存?")
    assert first["id"] != second
