"""真实模型安全评估：受控 API + 权限/故障降级，总调用 ≤25。"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from team_agent.api import create_app
from team_agent.config import ApiKeyConfig, Settings
from team_agent.orchestrator import build_system
from team_agent.service import TaskService

ROOT = Path(__file__).resolve().parents[1]
EXECUTOR_KEY = "eval-executor-key-123"
ADMIN_KEY = "eval-admin-key-123"
BOB_KEY = "eval-bob-key-123"
KEYS = (
    ApiKeyConfig(EXECUTOR_KEY, "alice", "executor"),
    ApiKeyConfig(BOB_KEY, "bob", "executor"),
    ApiKeyConfig(ADMIN_KEY, "root", "admin"),
)

TERMINAL = {"completed", "partial", "failed"}


def headers(key: str = EXECUTOR_KEY) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def poll(client: TestClient, task_id: str, timeout: float = 240.0) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = client.get(f"/v1/tasks/{task_id}", headers=headers())
        assert response.status_code == 200
        payload = response.json()
        if payload["status"] in TERMINAL:
            return payload
        time.sleep(0.5)
    raise TimeoutError(task_id)


def run_case(client: TestClient, request: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    response = client.post("/v1/tasks", json={"request": request}, headers=headers())
    assert response.status_code == 202, response.text
    task_id = response.json()["task_id"]
    payload = poll(client, task_id)
    return payload, []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "evidence" / "real-model")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    run_id = datetime.now(UTC).strftime("run-%Y%m%d-%H%M%S")
    run_dir = args.output / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    db_path = run_dir / "eval.db"
    settings = replace(
        settings,
        db_path=db_path,
        api_keys=KEYS,
        max_total_calls=25,
        max_calls_per_task=12,
        revision_limit=0,
        max_concurrent_tasks=2,
        max_requests_per_minute=100,
        max_input_chars=2000,
    )
    system = build_system(settings)
    repository = system.repository
    service = TaskService(settings, system=system, repository=repository)
    app = create_app(settings, service=service)
    results: list[dict[str, Any]] = []
    auth_checks: dict[str, int] = {}
    total_calls_at_end = 0

    with TestClient(app) as client:
        auth_checks["no_token"] = client.get("/v1/tasks").status_code
        auth_checks["wrong_token"] = client.get("/v1/tasks", headers=headers("wrong")).status_code
        auth_checks["executor_admin_api"] = client.get("/v1/admin/tasks", headers=headers()).status_code
        auth_checks["missing_task"] = client.get("/v1/tasks/T7-doesnotexist", headers=headers()).status_code

        cases = [
            ("R1", "库存直查", "现在 ThinkPad T14 Gen5 还有几台可用？"),
            ("R2", "工单直查", "工单 INC-2026-0001 现在是什么状态、谁在处理？"),
            (
                "R3",
                "新员工综合研判",
                "下周有一名研发岗新员工入职，请结合电脑库存、设备领用规定和现有工单，判断目前能否满足需求。",
            ),
        ]
        first_task_id = None
        for case_id, title, request in cases:
            payload, _ = run_case(client, request)
            if first_task_id is None:
                first_task_id = payload["task_id"]
            runs = repository.list_runs(payload["task_id"])
            final = payload.get("result") or {}
            tools = sorted({item["tool"] for item in final.get("evidence", [])})
            results.append(
                {
                    "id": case_id,
                    "title": title,
                    "request": request,
                    "status": payload["status"],
                    "audit_verdict": final.get("audit_verdict"),
                    "policy_unavailable": final.get("policy_unavailable"),
                    "tools": tools,
                    "models": sorted({run["model"] for run in runs if run["status"] == "ok"}),
                    "calls": len(runs),
                    "tokens": payload["total_tokens"],
                    "runs": runs,
                    "final": final,
                    "error_code": payload["error_code"],
                }
            )
        if first_task_id:
            auth_checks["cross_user"] = client.get(f"/v1/tasks/{first_task_id}", headers=headers(BOB_KEY)).status_code

    # 故障用例：Policy 模型不存在，使用共享预算与同一数据库
    bad_settings = replace(settings, policy=replace(settings.policy, model="model-does-not-exist-xyz"))
    bad_system = build_system(bad_settings)
    bad_system.budget = system.budget
    bad_service = TaskService(bad_settings, system=bad_system, repository=repository)
    bad_app = create_app(bad_settings, service=bad_service)
    with TestClient(bad_app) as client:
        payload, _ = run_case(client, "新员工电脑领用需要哪些条件？")
        runs = repository.list_runs(payload["task_id"])
        final = payload.get("result") or {}
        results.append(
            {
                "id": "R4",
                "title": "Policy 模型故障降级",
                "request": "新员工电脑领用需要哪些条件？",
                "status": payload["status"],
                "audit_verdict": final.get("audit_verdict"),
                "policy_unavailable": final.get("policy_unavailable"),
                "policy_summary": final.get("policy_summary"),
                "tools": sorted({item["tool"] for item in final.get("evidence", [])}),
                "models": sorted({run["model"] for run in runs if run["status"] == "ok"}),
                "calls": len(runs),
                "tokens": payload["total_tokens"],
                "runs": runs,
                "final": final,
                "error_code": payload["error_code"],
            }
        )

    total_calls_at_end = system.budget.total_calls
    failures: list[str] = []
    if auth_checks.get("no_token") != 401:
        failures.append("未认证访问未返回 401")
    if auth_checks.get("wrong_token") != 401:
        failures.append("错误密钥未返回 401")
    if auth_checks.get("executor_admin_api") != 403:
        failures.append("executor 访问管理员接口未返回 403")
    if auth_checks.get("cross_user") != 404:
        failures.append("跨用户任务未返回 404")
    if total_calls_at_end > 25:
        failures.append(f"真实调用 {total_calls_at_end} 超过 25")

    by_id = {item["id"]: item for item in results}
    if by_id["R1"]["status"] != "completed":
        failures.append("R1 未完成")
    if by_id["R2"]["status"] != "completed":
        failures.append("R2 未完成")
    if "T14" not in json.dumps(by_id["R1"]["final"], ensure_ascii=False):
        failures.append("R1 未返回 T14 库存事实")
    if by_id["R3"]["status"] not in {"partial", "completed"}:
        failures.append("R3 状态异常")
    if "auditor" not in {run["agent"] for run in by_id["R3"]["runs"]}:
        failures.append("R3 未执行审核")
    if by_id["R4"]["policy_unavailable"] is not True:
        failures.append("R4 未标记制度结论不可用")
    if by_id["R4"]["status"] not in {"partial", "failed"}:
        failures.append("R4 未降级")
    if by_id["R4"]["policy_summary"] is not None:
        failures.append("R4 仍返回制度结论")
    for item in results:
        if item["id"] == "R3" and not set(item["tools"]) <= {
            "query_inventory",
            "get_asset",
            "get_ticket",
            "list_tickets",
            "search_policies",
        }:
            failures.append("R3 出现未知工具")

    config = {
        "run_id": run_id,
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "models": {
            "planner": settings.planner.model,
            "business": settings.business.model,
            "policy": settings.policy.model,
            "auditor": settings.auditor.model,
        },
        "max_total_calls": settings.max_total_calls,
        "total_calls": total_calls_at_end,
        "api_auth_checks": auth_checks,
        "cases": len(results),
    }
    (run_dir / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "result.json").write_text(
        json.dumps({"config": config, "results": results, "failures": failures}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    lines = [
        "# 真实模型安全评估",
        "",
        f"- 运行：`{run_id}`，模型调用 {total_calls_at_end}/25",
        f"- 模型：{config['models']}",
        f"- 认证检查：{auth_checks}",
        "",
        "| 用例 | 状态 | 审核 | 工具 | 调用 |",
        "|---|---|---|---|---|",
    ]
    for item in results:
        tools_text = ", ".join(item["tools"]) or "-"
        lines.append(f"| {item['id']} | {item['status']} | {item['audit_verdict']} | {tools_text} | {item['calls']} |")
    lines += ["", "失败：" + ("无" if not failures else "；".join(failures)), ""]
    (run_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"调用 {total_calls_at_end}/25，用例 {len(results)}，失败：{failures or '无'}")
    print(f"证据：{run_dir}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
