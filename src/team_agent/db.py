"""SQLite 初始化与虚构种子数据导入。

业务数据（员工/资产/工单）落在同一个库里；Agent 自身的任务与事件表在 M2 加入。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from pydantic import ValidationError

from .domain import Asset, Employee, Ticket
from .errors import SeedDataError

SCHEMA = """
CREATE TABLE IF NOT EXISTS employees (
    employee_id TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    department  TEXT NOT NULL,
    title       TEXT NOT NULL,
    email       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assets (
    asset_id    TEXT PRIMARY KEY,
    category    TEXT NOT NULL,
    model       TEXT NOT NULL,
    status      TEXT NOT NULL,
    assignee_id TEXT REFERENCES employees(employee_id),
    location    TEXT NOT NULL,
    purchased_at TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_assets_category ON assets (category, status)
;

CREATE TABLE IF NOT EXISTS tickets (
    ticket_id    TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    description  TEXT NOT NULL,
    category     TEXT NOT NULL,
    priority     TEXT NOT NULL,
    status       TEXT NOT NULL,
    requester_id TEXT NOT NULL REFERENCES employees(employee_id),
    assignee_id  TEXT REFERENCES employees(employee_id),
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    comments_json TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets (status, priority);

CREATE TABLE IF NOT EXISTS tasks (
    task_id         TEXT PRIMARY KEY,
    status          TEXT NOT NULL CHECK (status IN (
                        'running', 'waiting_for_input', 'waiting_for_approval',
                        'completed', 'failed', 'limit_reached')),
    user_input      TEXT NOT NULL,
    final_answer    TEXT,
    error_code      TEXT,
    error_message   TEXT,
    pending_question TEXT,
    step_count      INTEGER NOT NULL DEFAULT 0,
    tool_call_count INTEGER NOT NULL DEFAULT 0,
    model           TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL,
    role            TEXT NOT NULL,
    content         TEXT,
    tool_calls_json TEXT,
    tool_call_id    TEXT,
    name            TEXT,
    created_at      TEXT NOT NULL,
    UNIQUE (task_id, seq)
);

CREATE TABLE IF NOT EXISTS agent_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    seq          INTEGER NOT NULL,
    type         TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL,
    UNIQUE (task_id, seq)
);

CREATE TABLE IF NOT EXISTS tool_calls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    call_id     TEXT NOT NULL,
    tool_name   TEXT NOT NULL,
    args_json   TEXT NOT NULL,
    result_json TEXT NOT NULL,
    ok          INTEGER NOT NULL CHECK (ok IN (0, 1)),
    error_code  TEXT,
    created_at  TEXT NOT NULL,
    UNIQUE (task_id, call_id)
);

CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY,
    task_id     TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    call_id     TEXT NOT NULL,
    tool_name   TEXT NOT NULL,
    args_json   TEXT NOT NULL,
    args_hash   TEXT NOT NULL,
    summary     TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected', 'executed')),
    created_at  TEXT NOT NULL,
    decided_at  TEXT,
    decided_by  TEXT,
    executed_at TEXT,
    result_json TEXT,
    UNIQUE (task_id, tool_name, args_hash)
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks (status, created_at);
CREATE INDEX IF NOT EXISTS idx_approvals_task ON approvals (task_id, status);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    db_path = str(path)
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(SCHEMA)
    return connection


def _load_json(path: Path) -> list[dict]:
    if not path.is_file():
        raise SeedDataError(f"缺少种子数据文件：{path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SeedDataError(f"{path.name} 不是合法 JSON：{exc}") from exc
    if not isinstance(payload, list):
        raise SeedDataError(f"{path.name} 顶层必须是数组")
    return payload


def _validate(models: type, rows: list[dict], source: str) -> list:
    parsed = []
    for index, row in enumerate(rows, start=1):
        try:
            parsed.append(models.model_validate(row))
        except ValidationError as exc:
            raise SeedDataError(f"{source} 第 {index} 条不合法：{exc.errors()[0]['msg']}") from exc
    return parsed


def seed_database(connection: sqlite3.Connection, seed_dir: Path | str) -> None:
    """幂等导入：每张表为空时才写入，避免覆盖运行期新增的工单。"""

    root = Path(seed_dir)
    if not root.is_dir():
        raise SeedDataError(f"种子数据目录不存在：{root}")

    if connection.execute("SELECT COUNT(*) FROM employees").fetchone()[0] == 0:
        employees: list[Employee] = _validate(Employee, _load_json(root / "employees.json"), "employees.json")
        connection.executemany(
            "INSERT INTO employees (employee_id, name, department, title, email) VALUES (?, ?, ?, ?, ?)",
            [(e.employee_id, e.name, e.department, e.title, e.email) for e in employees],
        )

    if connection.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 0:
        assets: list[Asset] = _validate(Asset, _load_json(root / "assets.json"), "assets.json")
        connection.executemany(
            "INSERT INTO assets (asset_id, category, model, status, assignee_id, location, purchased_at, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (a.asset_id, a.category, a.model, a.status.value, a.assignee_id, a.location, a.purchased_at, a.note)
                for a in assets
            ],
        )

    if connection.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 0:
        tickets: list[Ticket] = _validate(Ticket, _load_json(root / "tickets.json"), "tickets.json")
        connection.executemany(
            "INSERT INTO tickets (ticket_id, title, description, category, priority, status, requester_id, "
            "assignee_id, created_at, updated_at, comments_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    t.ticket_id,
                    t.title,
                    t.description,
                    t.category.value,
                    t.priority.value,
                    t.status.value,
                    t.requester_id,
                    t.assignee_id,
                    t.created_at,
                    t.updated_at,
                    json.dumps([c.model_dump() for c in t.comments], ensure_ascii=False),
                )
                for t in tickets
            ],
        )

    connection.commit()


def init_database(db_path: Path | str, seed_dir: Path | str) -> None:
    connection = connect(db_path)
    try:
        seed_database(connection, seed_dir)
    finally:
        connection.close()
