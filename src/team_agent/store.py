"""业务数据访问：资产、工单、员工。

所有工具只能通过这里读写，不允许拼接 SQL、访问文件路径或执行系统命令。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .db import connect
from .domain import Priority, TicketCategory, TicketStatus, now_iso
from .errors import AgentError


class StoreError(AgentError):
    pass


class NotFound(StoreError):
    pass


class Conflict(StoreError):
    pass


class DeskStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)

    def _connect(self) -> sqlite3.Connection:
        return connect(self.db_path)

    # ---------------------------------------------------------------- 员工

    @staticmethod
    def _resolve_employee(connection: sqlite3.Connection, value: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM employees WHERE employee_id = ?", (value.strip(),)).fetchone()
        if row is not None:
            return row
        rows = connection.execute("SELECT * FROM employees WHERE name = ?", (value.strip(),)).fetchall()
        if len(rows) == 1:
            return rows[0]
        if len(rows) > 1:
            raise Conflict(f"员工姓名 {value!r} 对应多个工号，请使用员工编号")
        raise NotFound(f"员工不存在：{value}")

    # ---------------------------------------------------------------- 资产

    def inventory(self, category: str | None = None, only_available: bool = True) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT category, model,
                       COUNT(*) AS total,
                       SUM(CASE WHEN status = 'in_stock' THEN 1 ELSE 0 END) AS available,
                       SUM(CASE WHEN status = 'in_use' THEN 1 ELSE 0 END) AS in_use,
                       SUM(CASE WHEN status = 'repair' THEN 1 ELSE 0 END) AS repair,
                       SUM(CASE WHEN status = 'retired' THEN 1 ELSE 0 END) AS retired
                  FROM assets
                 WHERE (? IS NULL OR category = ?)
                 GROUP BY category, model
                 ORDER BY category, model
                """,
                (category, category),
            ).fetchall()
        finally:
            connection.close()

        result = [
            {
                "category": row["category"],
                "model": row["model"],
                "total": row["total"],
                "available": row["available"],
                "in_use": row["in_use"],
                "repair": row["repair"],
                "retired": row["retired"],
            }
            for row in rows
        ]
        if only_available:
            result = [item for item in result if item["available"] > 0]
        return result

    def get_asset(self, *, asset_id: str | None = None, assignee: str | None = None) -> dict[str, Any] | list[dict]:
        connection = self._connect()
        try:
            if asset_id is not None:
                row = connection.execute("SELECT * FROM assets WHERE asset_id = ?", (asset_id,)).fetchone()
                if row is None:
                    raise NotFound(f"设备不存在：{asset_id}")
                return self._asset_dict(connection, row)

            assert assignee is not None
            employee = self._resolve_employee(connection, assignee)
            rows = connection.execute(
                "SELECT * FROM assets WHERE assignee_id = ? ORDER BY asset_id", (employee["employee_id"],)
            ).fetchall()
            return [self._asset_dict(connection, row) for row in rows]
        finally:
            connection.close()

    @staticmethod
    def _asset_dict(connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        assignee_name = None
        if row["assignee_id"]:
            employee = connection.execute(
                "SELECT name FROM employees WHERE employee_id = ?", (row["assignee_id"],)
            ).fetchone()
            assignee_name = employee["name"] if employee else None
        return {
            "asset_id": row["asset_id"],
            "category": row["category"],
            "model": row["model"],
            "status": row["status"],
            "assignee_id": row["assignee_id"],
            "assignee_name": assignee_name,
            "location": row["location"],
            "purchased_at": row["purchased_at"],
            "note": row["note"],
        }

    # ---------------------------------------------------------------- 工单

    def get_ticket(self, ticket_id: str) -> dict[str, Any]:
        connection = self._connect()
        try:
            row = connection.execute("SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)).fetchone()
            if row is None:
                raise NotFound(f"工单不存在：{ticket_id}")
            return self._ticket_dict(connection, row)
        finally:
            connection.close()

    def list_tickets(
        self,
        *,
        status: TicketStatus | str | None = None,
        assignee: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            clauses: list[str] = []
            params: list[Any] = []
            if status is not None:
                clauses.append("status = ?")
                params.append(TicketStatus(status).value)
            if assignee is not None:
                employee = self._resolve_employee(connection, assignee)
                clauses.append("assignee_id = ?")
                params.append(employee["employee_id"])
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            rows = connection.execute(
                f"SELECT * FROM tickets {where} ORDER BY created_at DESC, ticket_id DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
            return [self._ticket_dict(connection, row) for row in rows]
        finally:
            connection.close()

    def create_ticket(
        self,
        *,
        title: str,
        description: str,
        category: TicketCategory | str,
        priority: Priority | str,
        requester_id: str,
    ) -> dict[str, Any]:
        connection = self._connect()
        try:
            requester = self._resolve_employee(connection, requester_id)
            now = now_iso()
            ticket_id = self._next_ticket_id(connection)
            connection.execute(
                "INSERT INTO tickets (ticket_id, title, description, category, priority, status, requester_id, "
                "assignee_id, created_at, updated_at, comments_json) "
                "VALUES (?, ?, ?, ?, ?, 'open', ?, NULL, ?, ?, '[]')",
                (
                    ticket_id,
                    title.strip(),
                    description.strip(),
                    TicketCategory(category).value,
                    Priority(priority).value,
                    requester["employee_id"],
                    now,
                    now,
                ),
            )
            connection.commit()
            row = connection.execute("SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)).fetchone()
            return self._ticket_dict(connection, row)
        finally:
            connection.close()

    def update_ticket(
        self,
        *,
        ticket_id: str,
        status: TicketStatus | str | None = None,
        priority: Priority | str | None = None,
        assignee: str | None = None,
        comment: str | None = None,
        author_id: str = "EMP-1011",
    ) -> dict[str, Any]:
        connection = self._connect()
        try:
            row = connection.execute("SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)).fetchone()
            if row is None:
                raise NotFound(f"工单不存在：{ticket_id}")

            updates: list[str] = []
            params: list[Any] = []
            if status is not None:
                updates.append("status = ?")
                params.append(TicketStatus(status).value)
            if priority is not None:
                updates.append("priority = ?")
                params.append(Priority(priority).value)
            if assignee is not None:
                employee = self._resolve_employee(connection, assignee)
                updates.append("assignee_id = ?")
                params.append(employee["employee_id"])

            comments = json.loads(row["comments_json"])
            if comment:
                author = self._resolve_employee(connection, author_id)
                comments.append({"author_id": author["employee_id"], "at": now_iso(), "text": comment.strip()})
                updates.append("comments_json = ?")
                params.append(json.dumps(comments, ensure_ascii=False))

            updates.append("updated_at = ?")
            params.append(now_iso())
            params.append(ticket_id)
            connection.execute(f"UPDATE tickets SET {', '.join(updates)} WHERE ticket_id = ?", params)
            connection.commit()
            updated = connection.execute("SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)).fetchone()
            return self._ticket_dict(connection, updated)
        finally:
            connection.close()

    @staticmethod
    def _next_ticket_id(connection: sqlite3.Connection) -> str:
        row = connection.execute(
            "SELECT ticket_id FROM tickets WHERE ticket_id LIKE 'INC-2026-%' ORDER BY ticket_id DESC LIMIT 1"
        ).fetchone()
        sequence = int(row["ticket_id"].rsplit("-", 1)[1]) + 1 if row else 1
        return f"INC-2026-{sequence:04d}"

    @staticmethod
    def _ticket_dict(connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        def name_of(employee_id: str | None) -> str | None:
            if not employee_id:
                return None
            employee = connection.execute("SELECT name FROM employees WHERE employee_id = ?", (employee_id,)).fetchone()
            return employee["name"] if employee else None

        return {
            "ticket_id": row["ticket_id"],
            "title": row["title"],
            "description": row["description"],
            "category": row["category"],
            "priority": row["priority"],
            "status": row["status"],
            "requester_id": row["requester_id"],
            "requester_name": name_of(row["requester_id"]),
            "assignee_id": row["assignee_id"],
            "assignee_name": name_of(row["assignee_id"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "comments": json.loads(row["comments_json"]),
        }
