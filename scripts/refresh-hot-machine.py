#!/usr/bin/env python3
"""校正 Redis 热点镜像(brain:hot:<id>)里的 machine 字段，使其与 MariaDB 现值一致。

背景：brain:hot:<id> 是写入时的快照；machine 标签回填（如 '' -> server）之后，
旧快照仍保留旧值/缺失字段，而 brain_recent(what=memories) 直接读快照，
会出现「库里是 server、热点里没标签」的不一致。本脚本扫自己 Redis 库的热点键逐条校正。

运行（用插件自带运行时，内含 vendor/redis）:
  HERMES_DISABLE_LAZY_INSTALLS=1 /root/.hermes/tools/python-3.14.7+*/bin/python3 \
      -I /root/agent-db/refresh-hot-machine.py [--dry-run] [--hermes-home /root/.hermes] [--redis-db N]
（--redis-db：指定要校正的 Redis 库号；缺省=本机 brain.json 的库。用于给其它 agent 的库做校正，如 x260 的 db1）
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ap = argparse.ArgumentParser()
ap.add_argument("--dry-run", action="store_true")
ap.add_argument("--hermes-home", default="/root/.hermes")
ap.add_argument("--redis-db", type=int, default=None,
                help="指定 Redis 库号（缺省=本机 brain.json；用于校正其它 agent 的库）")
args = ap.parse_args()

sys.path.insert(0, os.path.join(args.hermes_home, "plugins", "brain"))
from core import BrainStore, resolve_config  # noqa: E402

cfg = resolve_config(args.hermes_home)
if args.redis_db is not None:
    cfg["redis_db"] = args.redis_db
store = BrainStore(cfg)
r = store.redis()
prefix = "brain:hot:"

fixed = consistent = orphan = 0
for key in r.scan_iter(match=prefix + "*"):
    key = key.decode() if isinstance(key, bytes) else str(key)
    mem_id_s = key[len(prefix):]
    if mem_id_s == "index" or not mem_id_s.isdigit():
        continue
    mem_id = int(mem_id_s)
    raw = r.get(key)
    if not raw:
        continue
    payload = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    rows = store._exec("SELECT machine FROM brain_memories WHERE id=%s", (mem_id,), fetch=True)
    if not rows:
        orphan += 1
        print(f"[{mem_id}] MariaDB 无此行（热点残留，可考虑删除）")
        continue
    db_machine = str(rows[0]["machine"] or "").strip()
    cur = str(payload.get("machine") or "").strip()
    if cur == db_machine:
        consistent += 1
        continue
    if args.dry_run:
        print(f"[{mem_id}] 将更新 machine: {cur!r} -> {db_machine!r}")
        fixed += 1
        continue
    payload["machine"] = db_machine
    ttl = r.ttl(key)
    data = json.dumps(payload, ensure_ascii=False)
    if isinstance(ttl, int) and ttl > 0:
        r.set(key, data, ex=ttl)
    else:
        r.set(key, data)
    fixed += 1
    print(f"[{mem_id}] machine: {cur!r} -> {db_machine!r} (ttl={ttl})")

mode = "预演" if args.dry_run else "已执行"
print(f"完成({mode}): 修正 {fixed} 条 · 已一致 {consistent} 条 · DB无记录 {orphan} 条")
