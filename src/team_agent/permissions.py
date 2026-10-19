"""Agent 工具最小权限：唯一授权来源。

两层强制：
1. 模型请求里只放该 Agent 允许的工具（schemas_for）；
2. 真正执行前再次校验（ReadOnlyTools.execute）。

提示词不参与授权判断。
"""

from __future__ import annotations

TOOL_ALLOWLIST: dict[str, frozenset[str]] = {
    "planner": frozenset(),
    "business": frozenset({"query_inventory", "get_asset", "get_ticket", "list_tickets"}),
    "policy": frozenset({"search_policies"}),
    "auditor": frozenset(),
}

KNOWN_TOOLS = frozenset({"query_inventory", "get_asset", "get_ticket", "list_tickets", "search_policies"})


def allowed_tools(agent: str) -> frozenset[str]:
    return TOOL_ALLOWLIST.get(agent, frozenset())


def is_allowed(agent: str, tool: str) -> bool:
    return tool in allowed_tools(agent)
