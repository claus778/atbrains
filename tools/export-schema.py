#!/usr/bin/env python3
"""从活库导出 atbrains 结构 DDL（表 / 视图 / 触发器），用于全新部署与审阅。

为什么不用 mysqldump：它会把视图写成 /*!50001 ... */ 条件注释、带 DEFINER、带 AUTO_INCREMENT=N，
落到文件里既难读又会在别的账号/机器上导入失败。这里逐对象 SHOW CREATE，只保留结构与索引。

用法：
  python3 /root/atbrains/tools/export-schema.py > /root/atbrains/setup/schema-v9.sql
"""
from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.environ.get("ATBRAINS_PLUGIN_DIR") or "/root/.hermes/plugins/atbrains-memory")
from core import AtbrainsStore, resolve_config  # noqa: E402

DB = os.environ.get("ATBRAINS_DB") or "agent_memory"


def clean(ddl: str) -> str:
    ddl = re.sub(r"\s*AUTO_INCREMENT=\d+", "", ddl)
    ddl = re.sub(r"DEFINER=`[^`]*`@`[^`]*`\s*", "", ddl)
    ddl = ddl.replace("SQL SECURITY DEFINER", "SQL SECURITY INVOKER")
    return ddl.rstrip().rstrip(";") + ";"


def main() -> int:
    st = AtbrainsStore(resolve_config(os.environ.get("HERMES_HOME") or "/root/.hermes"))
    conn = st._conn()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT TABLE_NAME, TABLE_TYPE FROM information_schema.tables "
            "WHERE TABLE_SCHEMA=%s ORDER BY TABLE_TYPE, TABLE_NAME", (DB,))
        objs = [(r["TABLE_NAME"] if isinstance(r, dict) else r[0],
                 r["TABLE_TYPE"] if isinstance(r, dict) else r[1]) for r in cur.fetchall()]
        tables = [n for n, t in objs if t == "BASE TABLE"]
        views = [n for n, t in objs if t == "VIEW"]
        cur.execute(
            "SELECT TRIGGER_NAME, ACTION_TIMING, EVENT_MANIPULATION, EVENT_OBJECT_TABLE, ACTION_STATEMENT "
            "FROM information_schema.triggers WHERE TRIGGER_SCHEMA=%s ORDER BY EVENT_OBJECT_TABLE, TRIGGER_NAME",
            (DB,))
        triggers = cur.fetchall()

    out: list[str] = []
    out.append("-- atbrains 记忆库结构 v9 —— 表 / 视图 / 触发器")
    out.append(f"-- 自动导出（tools/export-schema.py），导出时间 {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%SZ}")
    out.append("-- 不含数据、不含 AUTO_INCREMENT 计数、已剥离 DEFINER（任何账号都能导入）")
    out.append("-- 用法：先建库，再导入本文件 ——")
    out.append("--   CREATE DATABASE agent_memory CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;")
    out.append("--   mariadb -h127.0.0.1 -P57306 -uagent -p agent_memory < schema-v9.sql")
    out.append("-- 注：插件 ensure_schema() 也会幂等建出同样的结构；本文件用于审阅/离线重建。")
    out.append("")
    out.append("SET NAMES utf8mb4;")
    out.append(f"USE `{DB}`;")
    out.append("")

    out.append("-- ============================ 表 ============================")
    for name in tables:
        with conn.cursor() as cur:
            cur.execute(f"SHOW CREATE TABLE `{name}`")
            row = cur.fetchone()
            ddl = row[1] if isinstance(row, (list, tuple)) else row["Create Table"]
        ddl = clean(ddl).replace("CREATE TABLE `", "CREATE TABLE IF NOT EXISTS `", 1)
        out.append(f"\n-- ---- {name} ----")
        out.append(ddl)

    out.append("\n\n-- ============================ 视图 ============================")
    for name in views:
        with conn.cursor() as cur:
            cur.execute(f"SHOW CREATE VIEW `{name}`")
            row = cur.fetchone()
            ddl = row[1] if isinstance(row, (list, tuple)) else row["Create View"]
        out.append(f"\n-- ---- {name} ----")
        out.append("DROP VIEW IF EXISTS `%s`;" % name)
        out.append(clean(ddl).replace("CREATE ALGORITHM", "CREATE OR REPLACE ALGORITHM", 1))

    out.append("\n\n-- ========================== 触发器 ==========================")
    out.append("-- platform / author 规范化 + 向量时间戳兜底；导入顺序在表之后。")
    for t in triggers:
        if isinstance(t, dict):
            name, timing, event, table, stmt = (t["TRIGGER_NAME"], t["ACTION_TIMING"],
                                                t["EVENT_MANIPULATION"], t["EVENT_OBJECT_TABLE"],
                                                t["ACTION_STATEMENT"])
        else:
            name, timing, event, table, stmt = t
        out.append(f"\nDROP TRIGGER IF EXISTS `{name}`;")
        out.append(f"DELIMITER $$")
        out.append(f"CREATE TRIGGER `{name}` {timing} {event} ON `{table}` FOR EACH ROW {stmt.rstrip().rstrip(';')}$$")
        out.append("DELIMITER ;")

    print("\n".join(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
