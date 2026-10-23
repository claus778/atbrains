#!/usr/bin/env python3
"""清理 brain_turns 里「同一轮被写了两遍」的重复行。

背景：audit-turns.py 的匹配按规范化全文，遇到 Hermes 脱敏（sk-… 位数不同）、
"User correction during the turn:" 尾注时认不出原有轮次，会把同一轮再插一遍。
本脚本按「内容规则 + 时间就近」把每个 brain_turn 对到 state.db 的某条 user 消息上，
同一 (session, 消息) 命中多行时保留 id 最小的那行（原写入），删掉后插的重复行及其转录。

用法：
  python3 dedup-brain-turns.py               # 只报告
  python3 dedup-brain-turns.py --apply       # 真删
  python3 dedup-brain-turns.py --apply --machine server
"""
from __future__ import annotations

import argparse
import datetime
import re
import sqlite3
import sys

DEFAULT_STATE_DB = "/root/.hermes/state.db"
KEY_RE = re.compile(r"sk-[A-Za-z0-9._\-]{4,}", re.I)
norm = lambda s: " ".join((s or "").split())
mask = lambda s: KEY_RE.sub("<key>", s or "")


def candidates(un: str, users: list[tuple[int, str]]):
    """按内容规则找候选 user 消息下标。"""
    mun = mask(un)
    out = []
    for i, nu in users:
        if not nu:
            continue
        mnu = mask(nu)
        if mnu == mun or (len(mnu) >= 8 and (mun.startswith(mnu) or mnu.startswith(mun[:60]))):
            out.append(i)
        elif nu == un:
            out.append(i)
        elif un.startswith(nu) and (len(nu) >= 8 or un[len(nu):].lstrip().lower().startswith("user correction")):
            out.append(i)
        elif nu[:60] == un[:60]:
            out.append(i)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--machine", default="server")
    ap.add_argument("--state-db", default=DEFAULT_STATE_DB)
    ap.add_argument("--hermes-home", default="/root/.hermes")
    ap.add_argument("--plugin-dir", default="/root/.hermes/plugins/brain")
    args = ap.parse_args()

    sys.path.insert(0, args.plugin_dir)
    from core import BrainStore, resolve_config  # noqa: PLC0415

    store = BrainStore(resolve_config(args.hermes_home))
    store.ensure_schema()
    con = sqlite3.connect(f"file:{args.state_db}?mode=ro", uri=True)

    turns = store._exec(
        "SELECT id, session_id, user_content, ts FROM brain_turns WHERE machine=%s ORDER BY session_id, id",
        (args.machine,), fetch=True)
    by_sess: dict[str, list[dict]] = {}
    for t in turns:
        by_sess.setdefault(t["session_id"], []).append(dict(t))

    dupes = []
    for sid, rows_t in by_sess.items():
        msgs = con.execute(
            "SELECT id, role, content, timestamp FROM messages WHERE session_id=? AND role!='system' ORDER BY id",
            (sid,)).fetchall()
        if not msgs:
            continue
        users = [(i, norm(m[2])) for i, m in enumerate(msgs) if m[1] == "user"]
        ts_by_idx = {i: float(m[3]) for i, m in enumerate(msgs)}
        groups: dict[int, list[tuple[int, float]]] = {}
        for t in rows_t:
            un = norm(t["user_content"])
            cand = candidates(un, users)
            if not cand:
                continue
            bts = (t["ts"].replace(tzinfo=datetime.timezone.utc).timestamp()
                   if isinstance(t["ts"], datetime.datetime) else 0.0)
            best = min(cand, key=lambda i: abs(ts_by_idx.get(i, 0) - bts))
            groups.setdefault(best, []).append((t["id"], abs(ts_by_idx.get(best, 0) - bts)))
        for idx, lst in groups.items():
            if len(lst) < 2:
                continue
            lst.sort()
            keep = lst[0][0]
            drop = [x[0] for x in lst[1:]]
            # 只删「重复插入」的：时间差都很小（同一条消息被两行认领）
            if max(x[1] for x in lst) > 6 * 3600:
                continue
            # 该文本在会话里有多条候选消息时不动（可能是用户真的说了两遍）
            keep_text = next((t["user_content"] for t in rows_t if t["id"] == keep), "")
            if len(candidates(norm(keep_text), users)) > 1:
                continue
            dupes.append((sid, keep, drop, users and dict(users).get(idx, "")[:40]))

    print(f"发现 {len(dupes)} 组重复轮次（保留 id 最小的那行）")
    for sid, keep, drop, text in dupes[:20]:
        print(f"  {sid} keep={keep} drop={drop} | {text}")
    # 详细时间线，便于人工确认不是「两次相同的话」
    if args.verbose:
        for sid, keep, drop, _t in dupes:
            rows = {r["id"]: r for r in by_sess.get(sid, [])}
            line = [f"{i}@{rows[i]['ts']:%m-%d %H:%M:%S}" for i in [keep, *drop] if i in rows]
            print(f"    timeline {sid}: " + "  ".join(line))
    total_drop = sum(len(d[2]) for d in dupes)
    print(f"合计待删 {total_drop} 行")
    if not args.apply:
        print("（未写入；加 --apply 执行）")
        return 0
    for _sid, _keep, drop, _t in dupes:
        for tid in drop:
            store._exec("DELETE FROM brain_turn_messages WHERE turn_id=%s", (tid,))
            store._exec("DELETE FROM brain_turns WHERE id=%s", (tid,))
    left = store._exec("SELECT COUNT(*) AS c FROM brain_turns WHERE machine=%s", (args.machine,), fetch=True)[0]["c"]
    print(f"已删除 {total_drop} 行；现有 brain_turns(machine={args.machine}) = {left}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
