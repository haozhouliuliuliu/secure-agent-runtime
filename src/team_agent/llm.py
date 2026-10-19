"""OpenAI 兼容模型客户端：多供应商、限流、预算与用量记录。"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import ModelConfig
from .errors import BudgetExceeded, ModelError


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    raw_arguments: str
    arguments: dict[str, Any] | None
    parse_error: str | None = None


@dataclass(frozen=True, slots=True)
class LLMResponse:
    content: str | None
    tool_calls: tuple[ToolCall, ...]
    usage: dict[str, int] = field(default_factory=dict)
    latency_ms: int = 0
    model: str = ""

    @property
    def total_tokens(self) -> int:
        return int(self.usage.get("total_tokens") or 0)


class CallBudget:
    """全局与单任务调用预算；重试与失败请求同样计数。"""

    def __init__(self, max_total: int, max_per_task: int) -> None:
        self.max_total = max_total
        self.max_per_task = max_per_task
        self.total_calls = 0
        self.task_calls = 0
        self._lock = threading.Lock()

    def start_task(self) -> None:
        with self._lock:
            self.task_calls = 0

    def reserve(self) -> int:
        with self._lock:
            if self.total_calls >= self.max_total:
                raise BudgetExceeded(f"达到全局模型调用上限（{self.max_total} 次）")
            if self.task_calls >= self.max_per_task:
                raise BudgetExceeded(f"达到单任务模型调用上限（{self.max_per_task} 次）")
            self.total_calls += 1
            self.task_calls += 1
            return self.total_calls


class LLMClient:
    def __init__(self, *, timeout_seconds: float = 180.0) -> None:
        self.timeout_seconds = timeout_seconds
        self._client = httpx.Client(timeout=httpx.Timeout(timeout_seconds))
        self._rate_lock = threading.Lock()
        self._last_call: dict[str, float] = {}

    def close(self) -> None:
        self._client.close()

    def _wait_for_rate_limit(self, config: ModelConfig) -> None:
        if config.min_interval_seconds <= 0:
            return
        with self._rate_lock:
            last = self._last_call.get(config.provider, 0.0)
            wait = config.min_interval_seconds - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
            self._last_call[config.provider] = time.monotonic()

    def complete(
        self,
        config: ModelConfig,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        budget: CallBudget,
        max_tokens: int = 1200,
    ) -> LLMResponse:
        budget.reserve()
        self._wait_for_rate_limit(config)

        payload: dict[str, Any] = {
            "model": config.model,
            "messages": messages,
            "temperature": config.temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        started = time.perf_counter()
        try:
            response = self._client.post(
                config.api_base.rstrip("/") + "/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {config.api_key}"},
            )
        except httpx.TimeoutException as exc:
            raise ModelError("TIMEOUT", f"{config.role} 模型请求超时", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ModelError(
                "CONNECTION_ERROR", f"{config.role} 网络错误：{exc.__class__.__name__}", retryable=True
            ) from exc

        latency_ms = int((time.perf_counter() - started) * 1000)
        self._raise_for_status(response, config)
        return self._parse(response, config, latency_ms)

    @staticmethod
    def _raise_for_status(response: httpx.Response, config: ModelConfig) -> None:
        status = response.status_code
        if status < 400:
            return
        detail = (response.text or "").strip().replace("\n", " ")[:160]
        if status in (401, 403):
            raise ModelError("HTTP_401", f"{config.role} 鉴权失败：{detail}", retryable=False)
        if status == 404:
            raise ModelError("HTTP_404", f"{config.role} 模型/接口不存在：{detail}", retryable=False)
        if status == 429:
            raise ModelError("HTTP_429", f"{config.role} 触发限流：{detail}", retryable=True)
        if status >= 500:
            raise ModelError("HTTP_5XX", f"{config.role} 服务端错误：{detail}", retryable=True)
        raise ModelError("HTTP_4XX", f"{config.role} 请求被拒绝：{detail}", retryable=False)

    @staticmethod
    def _parse(response: httpx.Response, config: ModelConfig, latency_ms: int) -> LLMResponse:
        try:
            payload = response.json()
            message = payload["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ModelError("INVALID_RESPONSE", f"{config.role} 响应结构不合法", retryable=True) from exc

        content = message.get("content")
        calls: list[ToolCall] = []
        for index, item in enumerate(message.get("tool_calls") or []):
            function = item.get("function") or {}
            raw = function.get("arguments") or "{}"
            arguments: dict[str, Any] | None = None
            error: str | None = None
            try:
                decoded = json.loads(raw)
            except json.JSONDecodeError as exc:
                error = f"工具参数不是合法 JSON：{exc.msg}"
            else:
                if isinstance(decoded, dict):
                    arguments = decoded
                else:
                    error = "工具参数必须是 JSON 对象"
            calls.append(
                ToolCall(
                    id=item.get("id") or f"call_{index}",
                    name=function.get("name") or "",
                    raw_arguments=raw,
                    arguments=arguments,
                    parse_error=error,
                )
            )

        usage = payload.get("usage") or {}
        normalized = {
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
        }
        return LLMResponse(
            content=content if isinstance(content, str) else None,
            tool_calls=tuple(calls),
            usage=normalized,
            latency_ms=latency_ms,
            model=payload.get("model") or config.model,
        )

    def __enter__(self) -> LLMClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
