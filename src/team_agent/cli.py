"""多 Agent 协作演示 CLI。"""

from __future__ import annotations

import argparse
import json

from . import __version__
from .config import Settings
from .errors import AgentError, ConfigError
from .orchestrator import build_system, get_task_detail
from .repository import Repository, TaskNotFound

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_PARTIAL = 2
EXIT_CONFIG = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="secure-agent", description="企业 AI Agent 安全加固与受控部署")
    parser.add_argument("--version", action="version", version=f"secure-agent-runtime {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    run = subparsers.add_parser("run", help="执行一次联合研判")
    run.add_argument("request", help="用户请求")
    run.add_argument("--json", action="store_true")

    task = subparsers.add_parser("task", help="查看任务、子任务、交接与审核记录")
    task.add_argument("task_id")
    task.add_argument("--json", action="store_true")

    tasks = subparsers.add_parser("tasks", help="列出任务")
    tasks.add_argument("--limit", type=int, default=20)

    models = subparsers.add_parser("models", help="查看 Agent 与模型的路由关系")
    models.add_argument("--json", action="store_true")

    serve = subparsers.add_parser("serve", help="启动受控 HTTP API")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    return parser


def _settings() -> Settings:
    return Settings.from_env()


def _cmd_models(as_json: bool) -> int:
    settings = Settings.from_env(require_keys=False)
    rows = []
    for role in ("planner", "business", "policy", "auditor"):
        config = getattr(settings, role)
        if config is None:
            continue
        rows.append(
            {
                "role": role,
                "provider": config.provider,
                "model": config.model,
                "api_base": config.api_base,
                "temperature": config.temperature,
                "min_interval_seconds": config.min_interval_seconds,
            }
        )
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        for row in rows:
            print(f"{row['role']:<10} {row['provider']:<8} {row['model']:<18} {row['api_base']}")
    return EXIT_OK


def _cmd_run(request: str, as_json: bool) -> int:
    system = build_system(_settings())
    try:
        task_id = system.run(request)
    finally:
        system.client.close()
    detail = get_task_detail(system.repository, task_id)
    if as_json:
        print(json.dumps(detail, ensure_ascii=False, indent=2))
    else:
        _print_task(detail)
    status = detail["task"]["status"]
    return {"completed": EXIT_OK, "partial": EXIT_PARTIAL, "failed": EXIT_FAILED}.get(status, EXIT_FAILED)


def _cmd_task(task_id: str, as_json: bool) -> int:
    settings = Settings.from_env(require_keys=False)
    repository = Repository(settings.db_path)
    detail = get_task_detail(repository, task_id)
    if as_json:
        print(json.dumps(detail, ensure_ascii=False, indent=2))
    else:
        _print_task(detail)
    return EXIT_OK


def _cmd_tasks(limit: int) -> int:
    settings = Settings.from_env(require_keys=False)
    repository = Repository(settings.db_path)
    for task in repository.list_tasks(limit):
        print(f"{task['id']}  {task['status']:<10} {task['created_at']}  {task['user_input'][:60]}")
    return EXIT_OK


def _print_task(detail: dict) -> None:
    task = detail["task"]
    print(f"任务 {task['id']}  状态：{task['status']}")
    print(f"请求：{task['user_input']}")
    final = json.loads(task["final_json"]) if task["final_json"] else None
    if final:
        print(f"结论：{final.get('conclusion')}")
    for subtask in detail["subtasks"]:
        print(f"  - [{subtask['agent']:<8}] {subtask['status']:<9} {subtask['objective'][:60]}")
    print("模型调用：")
    for run in detail["runs"]:
        usage = f"{run['prompt_tokens']}+{run['completion_tokens']} tok" if run["prompt_tokens"] else ""
        print(f"  - {run['agent']:<8} {run['model']:<18} {run['phase']:<18} {run['status']} {usage}")
    audits = detail["audits"]
    if audits:
        latest = json.loads(audits[-1]["output_json"])
        print(f"审核：{latest['verdict']} — {latest['summary']}")
        for finding in latest.get("findings", []):
            print(f"  ! {finding['level']}/{finding['type']}: {finding['description']}")
    if final:
        for conflict in final.get("conflicts", []):
            print(f"冲突：{conflict.get('description')}")
        for question in final.get("open_questions", []):
            print(f"待确认：{question}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "models":
            return _cmd_models(args.json)
        if args.command == "run":
            return _cmd_run(args.request, args.json)
        if args.command == "task":
            return _cmd_task(args.task_id, args.json)
        if args.command == "tasks":
            return _cmd_tasks(args.limit)
        if args.command == "serve":
            import uvicorn

            from .api import create_app

            settings = _settings()
            uvicorn.run(
                create_app(settings),
                host=args.host or settings.host,
                port=args.port or settings.port,
                log_config=None,
                access_log=False,
            )
            return EXIT_OK
    except TaskNotFound as exc:
        print(f"错误：{exc}")
        return EXIT_CONFIG
    except (ConfigError, AgentError) as exc:
        print(f"错误：{exc}")
        return EXIT_CONFIG
    parser.print_help()
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
