#!/usr/bin/env python3
"""内置记忆(MEMORY.md/USER.md)↔ brain_memories 完整性对账"""
import re, os, sys, subprocess, json

HOME = "/root/.hermes"

def read_entries(path):
    txt = open(path, encoding="utf-8").read()
    # 按行首 § 拆分
    parts = re.split(r"^\s*§\s*$", txt, flags=re.M)
    out = []
    for p in parts:
        p = p.strip()
        if p:
            out.append(p)
    return out

def norm(s):
    return " ".join((s or "").split())

# 取 brain 里 active 的记忆
sys.path.insert(0, os.path.join(HOME, "plugins", "brain"))
from core import BrainStore, resolve_config
store = BrainStore(resolve_config(HOME))
rows = store._exec(
    "SELECT id, machine, kind, content FROM brain_memories WHERE active=1", fetch=True)
store.close()

brain = [{"id": r["id"], "machine": r["machine"] or "", "kind": r["kind"],
          "n": norm(r["content"])} for r in rows]

total_missing = 0
for fname, label in (("memories/MEMORY.md", "MEMORY"), ("memories/USER.md", "USER")):
    path = os.path.join(HOME, fname)
    entries = read_entries(path)
    print(f"══ {label}.md: {len(entries)} 条 ══")
    for e in entries:
        en = norm(e)
        hit = None
        for b in brain:
            if b["n"] == en:
                hit = b; break
        if hit is None:
            # 退化:前 60 字匹配
            for b in brain:
                if en and b["n"] and (b["n"][:60] == en[:60] or en[:60] in b["n"] or b["n"][:60] in en):
                    hit = b; break
        if hit:
            print(f"  ✅ [{hit['id']:>3}] {e[:58]}")
        else:
            total_missing += 1
            print(f"  ❌ 未镜像  {e[:70]}")
    print()

print(f"══ 结论:内置 {len(read_entries(os.path.join(HOME,'memories/MEMORY.md')))+len(read_entries(os.path.join(HOME,'memories/USER.md')))} 条,未镜像 {total_missing} 条 ══")
