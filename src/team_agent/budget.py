"""并发安全的模型调用预算。

- 全局上限：所有任务共享，任何并发都不能突破；
- 单任务上限：每个任务独立计数，任务之间不串扰。
"""

from __future__ import annotations

import threading

from .errors import BudgetExceeded


class BudgetManager:
    def __init__(self, global_limit: int, per_task_limit: int) -> None:
        if global_limit < 1 or per_task_limit < 1:
            raise ValueError("预算上限必须大于 0")
        self.global_limit = global_limit
        self.per_task_limit = per_task_limit
        self._lock = threading.Lock()
        self._total = 0
        self._per_task: dict[str, int] = {}

    @property
    def total_calls(self) -> int:
        with self._lock:
            return self._total

    def task_calls(self, task_id: str) -> int:
        with self._lock:
            return self._per_task.get(task_id, 0)

    def reserve(self, task_id: str) -> int:
        with self._lock:
            if self._total >= self.global_limit:
                raise BudgetExceeded(f"达到全局模型调用上限（{self.global_limit} 次）")
            used = self._per_task.get(task_id, 0)
            if used >= self.per_task_limit:
                raise BudgetExceeded(f"达到单任务模型调用上限（{self.per_task_limit} 次）")
            self._total += 1
            self._per_task[task_id] = used + 1
            return self._total

    def for_task(self, task_id: str) -> TaskBudget:
        return TaskBudget(self, task_id)


class TaskBudget:
    def __init__(self, manager: BudgetManager, task_id: str) -> None:
        self.manager = manager
        self.task_id = task_id

    @property
    def max_total(self) -> int:
        return self.manager.global_limit

    @property
    def max_per_task(self) -> int:
        return self.manager.per_task_limit

    @property
    def total_calls(self) -> int:
        return self.manager.total_calls

    @property
    def task_calls(self) -> int:
        return self.manager.task_calls(self.task_id)

    def reserve(self) -> int:
        return self.manager.reserve(self.task_id)
