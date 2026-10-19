"""配置与模型路由测试。"""

from __future__ import annotations

import pytest

from team_agent.cli import main
from team_agent.config import Settings
from team_agent.errors import ConfigError

ENV = {
    "DEEPSEEK_API_KEY": "k1",
    "QWEN_API_KEY": "k2",
    "KIMI_API_KEY": "k3",
    "PLANNER_MODEL": "deepseek-flash",
    "BUSINESS_MODEL": "qwen3.8-flash",
    "POLICY_MODEL": "qwen3.8-flash",
    "AUDITOR_MODEL": "kimi-k2.7-code",
}


def test_models_are_configured_per_role() -> None:
    settings = Settings.from_env(ENV)
    assert settings.planner.model == "deepseek-flash"
    assert settings.business.model == "qwen3.8-flash"
    assert settings.policy.model == "qwen3.8-flash"
    assert settings.auditor.model == "kimi-k2.7-code"
    assert settings.auditor.temperature == 1.0


def test_auditor_uses_different_model_from_investigators() -> None:
    settings = Settings.from_env(ENV)
    assert settings.auditor.model != settings.business.model
    assert settings.auditor.model != settings.policy.model


def test_at_least_two_model_ids() -> None:
    settings = Settings.from_env(ENV)
    models = {settings.planner.model, settings.business.model, settings.auditor.model}
    assert len(models) >= 2


def test_missing_key_is_rejected() -> None:
    env = dict(ENV)
    env.pop("KIMI_API_KEY")
    with pytest.raises(ConfigError):
        Settings.from_env(env)


def test_budget_defaults() -> None:
    settings = Settings.from_env(ENV)
    assert settings.max_total_calls == 50
    assert settings.max_calls_per_task == 12
    assert settings.revision_limit == 1


def test_models_command_without_keys(monkeypatch, capsys) -> None:
    for key in ("DEEPSEEK_API_KEY", "QWEN_API_KEY", "KIMI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    assert main(["models"]) == 0
    output = capsys.readouterr().out
    assert "planner" in output
    assert "auditor" in output
    assert "kimi" in output
