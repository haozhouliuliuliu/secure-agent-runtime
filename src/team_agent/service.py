"""受控任务服务：认证后的任务提交、并发限制、限流、预算与隔离。"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .auth import User
from .config import Settings
from .orchestrator import CollaborationSystem, build_system
from .repository import Repository, TaskNotFound

logger = logging.getLogger("secure_agent.service")

TERMINAL = {"completed", "partial", "failed"}


class ServiceError(Exception):
    def __init__(self, status_code: int, code: str) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code


class RateLimitError(ServiceError):
    def __init__(self) -> None:
        super().__init__(429, "RATE_LIMITED")


class OverloadedError(ServiceError):
    def __init__(self) -> None:
        super().__init__(503, "OVERLOADED")


class NotFoundError(ServiceError):
    def __init__(self) -> None:
        super().__init__(404, "TASK_NOT_FOUND")


class InputTooLargeError(ServiceError):
    def __init__(self) -> None:
        super().__init__(413, "INPUT_TOO_LARGE")


class SlidingWindowLimiter:
    def __init__(self, limit: int, window_seconds: float = 60.0) -> None:
        self.limit = limit
        self.window = window_seconds
        self._events: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            events = self._events.setdefault(key, deque())
            while events and now - events[0] > self.window:
                events.popleft()
            if len(events) >= self.limit:
                return False
            events.append(now)
            return True


def safe_error_code(task: dict[str, Any]) -> str | None:
    """只返回稳定错误码，绝不回显供应商的原始错误文本。"""
    raw = (task.get("error") or "").strip()
    if not raw:
        return None
    candidate = raw.split(":", 1)[0].strip().upper()
    if candidate and all(ch.isalnum() or ch == "_" for ch in candidate) and len(candidate) <= 40:
        return candidate
    return "TASK_FAILED"


class TaskService:
    def __init__(
        self,
        settings: Settings,
        *,
        system: CollaborationSystem | None = None,
        repository: Repository | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository or Repository(Path(settings.db_path))
        self.system = system or build_system(settings)
        self.executor = ThreadPoolExecutor(max_workers=settings.max_concurrent_tasks, thread_name_prefix="secure-task")
        self._slots = threading.BoundedSemaphore(settings.max_concurrent_tasks)
        self._limiter = SlidingWindowLimiter(settings.max_requests_per_minute)
        self._closed = False

    # ---------------------------------------------------------------- 生命周期

    def recover_interrupted(self) -> int:
        return self.repository.recover_interrupted()

    def ready(self) -> bool:
        if not self.settings.api_keys:
            return False
        if any(
            config is None
            for config in (self.settings.planner, self.settings.business, self.settings.policy, self.settings.auditor)
        ):
            return False
        return self.repository.ping()

    def shutdown(self) -> None:
        self._closed = True
        self.executor.shutdown(wait=False, cancel_futures=True)

    # ---------------------------------------------------------------- 任务

    def submit(self, user: User, request: str) -> str:
        if len(request) > self.settings.max_input_chars:
            raise InputTooLargeError()
        if not self._limiter.allow(user.user):
            logger.warning("rate limited", extra={"user": user.user, "status": 429})
            raise RateLimitError()
        if not self._slots.acquire(blocking=False):
            raise OverloadedError()
        try:
            task_id = self.repository.create_task(request, owner=user.user)
            self.executor.submit(self._run, task_id)
        except Exception:
            self._slots.release()
            raise
        logger.info(
            "task accepted",
            extra={"task_id": task_id, "user": user.user, "status": 202},
        )
        return task_id

    def _run(self, task_id: str) -> None:
        try:
            self.system.execute_task(task_id)
        except Exception:
            logger.exception("task crashed", extra={"task_id": task_id})
            try:
                self.repository.update_task(task_id, status="failed", error="INTERNAL_ERROR")
            except Exception:
                logger.error("failed to mark task", extra={"task_id": task_id})
        finally:
            self._slots.release()
            logger.info("task slot released", extra={"task_id": task_id})

    def get_task(self, user: User, task_id: str) -> dict[str, Any]:
        try:
            task = self.repository.get_task(task_id)
        except TaskNotFound as exc:
            raise NotFoundError() from exc
        if not user.is_admin and task.get("owner") != user.user:
            raise NotFoundError()
        return task

    def list_tasks(self, user: User, limit: int = 20) -> list[dict[str, Any]]:
        if user.is_admin:
            return self.repository.list_tasks(limit=limit)
        return self.repository.list_tasks(limit=limit, owner=user.user)

    def delete_task(self, user: User, task_id: str) -> None:
        if not user.is_admin:
            raise ServiceError(403, "FORBIDDEN")
        try:
            task = self.repository.get_task(task_id)
        except TaskNotFound as exc:
            raise NotFoundError() from exc
        self.repository.delete_task(task["id"])
        logger.info("task deleted", extra={"task_id": task_id, "user": user.user})


def public_task(task: dict[str, Any]) -> dict[str, Any]:
    """API 输出：只含安全字段，不含内部错误文本、prompt 或密钥。"""
    import json

    final = json.loads(task["final_json"]) if task.get("final_json") else None
    return {
        "task_id": task["id"],
        "owner": task.get("owner"),
        "status": task["status"],
        "created_at": task["created_at"],
        "updated_at": task["updated_at"],
        "model_calls": task["model_calls"],
        "total_tokens": task["total_tokens"],
        "result": final,
        "error_code": safe_error_code(task),
    }
