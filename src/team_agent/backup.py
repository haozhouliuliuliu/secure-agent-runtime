"""SQLite 备份与恢复（在线备份 API，不直接复制正在写入的库文件）。"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def backup_database(source: Path | str, destination: Path | str) -> Path:
    src = Path(source)
    dst = Path(destination)
    if not src.is_file():
        raise FileNotFoundError(f"数据库不存在：{src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    source_connection = sqlite3.connect(src)
    target_connection = sqlite3.connect(dst)
    try:
        source_connection.backup(target_connection)
    finally:
        target_connection.close()
        source_connection.close()
    return dst


def verify_database(path: Path | str) -> dict[str, int]:
    connection = sqlite3.connect(path)
    try:
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'collab_%'"
        ).fetchall()
        counts = {}
        for (name,) in tables:
            counts[name] = int(connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
        return counts
    finally:
        connection.close()


def restore_database(backup: Path | str, target: Path | str) -> Path:
    src = Path(backup)
    dst = Path(target)
    if not src.is_file():
        raise FileNotFoundError(f"备份不存在：{src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(src)
    target_connection = sqlite3.connect(dst)
    try:
        source_connection.backup(target_connection)
    finally:
        target_connection.close()
        source_connection.close()
    return dst
