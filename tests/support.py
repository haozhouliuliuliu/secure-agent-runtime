"""测试支撑：脚本化模型 + 真实工具/SQLite 的协作系统。"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from team_agent.budget import BudgetManager
from team_agent.config import ModelConfig, Settings
from team_agent.db import init_database
from team_agent.llm import LLMResponse, ToolCall
from team_agent.orchestrator import CollaborationSystem
from team_agent.policies import PolicyIndex
from team_agent.repository import Repository
from team_agent.store import DeskStore
from team_agent.tools import ReadOnlyTools

SEED_DIR = Path(__file__).resolve().parents[1] / "seed_data"


def model(role: str, name: str) -> ModelConfig:
    return ModelConfig(
        role=role,
        provider=role,
        api_base="http://test.invalid/v1",
        api_key="test-key",
        model=name,
        temperature=0.0,
    )


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    payload = {
        "db_path": tmp_path / "collab.db",
        "seed_dir": SEED_DIR,
        "planner": model("planner", "deepseek-test"),
        "business": model("business", "qwen-test"),
        "policy": model("policy", "qwen-policy-test"),
        "auditor": model("auditor", "kimi-auditor-test"),
        "max_total_calls": 50,
        "max_calls_per_task": 12,
        "revision_limit": 1,
    }
    payload.update(overrides)
    return Settings(**payload)


class FakeLLM:
    """按 role 排队返回预置响应；callable 可读取 messages 动态生成。"""

    def __init__(self) -> None:
        self.queues: dict[str, list[Any]] = {}
        self.defaults: dict[str, Any] = {}
        self.calls: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def set_default(self, role: str, handler: Any) -> None:
        self.defaults[role] = handler

    def queue(self, role: str, *items: Any) -> None:
        with self._lock:
            self.queues.setdefault(role, []).extend(items)

    def complete(self, config, messages, *, tools=None, budget=None, max_tokens=1200) -> LLMResponse:
        if budget is not None:
            budget.reserve()
        with self._lock:
            self.calls.append({"role": config.role, "model": config.model, "messages": messages, "tools": tools})
            queue = self.queues.get(config.role)
            if queue:
                item = queue.pop(0)
            elif config.role in self.defaults:
                item = self.defaults[config.role]
            else:
                raise AssertionError(f"{config.role} 没有预置响应")
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item(messages)
        assert isinstance(item, LLMResponse)
        return item

    def close(self) -> None:
        pass


@dataclass
class Env:
    system: CollaborationSystem
    fake: FakeLLM
    settings: Settings
    repository: Repository
    calls: list = field(default_factory=list)


def build_env(tmp_path: Path, **overrides: Any) -> Env:
    settings = make_settings(tmp_path, **overrides)
    init_database(settings.db_path, SEED_DIR)
    repository = Repository(settings.db_path)
    tools = ReadOnlyTools(DeskStore(settings.db_path), PolicyIndex.from_dir(SEED_DIR / "policies"))
    fake = FakeLLM()
    system = CollaborationSystem(
        settings=settings,
        repository=repository,
        tools=tools,
        client=fake,
        budget=BudgetManager(settings.max_total_calls, settings.max_calls_per_task),
    )
    return Env(system=system, fake=fake, settings=settings, repository=repository)


def answer(content: str) -> LLMResponse:
    return LLMResponse(content=content, tool_calls=(), usage={"prompt_tokens": 10, "completion_tokens": 5})


def tool_call(name: str, args: dict[str, Any], call_id: str = "call_1") -> LLMResponse:
    return LLMResponse(
        content=None,
        tool_calls=(
            ToolCall(
                id=call_id,
                name=name,
                raw_arguments=json.dumps(args, ensure_ascii=False),
                arguments=args,
            ),
        ),
        usage={"prompt_tokens": 12, "completion_tokens": 6},
    )


def plan(subtasks: list[tuple[str, str]], *, parallel: bool = True, audit: bool = True) -> LLMResponse:
    return answer(
        json.dumps(
            {
                "subtasks": [{"agent": agent, "objective": objective} for agent, objective in subtasks],
                "parallel": parallel,
                "needs_audit": audit,
                "reasoning": "test",
            },
            ensure_ascii=False,
        )
    )


def business_findings(summary: str = "可用笔记本 4 台", claim: str | None = None) -> Callable:
    def handler(messages: list[dict[str, Any]]) -> LLMResponse:
        evidence_id = "EV-MISSING"
        for message in reversed(messages):
            if message.get("role") != "tool":
                continue
            try:
                payload = json.loads(message.get("content") or "{}")
            except json.JSONDecodeError:
                continue
            if payload.get("evidence_id"):
                evidence_id = payload["evidence_id"]
                break
        payload = {
            "summary": summary,
            "claims": [
                {
                    "claim": claim or summary,
                    "evidence_ids": [evidence_id],
                    "value": "4",
                }
            ],
            "data_gaps": [],
            "confidence": 0.9,
        }
        return answer(json.dumps(payload, ensure_ascii=False))

    return handler


def policy_findings(
    condition: str = "研发岗标准配置为 ThinkPad T14",
    *,
    status: str = "现行",
    conflicts: list[dict[str, Any]] | None = None,
) -> LLMResponse:
    return answer(
        json.dumps(
            {
                "rules": [
                    {
                        "doc_id": "POL-IT-001",
                        "title": "电脑领用制度",
                        "section": "配置标准",
                        "version": "v2.1",
                        "status": status,
                        "condition": condition,
                        "citation": "电脑领用制度 · 配置标准",
                    }
                ],
                "conflicts": conflicts or [],
                "gaps": [],
                "confidence": 0.9,
            },
            ensure_ascii=False,
        )
    )


def audit_result(
    verdict: str = "pass",
    *,
    summary: str = "所有结论均有证据支持",
    findings: list[dict[str, Any]] | None = None,
    conflicts: list[dict[str, Any]] | None = None,
    followups: list[str] | None = None,
) -> LLMResponse:
    return answer(
        json.dumps(
            {
                "verdict": verdict,
                "findings": findings or [],
                "conflicts": conflicts or [],
                "unsupported_claims": [],
                "required_followups": followups or [],
                "summary": summary,
                "confidence": 0.9,
            },
            ensure_ascii=False,
        )
    )


def run_simple(env: Env, request: str = "现在笔记本库存多少？") -> dict[str, Any]:
    env.fake.queue("planner", plan([("business", "查询库存")], audit=False))
    env.fake.queue("business", tool_call("query_inventory", {}), business_findings())
    task_id = env.system.run(request)
    return env.repository.get_task(task_id)
