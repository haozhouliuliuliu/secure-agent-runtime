"""业务数据模型（全部为虚构数据）。

这些模型同时用于：种子数据校验、数据库读出后的结构化、工具返回值。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class AssetStatus(StrEnum):
    IN_STOCK = "in_stock"
    IN_USE = "in_use"
    REPAIR = "repair"
    RETIRED = "retired"


class TicketStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    PENDING = "pending"
    RESOLVED = "resolved"
    CLOSED = "closed"


class TicketCategory(StrEnum):
    HARDWARE = "hardware"
    SOFTWARE = "software"
    NETWORK = "network"
    ACCOUNT = "account"
    OTHER = "other"


class Priority(StrEnum):
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"
    P4 = "P4"


class Employee(BaseModel):
    model_config = ConfigDict(frozen=True)

    employee_id: str
    name: str
    department: str
    title: str
    email: str


class Asset(BaseModel):
    model_config = ConfigDict(frozen=True)

    asset_id: str
    category: str
    model: str
    status: AssetStatus
    assignee_id: str | None = None
    location: str
    purchased_at: str
    note: str = ""


class TicketComment(BaseModel):
    model_config = ConfigDict(frozen=True)

    author_id: str
    at: str
    text: str


class Ticket(BaseModel):
    model_config = ConfigDict(frozen=True)

    ticket_id: str
    title: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=500)
    category: TicketCategory
    priority: Priority
    status: TicketStatus
    requester_id: str
    assignee_id: str | None = None
    created_at: str
    updated_at: str
    comments: tuple[TicketComment, ...] = ()


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
