"""安全测试：授权、认证、脱敏、模型故障与边界。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from team_agent.auth import AuthError, KeyStore
from team_agent.config import ApiKeyConfig
from team_agent.errors import ModelError
from team_agent.llm import LLMResponse, ToolCall
from team_agent.secure_logging import JsonFormatter, redact

from .support import (
    build_env,
    business_findings,
    plan,
    tool_call,
)


def multi_call(*items: tuple[str, dict]) -> LLMResponse:
    return LLMResponse(
        content=None,
        tool_calls=tuple(
            ToolCall(id=f"c{index}", name=name, raw_arguments=json.dumps(args), arguments=args)
            for index, (name, args) in enumerate(items)
        ),
    )


def test_keystore_authentication_and_roles() -> None:
    store = KeyStore(
        (
            ApiKeyConfig("alice-secret-key", "alice", "executor"),
            ApiKeyConfig("admin-secret-key", "root", "admin"),
        )
    )
    user = store.authenticate("Bearer alice-secret-key")
    assert user.user == "alice" and not user.is_admin
    admin = store.authenticate("Bearer admin-secret-key")
    assert admin.is_admin
    with pytest.raises(AuthError) as missing:
        store.authenticate(None)
    assert missing.value.status_code == 401
    with pytest.raises(AuthError) as invalid:
        store.authenticate("Bearer wrong")
    assert invalid.value.status_code == 401


def test_business_tool_denial_is_logged_but_allowed_call_succeeds(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    env.fake.queue("planner", plan([("business", "查库存")], audit=False))
    env.fake.queue(
        "business",
        multi_call(("search_policies", {"query": "电脑领用"}), ("query_inventory", {})),
        business_findings(),
    )
    task_id = env.system.run("库存多少")
    task = env.repository.get_task(task_id)
    assert task["status"] == "completed"
    events = env.repository.list_events(task_id)
    denied = [event for event in events if event["type"] == "tool_denied"]
    assert denied
    payload = json.loads(denied[0]["payload_json"])
    assert payload["agent"] == "business"
    assert payload["tool"] == "search_policies"
    final = json.loads(task["final_json"])
    tools = {item["tool"] for item in final["evidence"]}
    assert tools == {"query_inventory"}


def test_policy_cannot_read_business_data_and_fails_honestly(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    env.fake.queue(
        "planner",
        plan([("business", "查库存"), ("policy", "查制度")]),
    )
    env.fake.queue("business", tool_call("query_inventory", {}), business_findings())
    env.fake.queue("policy", multi_call(("query_inventory", {}), ("list_tickets", {})))
    task_id = env.system.run("新员工需求能否满足")
    task = env.repository.get_task(task_id)
    assert task["status"] == "partial"
    final = json.loads(task["final_json"])
    assert final["policy_unavailable"] is True
    assert final["policy_summary"] is None
    assert "制度调查失败" in final["conclusion"]
    assert "POL-IT" not in json.dumps(final, ensure_ascii=False)
    events = env.repository.list_events(task_id)
    denied = [json.loads(event["payload_json"]) for event in events if event["type"] == "tool_denied"]
    assert {item["agent"] for item in denied} == {"policy"}
    assert {item["tool"] for item in denied} == {"query_inventory", "list_tickets"}


def test_policy_model_failure_does_not_trigger_substitute_search(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    env.fake.queue("planner", plan([("business", "查库存"), ("policy", "查制度")]))
    env.fake.queue("business", tool_call("query_inventory", {}), business_findings())
    env.fake.queue("policy", ModelError("HTTP_404", "model not found", retryable=False))
    task_id = env.system.run("新员工领用条件")
    task = env.repository.get_task(task_id)
    final = json.loads(task["final_json"])
    assert task["status"] == "partial"
    assert final["policy_unavailable"] is True
    assert final["policy_summary"] is None
    # 业务 Agent 没有制度检索权限，不能替代 Policy 产出制度结论。
    business_calls = [call for call in env.fake.calls if call["role"] == "business"]
    for call in business_calls:
        names = {schema["function"]["name"] for schema in (call["tools"] or [])}
        assert "search_policies" not in names


def test_business_unknown_tool_is_denied_without_data(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    env.fake.queue("planner", plan([("business", "查库存")], audit=False))
    env.fake.queue(
        "business",
        multi_call(("delete_all_tickets", {}), ("query_inventory", {})),
        business_findings(),
    )
    task_id = env.system.run("库存")
    assert env.repository.get_task(task_id)["status"] == "completed"
    events = [json.loads(event["payload_json"]) for event in env.repository.list_events(task_id)]
    assert any(item.get("tool") == "delete_all_tickets" for item in events if "tool" in item)


def test_redact_removes_keys_and_authorization() -> None:
    text = "Authorization: Bearer abc123 api_key=sk-abcdef123456789 token"
    cleaned = redact(text, ("abc123",))
    assert "abc123" not in cleaned
    assert "sk-abcdef" not in cleaned
    assert "[REDACTED]" in cleaned


def test_json_formatter_does_not_emit_secrets(caplog) -> None:
    logger = logging.getLogger("secure-test")
    logger.setLevel(logging.INFO)
    logger.addHandler(caplog.handler)
    logger.info("user key sk-secret1234567890 header Bearer abcdef")
    records = [record.getMessage() for record in caplog.records]
    formatter = JsonFormatter(("sk-secret1234567890",))
    payload = json.loads(formatter.format(caplog.records[-1]))
    assert "sk-secret" not in json.dumps(payload, ensure_ascii=False)
    assert "Bearer abcdef" not in json.dumps(payload, ensure_ascii=False)
    assert records
