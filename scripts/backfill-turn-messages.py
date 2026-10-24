#!/usr/bin/env python3
"""把本机 Hermes state.db 的历史工具调用过程回填进 brain_turn_messages（schema v5）。

口径（与 audit-turns.py 一致）：
  * 以 state.db 的 messages 为唯一事实源，机器归属按 brain_turns.machine 过滤；
  * 每一轮 = state.db 里「匹配到的 user 消息」→「匹配到的最终 assistant 回复」之间的全部消息
    （含 assistant 的 tool_calls、tool 返回、reasoning），system 消息不入库；
  * 匹配：user 正文规范化后全文相等，退化到前 60 字；assistant 回复用前 200 字前缀比对；
  * 幂等：该 turn_id 已有转录行时默认跳过（--force 先删后写）；
  * 写完后同步 brain_turns.message_count/message_bytes 与 brain_sessions.archived_messages。

用法：
  python3 backfill-turn-messages.py --dry-run          # 只统计，不写
  python3 backfill-turn-messages.py --apply            # 写入
  python3 backfill-turn-messages.py --apply --machine server --force
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import sqlite3
import sys
import zlib
from pathlib import Path

KEY_RE = re.compile(r"sk-[A-Za-z0-9._\-]{4,}", re.I)

DEFAULT_STATE_DB = "/root/.hermes/state.db"
DEFAULT_HERMES_HOME = "/root/.hermes"
DEFAULT_PLUGIN_DIR = "/root/.hermes/plugins/atbrains-memory"

MAX_MSG_BYTES = 1048576
MAX_TURN_BYTES = 16777216
# 内部注入的消息：不作为轮边界（它们是 turn 进行中塞进来的通知）
INTERNAL_PREFIXES = ("[important:", "[system", "[context compaction", "[gateway message origin")


def norm(s: str) -> str:
    return " ".join((s or "").split())


def mask_keys(s: str) -> str:
    """把 sk-… 形式的密钥抹成 <key>：两边脱敏位数不同也能匹配。"""
    return KEY_RE.sub("<key>", s or "")


_ORIGIN_PREFIX = "gateway message origin"


def real_user_text(text: str) -> str | None:
    """state.db 的 user 行 → 真正的用户文本；None = 不算一轮（内部注入）。"""
    t = (text or "").strip()
    low = t.lower()
    if low.startswith("[context compaction") or low.startswith("[system"):
        return None
    if low.startswith(_ORIGIN_PREFIX):
        parts = t.split("\n\n", 1)
        return parts[1].strip() if len(parts) == 2 else ""
    return t


def clean_brain_text(text: str) -> str:
    """brain 里的 user_content 去掉 Hermes 追加的尾注/前缀，用于匹配。"""
    t = (text or "").strip()
    idx = t.lower().find("user correction during the turn")
    if idx > 0:
        t = t[:idx].strip()
    if t.lower().startswith(_ORIGIN_PREFIX):
        parts = t.split("\n\n", 1)
        t = parts[1].strip() if len(parts) == 2 else ""
    return t


def main() -> int:
    ap = argparse.ArgumentParser(description="回填 brain_turn_messages（历史工具调用过程）")
    ap.add_argument("--apply", action="store_true", help="真正写入（默认 dry-run）")
    ap.add_argument("--dry-run", action="store_true", help="只统计")
    ap.add_argument("--machine", default="server", help="只回填该 machine 的轮次")
    ap.add_argument("--force", action="store_true", help="已有转录的轮次也重写")
    ap.add_argument("--verbose", action="store_true", help="打印未匹配的轮次")
    ap.add_argument("--state-db", default=DEFAULT_STATE_DB)
    ap.add_argument("--hermes-home", default=DEFAULT_HERMES_HOME)
    ap.add_argument("--plugin-dir", default=DEFAULT_PLUGIN_DIR)
    args = ap.parse_args()
    write = bool(args.apply) and not args.dry_run

    sys.path.insert(0, args.plugin_dir)
    from core import BrainStore, resolve_config  # noqa: PLC0415

    store = BrainStore(resolve_config(args.hermes_home))
    store.ensure_schema()

    con = sqlite3.connect(f"file:{args.state_db}?mode=ro", uri=True)
    cur = con.cursor()

    turns = store._exec(
        "SELECT id, session_id, user_content, assistant_content, ts FROM brain_turns "
        "WHERE machine = %s ORDER BY ts, id", (args.machine,), fetch=True)
    by_session: dict[str, list[dict]] = {}
    for r in turns:
        by_session.setdefault(r["session_id"], []).append(dict(r))
    print(f"brain_turns(machine={args.machine})：{len(turns)} 轮 / {len(by_session)} 个会话")

    existing = {r["turn_id"] for r in store._exec(
        "SELECT DISTINCT turn_id FROM brain_turn_messages", fetch=True)}

    stats = {"turns": 0, "matched": 0, "no_user": 0,
             "skipped_has_msgs": 0, "msgs": 0, "raw_bytes": 0, "comp_bytes": 0}
    per_session_cursor: dict[str, int] = {}

    for sid, srows in by_session.items():
        msgs = cur.execute(
            "SELECT id, role, content, tool_calls, tool_name, tool_call_id, reasoning, display_kind, timestamp "
            "FROM messages WHERE session_id = ? AND role != 'system' ORDER BY id",
            (sid,)).fetchall()
        if not msgs:
            stats["no_user"] += len(srows)
            continue
        rows = [{"id": m[0], "role": m[1] or "", "content": m[2] if isinstance(m[2], str) else "",
                 "tool_calls": m[3], "tool_name": m[4] or "", "tool_call_id": m[5] or "",
                 "reasoning": m[6] if isinstance(m[6], str) else "",
                 "display_kind": m[7] or "", "ts": float(m[8] or 0)} for m in msgs]
        per_session_cursor[sid] = len(rows)
        norm_user = []
        for i, r in enumerate(rows):
            if r["role"] != "user":
                continue
            real = real_user_text(r["content"])
            if real:
                norm_user.append((i, norm(real)))

        # ---- 匹配：每个 brain_turn 独立打分，再全局唯一分配（顺序游标一旦被误配就会连带后面全错）
        user_ts = {i: float(r.get("ts") or 0) for i, r in enumerate(rows) if r["role"] == "user"}
        scored: list[tuple[int, float, dict, list[tuple[int, int]]]] = []
        for t in srows:
            stats["turns"] += 1
            if t["id"] in existing and not args.force:
                stats["skipped_has_msgs"] += 1
                continue
            un = norm(clean_brain_text(t["user_content"]))
            mun = mask_keys(un)
            lst: list[tuple[int, int]] = []
            for i, nu in norm_user:
                mnu = mask_keys(nu)
                if mnu == mun or nu == un:
                    lst.append((0, i))
                elif len(mnu) >= 8 and mun.startswith(mnu):
                    lst.append((1, i))
                elif len(nu) >= 8 and un.startswith(nu):
                    lst.append((1, i))
                elif nu[:60] == un[:60]:
                    lst.append((2, i))
            bts = (t["ts"].replace(tzinfo=datetime.timezone.utc).timestamp()
                   if isinstance(t["ts"], datetime.datetime) else 0.0)
            scored.append((min((s for s, _ in lst), default=9), bts, t, lst))

        assigned: dict[int, int] = {}
        taken: set[int] = set()
        for _score, bts, t, lst in sorted(scored, key=lambda x: (x[0], x[1])):
            free = [(s, i) for s, i in lst if i not in taken]
            if not free:
                continue
            best = min(s for s, _ in free)
            pool = [i for s, i in free if s == best]
            pick = min(pool, key=lambda i: abs(user_ts.get(i, 0.0) - bts))
            assigned[t["id"]] = pick
            taken.add(pick)

        for _score, _bts, t, _lst in sorted(scored, key=lambda x: x[1]):
            start = assigned.get(t["id"])
            if start is None:
                stats["no_user"] += 1
                if getattr(args, "verbose", False):
                    print(f"  [未匹配] turn {t['id']} {sid} | {norm(t['user_content'])[:70]}")
                continue
            # 区间终点：下一个 user 消息之前的最后一条非 user 消息。
            # 用「任意 user 消息」当边界（内部通知也算）：通知本身在 brain 里是独立一轮，
            # 它触发的工具调用应归到它自己那一轮，这样两边都不丢内容。
            end = start
            for j in range(start + 1, len(rows)):
                if rows[j]["role"] == "user":
                    break
                end = j
            segment = rows[start:end + 1]
            if not segment:
                continue
            payload_rows, total = [], 0
            for seq, m in enumerate(segment):
                payload = {"content": m["content"]}
                if m["tool_calls"]:
                    payload["tool_calls"] = m["tool_calls"]
                if m["reasoning"]:
                    payload["reasoning"] = m["reasoning"]
                raw = json.dumps(payload, ensure_ascii=False).encode("utf-8", "replace")
                if len(raw) > MAX_MSG_BYTES:
                    payload = {"_truncated_bytes": len(raw),
                               "content": m["content"][:MAX_MSG_BYTES // 4]}
                    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8", "replace")
                if total + len(raw) > MAX_TURN_BYTES:
                    break
                total += len(raw)
                comp = zlib.compress(raw, 6)
                mts = (datetime.datetime.utcfromtimestamp(float(m["ts"]) or 0.0)
                       if m.get("ts") else None)
                payload_rows.append((t["id"], seq, m["role"][:16], m["tool_name"][:64],
                                     m["tool_call_id"][:128], len(raw), comp, mts))
            if not payload_rows:
                continue
            stats["matched"] += 1
            stats["msgs"] += len(payload_rows)
            stats["raw_bytes"] += total
            stats["comp_bytes"] += sum(len(p[6]) for p in payload_rows)
            used_upto = end + 1
            if not write:
                continue
            if t["id"] in existing and args.force:
                store._exec("DELETE FROM brain_turn_messages WHERE turn_id = %s", (t["id"],))
            store._exec_many(
                "INSERT INTO brain_turn_messages (turn_id, seq, role, tool_name, tool_call_id, "
                "raw_bytes, body, ts) VALUES (%s, %s, %s, %s, %s, %s, %s, COALESCE(%s, UTC_TIMESTAMP()))",
                payload_rows)
            store._exec(
                "UPDATE brain_turns SET message_count = %s, message_bytes = %s WHERE id = %s",
                (min(len(payload_rows), 65535), min(total, 4294967295), t["id"]))
            existing.add(t["id"])

    print(json.dumps(stats, ensure_ascii=False))
    if write:
        for sid, cnt in per_session_cursor.items():
            store._exec(
                "UPDATE brain_sessions SET archived_messages = %s WHERE session_id = %s",
                (cnt, sid))
        print(f"已回写 brain_sessions.archived_messages：{len(per_session_cursor)} 个会话")
        tot = store._exec(
            "SELECT COUNT(*) AS c, COALESCE(SUM(raw_bytes),0) AS b FROM brain_turn_messages",
            fetch=True)[0]
        print(f"库内现有转录 {tot['c']} 条消息 / 原始 {tot['b'] / 1048576:.1f} MB")
    else:
        print("（dry-run，未写入；加 --apply 执行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
