#!/usr/bin/env python3
"""brain_turns 完整性审计与回填（以本机 Hermes state.db 为基准）

背景：Hermes 本机会话全集在 state.db（sessions / messages 表）。
brain 插件理论上每轮把 (用户消息全文, 助手最终回复全文) 双写进 MariaDB brain_turns。
但配置变更窗口、写入失败等会静默丢轮次。本脚本逐会话比对两者，列出缺失轮次；
--apply 时从 state.db 原样回填（含全文），并校正 brain_sessions 的计数与起始时间。

用法（每台机器在各自机器上跑；x260-debian 应在笔记本上跑同样的命令）：
  python3 audit-turns.py                 # 审计（只读，不改数据）
  python3 audit-turns.py --apply         # 审计并回填缺失轮次
  python3 audit-turns.py --since '2026-10-12 15:00'    # 自定义起点（本地时间）
  python3 audit-turns.py --session 20261012_150311_c59721   # 只查一个会话
其它机器可带 --state-db / --plugin-dir / --hermes-home / --machine 覆盖默认值。

行为说明：
  * 匹配以"规范化空白后全文相等"为准（退化为前 60 字前缀匹配）；
  * 跳过"进行中"的末轮（最近 30 分钟内、尚无助手回复的），它会在回合结束时自动归档；
  * 跳过 "[System: ..." 之类的伪用户消息（不参与回填）；
  * 回填 ts 用该轮最终回复的时间（UTC，与 MariaDB 容器时区一致）；
  * brain_sessions 的 turn_count/started_at/last_active 按回填结果自动校正。
"""
from __future__ import annotations

import argparse
import datetime
import re
import sqlite3
import sys
from pathlib import Path

DEFAULT_STATE_DB = "/root/.hermes/state.db"
DEFAULT_HERMES_HOME = "/root/.hermes"
DEFAULT_PLUGIN_DIR = "/root/.hermes/plugins/brain"
DEFAULT_MACHINE = "server"

IN_FLIGHT_SECONDS = 1800  # 末轮 30 分钟内视为进行中


def norm(s: str) -> str:
    return " ".join((s or "").split())


_KEY_RE = re.compile(r"sk-[A-Za-z0-9._\-]{4,}", re.I)


def mask_keys(s: str) -> str:
    """抹掉 sk-… 密钥：同一密钥在两处脱敏位数不同，规范化后才能比对。"""
    return _KEY_RE.sub("<key>", s or "")


_ORIGIN_PREFIX = "gateway message origin"


def real_user_text(text: str) -> str | None:
    """state.db 里的 user 行取「真正的用户文本」。None = 不算一轮（内部注入）。

    * '[CONTEXT COMPACTION …]' / '[System: …]' → 不是用户轮，跳过；
    * 'Gateway message origin (JSON…)' → 后面隔一个空行才是用户原话。
    """
    t = (text or "").strip()
    low = t.lower()
    if low.startswith("[context compaction") or low.startswith("[system"):
        return None
    if low.startswith(_ORIGIN_PREFIX):
        parts = t.split("\n\n", 1)
        return parts[1].strip() if len(parts) == 2 else ""
    return t


def to_utc(epoch: float) -> str:
    """回填用：UTC（与 MariaDB 容器时区一致；2026-10-13 定稿，库内时间保持 UTC）。"""
    return datetime.datetime.fromtimestamp(
        float(epoch), datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def to_local(epoch: float) -> str:
    return datetime.datetime.fromtimestamp(float(epoch)).strftime("%m-%d %H:%M:%S")


def main() -> int:
    ap = argparse.ArgumentParser(description="brain_turns 完整性审计与回填")
    ap.add_argument("--apply", action="store_true", help="执行回填（默认只审计）")
    ap.add_argument("--since", default="2026-10-12 15:00",
                    help="起点（本地时间 YYYY-MM-DD HH:MM，默认 brain 全面上线时刻）")
    ap.add_argument("--session", default="", help="只处理指定 session_id")
    ap.add_argument("--state-db", default=DEFAULT_STATE_DB)
    ap.add_argument("--hermes-home", default=DEFAULT_HERMES_HOME)
    ap.add_argument("--plugin-dir", default=DEFAULT_PLUGIN_DIR)
    ap.add_argument("--machine", default=DEFAULT_MACHINE)
    args = ap.parse_args()

    sys.path.insert(0, args.plugin_dir)
    from core import BrainStore, resolve_config  # noqa: PLC0415

    store = BrainStore(resolve_config(args.hermes_home))
    store.ensure_schema()

    con = sqlite3.connect(f"file:{args.state_db}?mode=ro", uri=True)
    cur = con.cursor()

    since_epoch = datetime.datetime.strptime(args.since, "%Y-%m-%d %H:%M").timestamp()
    if args.session:
        sids = [args.session]
    else:
        sids = [r[0] for r in cur.execute(
            "SELECT id FROM sessions WHERE started_at >= ? ORDER BY started_at",
            (since_epoch,)).fetchall()]

    brain_rows = store._exec(
        "SELECT id, session_id, user_content FROM brain_turns", fetch=True)
    bt: dict[str, list[dict]] = {}
    for r in brain_rows:
        bt.setdefault(r["session_id"], []).append(
            {"id": r["id"], "u_norm": norm(r["user_content"])})

    now = datetime.datetime.now().timestamp()
    total_missing = 0
    total_backfilled = 0
    total_skipped = 0

    for sid in sids:
        row = cur.execute("SELECT source, started_at FROM sessions WHERE id=?", (sid,)).fetchone()
        if not row:
            continue
        source, _started = row
        msgs = cur.execute(
            "SELECT role, content, timestamp FROM messages "
            "WHERE session_id=? ORDER BY id", (sid,)).fetchall()

        turns: list[dict] = []
        cur_t: dict | None = None
        for role, content, ts in msgs:
            text = content if isinstance(content, str) else str(content or "")
            if role == "user":
                real = real_user_text(text)
                if real is None:
                    continue
                text = real
            if role == "user" and norm(text):
                cur_t = {"u": text, "u_ts": float(ts), "a": None, "a_ts": None}
                turns.append(cur_t)
            elif role == "assistant" and norm(text) and cur_t is not None:
                cur_t["a"] = text
                cur_t["a_ts"] = float(ts)

        pool = [dict(x) for x in bt.get(sid, [])]
        missing: list[dict] = []
        for t in turns:
            un = norm(t["u"])
            mun = mask_keys(un)
            hit = None
            for i, b in enumerate(pool):
                if mask_keys(b["u_norm"]) == mun:
                    hit = i
                    break
            if hit is None:
                for i, b in enumerate(pool):
                    bn = mask_keys(b["u_norm"])
                    if (len(bn) >= 8 and mun.startswith(bn)) or (len(mun) >= 8 and bn.startswith(mun)):
                        hit = i
                        break
            if hit is None:
                for i, b in enumerate(pool):
                    if un and b["u_norm"] and b["u_norm"][:60] == un[:60]:
                        hit = i
                        break
            if hit is not None:
                pool.pop(hit)
            else:
                missing.append(t)

        pending: list[dict] = []
        for t in missing:
            un = norm(t["u"])
            if un.startswith("[") and un.lower().startswith("[system"):
                print(f"  [跳过-伪消息] {sid}: {un[:40]}…")
                total_skipped += 1
                continue
            if t["a"] is None:
                print(f"  [跳过-无回复] {sid}: {to_local(t['u_ts'])} | {un[:40]}")
                total_skipped += 1
                continue
            is_last = turns and t is turns[-1]
            if is_last and (now - t["u_ts"]) < IN_FLIGHT_SECONDS:
                print(f"  [跳过-进行中] {sid}: {to_local(t['u_ts'])} | {un[:40]}（回合结束后会自动归档）")
                total_skipped += 1
                continue
            pending.append(t)

        if not pending:
            continue

        total_missing += len(pending)
        print(f"会话 {sid}（{source}）缺 {len(pending)} 轮：")
        for t in pending:
            print(f"  - {to_local(t['u_ts'])} | {norm(t['u'])[:56]}")

        if args.apply:
            for t in pending:
                ts_val = to_utc(t["a_ts"] or t["u_ts"])
                u_raw, a_raw = str(t["u"]), str(t["a"])
                trunc = 1 if (len(u_raw) > 16000 or len(a_raw) > 16000) else 0
                store._exec(
                    "INSERT INTO brain_turns "
                    "(session_id, platform, author, machine, truncated, user_content, assistant_content, ts) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (sid, str(source or ""), "", args.machine, trunc,
                     u_raw[:16000], a_raw[:16000], ts_val))
                total_backfilled += 1
            cnt_row = store._exec(
                "SELECT COUNT(*) c, MIN(ts) mn, MAX(ts) mx FROM brain_turns WHERE session_id=%s",
                (sid,), fetch=True)[0]
            store._exec(
                "INSERT INTO brain_sessions "
                "(session_id, platform, machine, started_at, last_active, turn_count) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE platform=VALUES(platform), machine=VALUES(machine), "
                "started_at=LEAST(started_at, VALUES(started_at)), "
                "last_active=GREATEST(last_active, VALUES(last_active)), "
                "turn_count=VALUES(turn_count)",
                (sid, str(source or ""), args.machine,
                 cnt_row["mn"], cnt_row["mx"], int(cnt_row["c"])))
            print(f"  => 已回填 {len(pending)} 轮，brain_sessions 计数校正为 {int(cnt_row['c'])}")

    print()
    if args.apply:
        print(f"=== 缺 {total_missing} 轮，已回填 {total_backfilled} 轮，跳过 {total_skipped} 条 ===")
    else:
        print(f"=== 缺 {total_missing} 轮，跳过 {total_skipped} 条（未写库；--apply 执行回填） ===")
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
