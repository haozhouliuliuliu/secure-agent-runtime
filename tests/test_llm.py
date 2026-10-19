"""模型客户端：解析、错误码、重试标记、超时与预算。"""

from __future__ import annotations

import json

import httpx
import pytest

from team_agent.budget import BudgetManager
from team_agent.config import ModelConfig
from team_agent.errors import BudgetExceeded, ModelError
from team_agent.llm import LLMClient

CONFIG = ModelConfig(
    role="business",
    provider="qwen",
    api_base="https://example.invalid/v1",
    api_key="test-key",
    model="test-model",
    temperature=0.0,
)


def make_client(handler) -> LLMClient:
    client = LLMClient(timeout_seconds=5)
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    return client


def budget() -> tuple[BudgetManager, object]:
    manager = BudgetManager(10, 10)
    return manager, manager.for_task("t1")


def test_successful_tool_call_is_parsed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "test-model",
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "function": {
                                        "name": "query_inventory",
                                        "arguments": json.dumps({"category": "笔记本电脑"}),
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )

    manager, task_budget = budget()
    with make_client(handler) as client:
        response = client.complete(CONFIG, [{"role": "user", "content": "查库存"}], budget=task_budget)
    assert response.tool_calls[0].name == "query_inventory"
    assert response.tool_calls[0].arguments == {"category": "笔记本电脑"}
    assert response.usage["total_tokens"] == 15
    assert manager.total_calls == 1


def test_401_is_not_retryable() -> None:
    with (
        make_client(lambda request: httpx.Response(401, text="bad key")) as client,
        pytest.raises(ModelError) as excinfo,
    ):
        client.complete(CONFIG, [{"role": "user", "content": "x"}], budget=budget()[1])
    assert excinfo.value.code == "HTTP_401"
    assert excinfo.value.retryable is False


def test_429_is_retryable() -> None:
    with (
        make_client(lambda request: httpx.Response(429, text="slow down")) as client,
        pytest.raises(ModelError) as excinfo,
    ):
        client.complete(CONFIG, [{"role": "user", "content": "x"}], budget=budget()[1])
    assert excinfo.value.code == "HTTP_429"
    assert excinfo.value.retryable is True


def test_5xx_is_retryable() -> None:
    with (
        make_client(lambda request: httpx.Response(503, text="unavailable")) as client,
        pytest.raises(ModelError) as excinfo,
    ):
        client.complete(CONFIG, [{"role": "user", "content": "x"}], budget=budget()[1])
    assert excinfo.value.code == "HTTP_5XX"
    assert excinfo.value.retryable is True


def test_timeout_is_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout", request=request)

    with make_client(handler) as client, pytest.raises(ModelError) as excinfo:
        client.complete(CONFIG, [{"role": "user", "content": "x"}], budget=budget()[1])
    assert excinfo.value.code == "TIMEOUT"
    assert excinfo.value.retryable is True


def test_invalid_json_body_is_rejected() -> None:
    with (
        make_client(lambda request: httpx.Response(200, text="not-json")) as client,
        pytest.raises(ModelError) as excinfo,
    ):
        client.complete(CONFIG, [{"role": "user", "content": "x"}], budget=budget()[1])
    assert excinfo.value.code == "INVALID_RESPONSE"


def test_invalid_tool_arguments_are_marked() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [{"id": "c1", "function": {"name": "get_ticket", "arguments": "{"}}],
                        }
                    }
                ],
                "usage": {},
            },
        )

    with make_client(handler) as client:
        response = client.complete(CONFIG, [{"role": "user", "content": "x"}], budget=budget()[1])
    assert response.tool_calls[0].arguments is None
    assert response.tool_calls[0].parse_error


def test_budget_exceeded_blocks_request() -> None:
    manager = BudgetManager(1, 1)
    task_budget = manager.for_task("t1")
    manager.reserve("t1")
    with make_client(lambda request: httpx.Response(200, json={})) as client, pytest.raises(BudgetExceeded):
        client.complete(CONFIG, [{"role": "user", "content": "x"}], budget=task_budget)
