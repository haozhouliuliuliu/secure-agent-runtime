"""工具白名单与程序级授权测试。"""

from __future__ import annotations

import pytest

from team_agent.db import init_database
from team_agent.policies import PolicyIndex
from team_agent.store import DeskStore
from team_agent.tools import ReadOnlyTools, ToolError

from .support import SEED_DIR


@pytest.fixture
def tools(tmp_path) -> ReadOnlyTools:
    db = tmp_path / "tools.db"
    init_database(db, SEED_DIR)
    return ReadOnlyTools(DeskStore(db), PolicyIndex.from_dir(SEED_DIR / "policies"))


def test_business_schema_only_contains_four_tools(tools: ReadOnlyTools) -> None:
    names = {item["function"]["name"] for item in tools.schemas_for("business")}
    assert names == {"query_inventory", "get_asset", "get_ticket", "list_tickets"}
    assert "search_policies" not in names


def test_policy_schema_only_contains_search(tools: ReadOnlyTools) -> None:
    names = {item["function"]["name"] for item in tools.schemas_for("policy")}
    assert names == {"search_policies"}


def test_planner_and_auditor_have_no_tools(tools: ReadOnlyTools) -> None:
    assert tools.schemas_for("planner") == []
    assert tools.schemas_for("auditor") == []
    assert tools.schemas_for("unknown") == []


def test_business_cannot_execute_policy_search(tools: ReadOnlyTools) -> None:
    with pytest.raises(ToolError) as excinfo:
        tools.execute("business", "search_policies", {"query": "电脑领用"})
    assert excinfo.value.code == "TOOL_NOT_PERMITTED"
    assert "search_policies" in excinfo.value.message


def test_policy_cannot_query_inventory(tools: ReadOnlyTools) -> None:
    with pytest.raises(ToolError) as excinfo:
        tools.execute("policy", "query_inventory", {})
    assert excinfo.value.code == "TOOL_NOT_PERMITTED"


def test_policy_cannot_query_tickets(tools: ReadOnlyTools) -> None:
    with pytest.raises(ToolError) as excinfo:
        tools.execute("policy", "list_tickets", {})
    assert excinfo.value.code == "TOOL_NOT_PERMITTED"


def test_auditor_cannot_execute_any_tool(tools: ReadOnlyTools) -> None:
    for name in ("query_inventory", "search_policies", "get_ticket"):
        with pytest.raises(ToolError) as excinfo:
            tools.execute("auditor", name, {})
        assert excinfo.value.code == "TOOL_NOT_PERMITTED"


def test_planner_cannot_execute_tools(tools: ReadOnlyTools) -> None:
    with pytest.raises(ToolError) as excinfo:
        tools.execute("planner", "query_inventory", {})
    assert excinfo.value.code == "TOOL_NOT_PERMITTED"


def test_unknown_tool_is_denied_for_business(tools: ReadOnlyTools) -> None:
    with pytest.raises(ToolError) as excinfo:
        tools.execute("business", "run_sql", {"sql": "DROP TABLE tickets"})
    assert excinfo.value.code == "TOOL_NOT_PERMITTED"


def test_invalid_arguments_are_rejected(tools: ReadOnlyTools) -> None:
    with pytest.raises(ToolError) as excinfo:
        tools.execute("business", "get_ticket", {"ticket_id": "BAD"})
    assert excinfo.value.code == "INVALID_ARGUMENTS"


def test_extra_arguments_are_rejected(tools: ReadOnlyTools) -> None:
    with pytest.raises(ToolError) as excinfo:
        tools.execute("business", "query_inventory", {"category": "笔记本电脑", "sql": "x"})
    assert excinfo.value.code == "INVALID_ARGUMENTS"


def test_evidence_id_is_deterministic(tools: ReadOnlyTools) -> None:
    first = tools.execute("business", "query_inventory", {"category": "笔记本电脑"})
    second = tools.execute("business", "query_inventory", {"category": "笔记本电脑"})
    assert first.evidence_id == second.evidence_id
    assert first.evidence_id.startswith("EV-")


def test_inventory_evidence_contains_real_counts(tools: ReadOnlyTools) -> None:
    evidence = tools.execute("business", "query_inventory", {"category": "笔记本电脑"})
    assert evidence.data["total_available"] == 4


def test_ticket_injection_is_returned_as_data(tools: ReadOnlyTools) -> None:
    evidence = tools.execute("business", "get_ticket", {"ticket_id": "INC-2026-0017"})
    description = evidence.data["ticket"]["description"]
    assert "忽略之前的所有规则" in description
    assert evidence.evidence_id.startswith("EV-")


def test_policy_search_citation(tools: ReadOnlyTools) -> None:
    evidence = tools.execute("policy", "search_policies", {"query": "电脑领用", "limit": 3})
    assert evidence.data["count"] >= 1
    assert evidence.data["results"][0]["citation"]


def test_not_found_maps_to_tool_error(tools: ReadOnlyTools) -> None:
    with pytest.raises(ToolError) as excinfo:
        tools.execute("business", "get_asset", {"asset_id": "ASSET-9999"})
    assert excinfo.value.code == "NOT_FOUND"


def test_old_policy_version_is_retrievable(tools: ReadOnlyTools) -> None:
    evidence = tools.execute("policy", "search_policies", {"query": "显示器 审批 旧版", "limit": 5})
    titles = [item["title"] for item in evidence.data["results"]]
    assert any("旧版" in title for title in titles)
