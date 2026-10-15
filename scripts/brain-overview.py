#!/usr/bin/env python3
"""brain 全库总览 —— 所有章节一律「最新在前」。

用法:
  python3 brain-overview.py                  # 记忆全量(活跃) + 最近100轮对话 + 最近40个会话
  python3 brain-overview.py --out FILE       # 写入文件（如 /root/agent-db/brain-overview.txt）
  python3 brain-overview.py --turns 300 --sessions 50
  python3 brain-overview.py --all            # 记忆包含已停用（active=0，标 ·停用）
  python3 brain-overview.py --machine x260-debian   # 只看某一台机器的行

数据源: /root/.hermes/.env 的 BRAIN_MARIADB_PASSWORD，直连 MariaDB 127.0.0.1:57306。
排序恒为最新在前: 记忆 created_at DESC(id 兜底) / 对话 ts DESC(id 兜底) / 会话 last_active DESC。
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time

HERMES_HOME = os.environ.get("BRAIN_HERMES_HOME", "/root/.hermes")
HOST, PORT, USER, DB_NAME = "127.0.0.1", "57306", "agent", "hermes_agent_memory"


def _password() -> str:
    env_path = os.path.join(HERMES_HOME, ".env")
    try:
        for line in open(env_path, encoding="utf-8", errors="replace"):
            if line.startswith("BRAIN_MARIADB_PASSWORD"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError as exc:
        sys.exit(f"无法读取 {env_path}: {exc}")
    sys.exit(f"{env_path} 中未找到 BRAIN_MARIADB_PASSWORD")


def q(sql: str) -> list[str]:
    env = dict(os.environ, MYSQL_PWD=_password())
    proc = subprocess.run(
        ["mysql", "-h", HOST, "-P", PORT, "-u", USER, DB_NAME,
         "--default-character-set=utf8mb4", "-N", "-B", "-e", sql],
        capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        sys.exit(f"查询失败: {proc.stderr.strip()}")
    return [ln for ln in proc.stdout.splitlines() if ln]


def main() -> None:
    ap = argparse.ArgumentParser(description="brain 全库总览（最新在前）")
    ap.add_argument("--turns", type=int, default=100, help="对话条数（默认 100）")
    ap.add_argument("--sessions", type=int, default=40, help="会话条数（默认 40）")
    ap.add_argument("--all", action="store_true", help="记忆含已停用条目")
    ap.add_argument("--machine", default="", help="只看某台机器（如 server / x260-debian）")
    ap.add_argument("--out", default="", help="写入文件（缺省打印到屏幕）")
    args = ap.parse_args()

    cond_m = "1=1" if args.all else "active=1"
    if args.machine:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", args.machine):
            sys.exit("--machine 只允许字母/数字/._-")
        cond_m += f" AND machine='{args.machine}'"
    cond_t = "1=1"
    cond_s = "1=1"
    if args.machine:
        cond_t += f" AND machine='{args.machine}'"
        cond_s += f" AND machine='{args.machine}'"

    counts = q(
        "SELECT CONCAT("
        "(SELECT COUNT(*) FROM brain_memories WHERE active=1), ' / ',"
        "(SELECT COUNT(*) FROM brain_memories), ',' ,"
        "(SELECT COUNT(*) FROM brain_turns), ',',"
        "(SELECT COUNT(*) FROM brain_sessions), ',',"
        "(SELECT COUNT(*) FROM brain_archive))")[0]
    parts = counts.split(",")
    act, total = parts[0].split(" / ")
    turns_n, sess_n, arch_n = parts[1], parts[2], parts[3]

    mems = q(
        "SELECT CONCAT('[', id, '] ', DATE_FORMAT(created_at,'%m-%d %H:%i'), ' ',"
        " COALESCE(NULLIF(machine,''),'?'), ' ', kind, '·', importance,"
        " IF(active=0,' ·停用',''), IF(tags<>'', CONCAT(' #', tags), ''), ' | ',"
        " LEFT(REPLACE(REPLACE(content, CHAR(10), ' '), CHAR(9), ' '), 160))"
        f" FROM brain_memories WHERE {cond_m}"
        " ORDER BY created_at DESC, id DESC")
    turns = q(
        "SELECT CONCAT(DATE_FORMAT(ts,'%m-%d %H:%i'), ' [', COALESCE(NULLIF(machine,''),'?'), '] ',"
        " session_id, ' | ', LEFT(REPLACE(REPLACE(user_content, CHAR(10), ' '), CHAR(9), ' '), 110))"
        f" FROM brain_turns WHERE {cond_t}"
        " ORDER BY ts DESC, id DESC"
        f" LIMIT {max(0, int(args.turns))}")
    sess = q(
        "SELECT CONCAT(DATE_FORMAT(last_active,'%m-%d %H:%i'), ' ',"
        " COALESCE(NULLIF(machine,''),'?'), ' ', session_id, '  ', turn_count, '轮')"
        f" FROM brain_sessions WHERE {cond_s}"
        " ORDER BY last_active DESC"
        f" LIMIT {max(0, int(args.sessions))}")

    now = time.strftime("%Y-%m-%d %H:%M")
    lines: list[str] = []
    lines.append("=" * 68)
    lines.append("brain 全库总览 · 最新在前")
    lines.append(f"生成 {now} · MariaDB {HOST}:{PORT} / {DB_NAME}"
                 + (f" · 过滤 machine={args.machine}" if args.machine else ""))
    lines.append(f"总量 记忆 {act} 活跃 / {total} 总计 · 对话 {turns_n} · 会话 {sess_n} · 存档 {arch_n}")
    lines.append("=" * 68)
    lines.append("")
    lines.append(f"── 记忆（brain_memories，{len(mems)} 条，最新在前"
                 + ("，含停用" if args.all else "") + "）──")
    lines.extend(mems)
    lines.append("")
    lines.append(f"── 最近对话（brain_turns，{len(turns)} 条，最新在前）──")
    lines.extend(turns)
    lines.append("")
    lines.append(f"── 会话（brain_sessions，{len(sess)} 个，最新在前）──")
    lines.extend(sess)
    lines.append("")
    text = "\n".join(lines)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print(f"已写入 {args.out}（{len(text.splitlines())} 行）")
    else:
        print(text)


if __name__ == "__main__":
    main()
