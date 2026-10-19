"""项目异常。"""

from __future__ import annotations


class AgentError(Exception):
    """所有可预期错误的基类。"""


class ConfigError(AgentError):
    """配置缺失或非法。"""


class SeedDataError(AgentError):
    """种子数据缺失或格式不合法。"""


class ModelError(AgentError):
    """模型调用失败。code 稳定可枚举；retryable 决定是否值得重试。"""

    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class InvalidStructuredOutput(AgentError):
    """模型返回的内容无法通过 JSON/Pydantic 校验。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class BudgetExceeded(AgentError):
    """超出模型调用预算/单任务上限。"""
