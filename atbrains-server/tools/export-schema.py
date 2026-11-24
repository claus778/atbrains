#!/usr/bin/env python3
"""导出 atbrains 结构 DDL（离线从 schema.py 导出，或从活库 SHOW CREATE 导出）。

用法:
    python3 tools/export-schema.py --from-code > deploy/schema.sql
    python3 tools/export-schema.py --live > deploy/schema.sql
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER_DIR = ROOT
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from atbrains_server.config import resolve_config  # noqa: E402
from atbrains_server.schema import SCHEMA_VERSION, _DDL, _MIGRATIONS  # noqa: E402
from atbrains_server.store import AtbrainsStore  # noqa: E402


def clean(ddl: str) -> str:
    ddl = re.sub(r"\s*AUTO_INCREMENT=\d+", "", ddl)
    ddl = re.sub(r"DEFINER=`[^`]*`@`[^`]*`\s*", "", ddl)
    ddl = ddl.replace("SQL SECURITY DEFINER", "SQL SECURITY INVOKER")
    return ddl.rstrip().rstrip(";") + ";"


def export_from_code(db: str) -> str:
    out: list[str] = [
        f"-- atbrains 记忆库结构 v{SCHEMA_VERSION}（由 tools/export-schema.py --from-code 生成）",
        "SET NAMES utf8mb4;",
        f"CREATE DATABASE IF NOT EXISTS `{db}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;",
        f"USE `{db}`;",
        "",
        "-- ============================ 基础表 DDL ============================",
    ]
    for stmt in _DDL:
        out.append(stmt.rstrip().rstrip(";") + ";")
        out.append("")
    out.append("-- ============================ 增量迁移 ============================")
    for ver in sorted(_MIGRATIONS):
        desc, stmts = _MIGRATIONS[ver]
        out.append(f"-- v{ver}: {desc}")
        for s in stmts:
            out.append(s.rstrip().rstrip(";") + ";")
        out.append("")
    out.append(
        f"INSERT INTO brain_meta (meta_key, meta_value) VALUES ('schema_version', '{SCHEMA_VERSION}') "
        "ON DUPLICATE KEY UPDATE meta_value = VALUES(meta_value);"
    )
    return "\n".join(out)


def export_live(db: str) -> str:
    st = AtbrainsStore(resolve_config(), start_workers=False)
    conn = st._conn()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT TABLE_NAME, TABLE_TYPE FROM information_schema.tables "
            "WHERE TABLE_SCHEMA=%s ORDER BY TABLE_TYPE, TABLE_NAME",
            (db,),
        )
        objs = [(r["TABLE_NAME"], r["TABLE_TYPE"]) for r in cur.fetchall()]
        tables = [n for n, t in objs if t == "BASE TABLE"]
        views = [n for n, t in objs if t == "VIEW"]
        cur.execute(
            "SELECT TRIGGER_NAME, ACTION_TIMING, EVENT_MANIPULATION, EVENT_OBJECT_TABLE, ACTION_STATEMENT "
            "FROM information_schema.triggers WHERE TRIGGER_SCHEMA=%s ORDER BY EVENT_OBJECT_TABLE, TRIGGER_NAME",
            (db,),
        )
        triggers = cur.fetchall()

    out: list[str] = [
        f"-- atbrains 记忆库结构 v{SCHEMA_VERSION}（从活库导出）",
        f"-- 导出时间 {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%SZ}",
        "SET NAMES utf8mb4;",
        f"USE `{db}`;",
        "",
        "-- ============================ 表 ============================",
    ]
    for name in tables:
        with conn.cursor() as cur:
            cur.execute(f"SHOW CREATE TABLE `{name}`")
            ddl = cur.fetchone()["Create Table"]
        out.append(f"\n-- ---- {name} ----")
        out.append(clean(ddl).replace("CREATE TABLE `", "CREATE TABLE IF NOT EXISTS `", 1))

    out.append("\n\n-- ============================ 视图 ============================")
    for name in views:
        with conn.cursor() as cur:
            cur.execute(f"SHOW CREATE VIEW `{name}`")
            ddl = cur.fetchone()["Create View"]
        out.append(f"\n-- ---- {name} ----")
        out.append(f"DROP VIEW IF EXISTS `{name}`;")
        out.append(clean(ddl).replace("CREATE ALGORITHM", "CREATE OR REPLACE ALGORITHM", 1))

    out.append("\n\n-- ========================== 触发器 ==========================")
    for t in triggers:
        out.append(f"\nDROP TRIGGER IF EXISTS `{t['TRIGGER_NAME']}`;")
        out.append("DELIMITER $$")
        out.append(
            f"CREATE TRIGGER `{t['TRIGGER_NAME']}` {t['ACTION_TIMING']} {t['EVENT_MANIPULATION']} "
            f"ON `{t['EVENT_OBJECT_TABLE']}` FOR EACH ROW {t['ACTION_STATEMENT'].rstrip().rstrip(';')}$$"
        )
        out.append("DELIMITER ;")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-code", action="store_true", help="从 schema.py 离线生成 DDL+迁移 SQL")
    ap.add_argument("--live", action="store_true", help="从活库 SHOW CREATE 导出")
    ap.add_argument("--db", default=os.environ.get("ATBRAINS_DB") or "agent_memory")
    args = ap.parse_args()
    if args.live:
        print(export_live(args.db))
    else:
        print(export_from_code(args.db))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
