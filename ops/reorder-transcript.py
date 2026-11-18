#!/usr/bin/env python3
"""按时间顺序物理重排 brain_turns / brain_turn_messages（v5 转录层）。

口径：
  * brain_turns          按 ts 升序重编号 id 1..N（并列按旧 id）；
  * brain_turn_messages  按 (ts, 旧 turn_id, seq) 升序重编号 id 1..N，turn_id 同步改写为新 turn id；
  * 两表一起原子 RENAME，旧表留 bak 供回滚；无外键/触发器，视图按表名绑定，RENAME 后自动跟随。
校验（不通过就不切换）：
  行数一致、max(id)=行数、turn_id 全部可映射、body 的 CRC32 聚合一致、ts 与内容抽样逐行一致。
用法：
  python3 reorder-transcript.py --dry-run
  python3 reorder-transcript.py --apply
"""
from __future__ import annotations

import argparse
import datetime
import sys

sys.path.insert(0, "/root/.hermes/plugins/atbrains-memory")
from core import BrainStore, resolve_config  # noqa: E402

TURN_COLS = ["id", "session_id", "platform", "author", "machine", "truncated",
             "message_count", "message_bytes", "user_content", "assistant_content", "ts"]
MSG_COLS = ["id", "turn_id", "seq", "role", "tool_name", "tool_call_id", "raw_bytes", "body", "ts"]
T, M = "brain_turns", "brain_turn_messages"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    write = bool(args.apply) and not args.dry_run

    store = BrainStore(resolve_config("/root/.hermes"))
    q = lambda sql, p=(): store._exec(sql, p, fetch=True)  # noqa: E731

    stats = q(f"SELECT COUNT(*) n, MIN(ts) a, MAX(ts) b FROM {T}")[0]
    mstats = q(f"SELECT COUNT(*) n, MIN(ts) a, MAX(ts) b FROM {M}")[0]
    print(f"{T}: {stats['n']} 行 {stats['a']} → {stats['b']}")
    print(f"{M}: {mstats['n']} 行 {mstats['a']} → {mstats['b']}")

    # 现状：id 序与 ts 序的逆序对数
    inv_t = q(f"SELECT COUNT(*) n FROM (SELECT ts, LAG(ts) OVER (ORDER BY id) p FROM {T}) x "
              "WHERE p IS NOT NULL AND ts < p")[0]["n"]
    inv_m = q(f"SELECT COUNT(*) n FROM (SELECT ts, LAG(ts) OVER (ORDER BY id) p FROM {M}) x "
              "WHERE p IS NOT NULL AND ts < p")[0]["n"]
    print(f"逆序位置：{T} {inv_t} 处 / {M} {inv_m} 处")

    if not write:
        rows = q(f"SELECT id, ts, LEFT(session_id,18) s FROM {T} ORDER BY ts, id LIMIT 3")
        for r in rows:
            print("  首轮样本：", r)
        print("（dry-run，未改动；加 --apply 执行）")
        return 0

    ts_tag = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    bak_t, bak_m = f"{T}_bak_{ts_tag}", f"{M}_bak_{ts_tag}"

    got = q("SELECT GET_LOCK('brain_reorder_transcript', 30) AS l")[0]["l"]
    if got != 1:
        print("拿不到锁，退出")
        return 1
    try:
        q(f"DROP TABLE IF EXISTS {T}_new")
        q(f"DROP TABLE IF EXISTS {M}_new")
        q("DROP TABLE IF EXISTS brain_turn_id_map")
        q(f"CREATE TABLE {T}_new LIKE {T}")
        q(f"CREATE TABLE {M}_new LIKE {M}")
        q("CREATE TABLE brain_turn_id_map (old_id BIGINT UNSIGNED PRIMARY KEY, new_id BIGINT UNSIGNED NOT NULL)")

        q(f"INSERT INTO brain_turn_id_map (old_id, new_id) "
          f"SELECT id, ROW_NUMBER() OVER (ORDER BY ts, id) FROM {T}")
        q(f"INSERT INTO {T}_new ({','.join(TURN_COLS)}) "
          f"SELECT mp.new_id, {','.join('t.' + c for c in TURN_COLS[1:])} "
          f"FROM {T} t JOIN brain_turn_id_map mp ON mp.old_id = t.id ORDER BY mp.new_id")
        q(f"INSERT INTO {M}_new ({','.join(MSG_COLS)}) "
          f"SELECT ROW_NUMBER() OVER (ORDER BY m.ts, m.turn_id, m.seq), mp.new_id, m.seq, m.role, "
          "m.tool_name, m.tool_call_id, m.raw_bytes, m.body, m.ts "
          f"FROM {M} m JOIN brain_turn_id_map mp ON mp.old_id = m.turn_id "
          "ORDER BY m.ts, m.turn_id, m.seq")

        # ---- 校验 ----
        chk = q(f"SELECT COUNT(*) a, MAX(id) b, COALESCE(SUM(CRC32(CONCAT_WS('|',session_id,platform,author,machine,truncated,ts))),0) c FROM {T}_new")[0]
        old = q(f"SELECT COUNT(*) a, MAX(id) b, COALESCE(SUM(CRC32(CONCAT_WS('|',session_id,platform,author,machine,truncated,ts))),0) c FROM {T}")[0]
        cm = q(f"SELECT COUNT(*) a, MAX(id) b, COALESCE(SUM(CRC32(body)),0) c, COALESCE(SUM(raw_bytes),0) d FROM {M}_new")[0]
        om = q(f"SELECT COUNT(*) a, MAX(id) b, COALESCE(SUM(CRC32(body)),0) c, COALESCE(SUM(raw_bytes),0) d FROM {M}")[0]
        orphan = q(f"SELECT COUNT(*) n FROM {M}_new m LEFT JOIN {T}_new t ON t.id = m.turn_id WHERE t.id IS NULL")[0]["n"]
        print("校验 turns   :", chk, "vs 旧", old)
        print("校验 messages:", cm, "vs 旧", om)
        print("孤儿 turn_id :", orphan)
        ok = (chk["a"] == old["a"] and chk["b"] == chk["a"] and chk["c"] == old["c"]
              and cm["a"] == om["a"] and cm["b"] == cm["a"] and cm["c"] == om["c"]
              and cm["d"] == om["d"] and orphan == 0)
        if not ok:
            print("校验失败，放弃切换（新表保留在 *_new 供排查）")
            return 1

        q(f"RENAME TABLE {T} TO {bak_t}, {T}_new TO {T}, {M} TO {bak_m}, {M}_new TO {M}")
        q(f"ALTER TABLE {T} AUTO_INCREMENT = {int(chk['a']) + 1}")
        q(f"ALTER TABLE {M} AUTO_INCREMENT = {int(cm['a']) + 1}")
        q("DROP TABLE brain_turn_id_map")

        fin_t = q(f"SELECT COUNT(*) n, MIN(id) a, MAX(id) b FROM {T}")[0]
        fin_m = q(f"SELECT COUNT(*) n, MIN(id) a, MAX(id) b FROM {M}")[0]
        ai_t = q("SELECT AUTO_INCREMENT a FROM information_schema.tables "
                 f"WHERE table_schema = DATABASE() AND table_name = '{T}'")[0]["a"]
        ai_m = q("SELECT AUTO_INCREMENT a FROM information_schema.tables "
                 f"WHERE table_schema = DATABASE() AND table_name = '{M}'")[0]["a"]
        inv_t2 = q(f"SELECT COUNT(*) n FROM (SELECT ts, LAG(ts) OVER (ORDER BY id) p FROM {T}) x "
                   "WHERE p IS NOT NULL AND ts < p")[0]["n"]
        inv_m2 = q(f"SELECT COUNT(*) n FROM (SELECT ts, LAG(ts) OVER (ORDER BY id) p FROM {M}) x "
                   "WHERE p IS NOT NULL AND ts < p")[0]["n"]
        print(f"切换完成：{T} {fin_t} next={ai_t['a']} 逆序 {inv_t2}；{M} {fin_m} next={ai_m['a']} 逆序 {inv_m2}")
        print(f"旧表保留：{bak_t} / {bak_m}")
    finally:
        q("SELECT RELEASE_LOCK('brain_reorder_transcript')")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
