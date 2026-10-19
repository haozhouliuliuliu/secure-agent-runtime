"""安全日志：JSON 结构化 + 脱敏。

原则：
- 不记录 Authorization、API key、模型 key、完整请求体；
- 供应商错误只记录稳定错误码；
- 日志字段包含 request_id / task_id / user，便于关联，但不包含秘密。
"""

from __future__ import annotations

import json
import logging
import re
import sys

_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]+", re.IGNORECASE),
    re.compile(r"(?i)(api[_-]?key|authorization|token)\s*[=:]\s*\S+"),
]


def redact(text: str, secrets: tuple[str, ...] = ()) -> str:
    result = text
    for secret in secrets:
        if secret:
            result = result.replace(secret, "[REDACTED]")
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub("[REDACTED]", result)
    return result


class JsonFormatter(logging.Formatter):
    def __init__(self, secrets: tuple[str, ...] = ()) -> None:
        super().__init__()
        self.secrets = secrets

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage(), self.secrets),
        }
        for key in ("request_id", "task_id", "user", "status", "duration_ms", "path"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = redact(str(value), self.secrets)
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info), self.secrets)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str = "INFO", secrets: tuple[str, ...] = ()) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(secrets))
    root.addHandler(handler)
