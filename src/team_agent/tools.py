"""只读工具注册表：库存、设备、工单、制度检索。

每次执行都会生成可引用的证据 ID（EV-xxxxxxxx），Agent 的结论必须绑定这些 ID。
不存在任何写工具、SQL、文件或命令入口。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .errors import AgentError
from .permissions import allowed_tools, is_allowed
from .policies import PolicyIndex
from .store import DeskStore, NotFound, StoreError


class ToolError(AgentError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchPoliciesArgs(_Args):
    query: str = Field(min_length=1, max_length=120, description="检索关键词")
    limit: int = Field(default=3, ge=1, le=5)


class QueryInventoryArgs(_Args):
    category: Literal["笔记本电脑", "显示器", "扩展坞", "手机"] | None = Field(
        default=None, description="设备类别；不传表示全部"
    )
    only_available: bool = Field(default=True, description="只返回有可用库存的型号")


class GetAssetArgs(_Args):
    asset_id: str | None = Field(default=None, min_length=3, max_length=40)
    assignee: str | None = Field(default=None, min_length=1, max_length=40)

    @model_validator(mode="after")
    def _exactly_one(self) -> GetAssetArgs:
        if (self.asset_id is None) == (self.assignee is None):
            raise ValueError("asset_id 与 assignee 必须且只能提供一个")
        return self


class GetTicketArgs(_Args):
    ticket_id: str = Field(pattern=r"^INC-\d{4}-\d{4}$")


class ListTicketsArgs(_Args):
    status: Literal["open", "in_progress", "pending", "resolved", "closed"] | None = None
    assignee: str | None = Field(default=None, max_length=40)
    limit: int = Field(default=10, ge=1, le=20)


@dataclass(frozen=True, slots=True)
class Evidence:
    evidence_id: str
    tool: str
    args: dict[str, Any]
    data: Any

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "tool": self.tool,
            "args": self.args,
            "data": self.data,
        }


_SPECS = [
    {
        "name": "search_policies",
        "description": "检索虚构企业 IT 制度，返回文档标题、章节、引用与片段。制度问题必须使用。",
        "parameters": SearchPoliciesArgs.model_json_schema(),
        "model": SearchPoliciesArgs,
    },
    {
        "name": "query_inventory",
        "description": "查询设备库存数量与可用状态。",
        "parameters": QueryInventoryArgs.model_json_schema(),
        "model": QueryInventoryArgs,
    },
    {
        "name": "get_asset",
        "description": "按设备编号或领用人查询设备归属与状态。",
        "parameters": GetAssetArgs.model_json_schema(),
        "model": GetAssetArgs,
    },
    {
        "name": "get_ticket",
        "description": "查询单个工单详情。",
        "parameters": GetTicketArgs.model_json_schema(),
        "model": GetTicketArgs,
    },
    {
        "name": "list_tickets",
        "description": "按状态或处理人列出工单。",
        "parameters": ListTicketsArgs.model_json_schema(),
        "model": ListTicketsArgs,
    },
]


class ReadOnlyTools:
    def __init__(self, store: DeskStore, policies: PolicyIndex) -> None:
        self.store = store
        self.policies = policies

    def schemas_for(self, agent: str) -> list[dict[str, Any]]:
        """只返回该 Agent 被授权的工具；未授权工具不会出现在模型请求里。"""
        allowed = allowed_tools(agent)
        result = []
        for spec in _SPECS:
            if spec["name"] not in allowed:
                continue
            parameters = dict(spec["parameters"])
            parameters.pop("title", None)
            result.append(
                {
                    "type": "function",
                    "function": {
                        "name": spec["name"],
                        "description": spec["description"],
                        "parameters": parameters,
                    },
                }
            )
        return result

    def execute(self, agent: str, name: str, raw_args: dict[str, Any]) -> Evidence:
        # 程序级授权：即使模型绕过工具列表，也无法执行未授权工具。
        if not is_allowed(agent, name):
            raise ToolError("TOOL_NOT_PERMITTED", f"{agent} 无权调用工具 {name}")
        spec = next((item for item in _SPECS if item["name"] == name), None)
        if spec is None:
            raise ToolError("TOOL_NOT_FOUND", f"未注册的工具：{name}")
        try:
            args = spec["model"].model_validate(raw_args)
        except ValidationError as exc:
            detail = "; ".join(
                f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}" for error in exc.errors()[:3]
            )
            raise ToolError("INVALID_ARGUMENTS", f"参数不合法：{detail}") from exc

        try:
            data = self._dispatch(name, args)
        except NotFound as exc:
            raise ToolError("NOT_FOUND", str(exc)) from exc
        except StoreError as exc:
            raise ToolError("TOOL_ERROR", str(exc)) from exc

        payload = {"tool": name, "args": args.model_dump(mode="json"), "data": data}
        digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[
            :10
        ]
        return Evidence(
            evidence_id=f"EV-{digest}",
            tool=name,
            args=args.model_dump(mode="json"),
            data=data,
        )

    def _dispatch(self, name: str, args: BaseModel) -> Any:
        if name == "search_policies":
            assert isinstance(args, SearchPoliciesArgs)
            results = self.policies.search(args.query, args.limit)
            return {
                "query": args.query,
                "count": len(results),
                "results": results,
                "message": "未检索到条款，不得编造制度" if not results else "",
            }
        if name == "query_inventory":
            assert isinstance(args, QueryInventoryArgs)
            items = self.store.inventory(category=args.category, only_available=args.only_available)
            return {"items": items, "total_available": sum(item["available"] for item in items)}
        if name == "get_asset":
            assert isinstance(args, GetAssetArgs)
            data = self.store.get_asset(asset_id=args.asset_id, assignee=args.assignee)
            return {"assets": data if isinstance(data, list) else [data]}
        if name == "get_ticket":
            assert isinstance(args, GetTicketArgs)
            return {"ticket": self.store.get_ticket(args.ticket_id)}
        if name == "list_tickets":
            assert isinstance(args, ListTicketsArgs)
            tickets = self.store.list_tickets(status=args.status, assignee=args.assignee, limit=args.limit)
            return {"tickets": tickets, "count": len(tickets)}
        raise ToolError("TOOL_NOT_FOUND", f"未注册的工具：{name}")
