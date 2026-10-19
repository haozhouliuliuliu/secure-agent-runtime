"""数据库备份/恢复/清理命令行。

示例：
    python -m team_agent.backup_cli backup --db data/collab.db --out backups/collab.db
    python -m team_agent.backup_cli verify --db backups/collab.db
    python -m team_agent.backup_cli restore --backup backups/collab.db --db data/collab.db
    python -m team_agent.backup_cli purge --db data/collab.db --days 30
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .backup import backup_database, restore_database, verify_database
from .repository import Repository


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="secure-agent-backup")
    sub = parser.add_subparsers(dest="command", required=True)

    backup = sub.add_parser("backup")
    backup.add_argument("--db", type=Path, required=True)
    backup.add_argument("--out", type=Path, required=True)

    verify = sub.add_parser("verify")
    verify.add_argument("--db", type=Path, required=True)

    restore = sub.add_parser("restore")
    restore.add_argument("--backup", type=Path, required=True)
    restore.add_argument("--db", type=Path, required=True)

    purge = sub.add_parser("purge")
    purge.add_argument("--db", type=Path, required=True)
    purge.add_argument("--days", type=int, required=True)

    args = parser.parse_args(argv)
    if args.command == "backup":
        path = backup_database(args.db, args.out)
        print(f"backup: {path}")
        return 0
    if args.command == "verify":
        counts = verify_database(args.db)
        print(f"tables: {counts}")
        return 0
    if args.command == "restore":
        path = restore_database(args.backup, args.db)
        print(f"restored: {path}")
        return 0
    cutoff = (datetime.now(UTC) - timedelta(days=args.days)).isoformat(timespec="seconds")
    deleted = Repository(args.db).purge_tasks(cutoff)
    print(f"purged: {deleted}")
    return 0
