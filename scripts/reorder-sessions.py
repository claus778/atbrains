#!/usr/bin/env python3
"""把 brain_sessions 重建为「按时间顺序」的物理表。

现状：主键是 session_id（varchar），InnoDB 按主键聚簇 → 全表扫描是字典序，
      local-… 和 2026… 混在一起，看起来时间线乱。
目标：新增自增 id 作主键，id 严格按 started_at 递增（1..N，无空洞），session_id 改唯一键。
      插件用 ON DUPLICATE KEY UPDATE 依赖唯一键，不依赖主键形态，改完照常工作。

用法：
  python3 reorder-sessions.py --dry-run
  python3 reorder-sessions.py --apply
"""
from __future__ import annotations

import argparse
import json
import sys
import time

DDL_NEW = """CREATE TABLE brain_sessions_new (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    session_id VARCHAR(96) NOT NULL,
    platform VARCHAR(48) NOT NULL DEFAULT '',
    machine VARCHAR(48) NOT NULL DEFAULT '',
    started_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_active DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    turn_count INT NOT NULL DEFAULT 0,
    archived_messages INT UNSIGNED NOT NULL DEFAULT 0,
    note VARCHAR(512) NOT NULL DEFAULT '',
    PRIMARY KEY (id),
    UNIQUE KEY uniq_session_id (session_id),
    KEY idx_sessions_time (started_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--hermes-home", default="/root/.hermes")
    ap.add_argument("--plugin-dir", default="/root/.hermes/plugins/brain")
    args = ap.parse_args()
    write = bool(args.apply) and not args.dry_run

    sys.path.insert(0, args.plugin_dir)
    from core import BrainStore, resolve_config  # noqa: PLC0415

    store = BrainStore(resolve_config(args.hermes_home))
    cur = store._conn().cursor()

    cur.execute("SELECT COUNT(*) AS n, MIN(started_at) AS mn, MAX(started_at) AS mx FROM brain_sessions")
    before = cur.fetchone()
    print(f"现有 {before['n']} 行 | {before['mn']} → {before['mx']}")

    cur.execute("SHOW COLUMNS FROM brain_sessions LIKE 'id'")
    if cur.fetchone():
        print("已经有 id 列，跳过（无需重建）")
        return 0
    if not write:
        print("将新建 brain_sessions_new，按 started_at 赋 id 1..N，再原子 RENAME。")
        print("（dry-run，未写入；加 --apply 执行）")
        return 0

    cur.execute("SELECT GET_LOCK('brain_sessions_reorder', 30) AS got")
    if not (cur.fetchone() or {}).get("got"):
        print("拿不到 GET_LOCK('brain_sessions_reorder')，有别的进程在动这张表，退出")
        return 1
    try:
        cur.execute("DROP TABLE IF EXISTS brain_sessions_new")
        cur.execute(DDL_NEW)
        cur.execute(
            "INSERT INTO brain_sessions_new "
            "(id, session_id, platform, machine, started_at, last_active, turn_count, archived_messages, note) "
            "SELECT ROW_NUMBER() OVER (ORDER BY started_at, session_id) AS id, "
            "session_id, platform, machine, started_at, last_active, turn_count, archived_messages, note "
            "FROM brain_sessions ORDER BY started_at, session_id")
        cur.execute("SELECT COUNT(*) AS n, MIN(id) AS mn, MAX(id) AS mx, "
                    "COUNT(DISTINCT id) AS uniq FROM brain_sessions_new")
        new = {k: int(v) for k, v in (cur.fetchone() or {}).items()}
        print(f"新表 {new['n']} 行 | id {new['mn']}..{new['mx']} | 唯一 id {new['uniq']}")
        if new["n"] != before["n"] or new["mn"] != 1 or new["mx"] != new["n"]:
            raise SystemExit("校验失败：行数或 id 连续性不对，已中止（旧表未动）")
        cur.execute(
            "SELECT COUNT(*) AS c FROM brain_sessions_new n "
            "LEFT JOIN brain_sessions o ON o.session_id = n.session_id WHERE o.session_id IS NULL")
        if int(cur.fetchone()["c"]):
            raise SystemExit("校验失败：有 session_id 在旧表里找不到，已中止")
        bak = time.strftime("brain_sessions_bak_%Y%m%d_%H%M%S")
        cur.execute(f"RENAME TABLE brain_sessions TO {bak}, brain_sessions_new TO brain_sessions")
        print(f"已切换：旧表保留为 {bak}")
        cur.execute("SELECT COUNT(*) AS n, MIN(id) AS mn, MAX(id) AS mx FROM brain_sessions")
        after = cur.fetchone()
        print(f"新 brain_sessions：{after['n']} 行 | id {after['mn']}..{after['mx']}")
        cur.execute("SHOW CREATE TABLE brain_sessions")
        print(json.dumps(cur.fetchone(), ensure_ascii=False, default=str)[:400])
    finally:
        try:
            cur.execute("SELECT RELEASE_LOCK('brain_sessions_reorder')")
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
