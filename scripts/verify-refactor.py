#!/usr/bin/env python3
"""brain 改造(2/3/4 步)收尾验收 —— 一次跑完所有硬指标。"""
import json, subprocess, sys, time, urllib.error, urllib.request

BRAIN = "/root/.hermes/plugins/brain"
ATBRAINS = "/root/.hermes/plugins/atbrains"
sys.path.insert(0, BRAIN)

PASS, FAIL = [], []


def chk(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))


print("=" * 62)
print("① 语法与 schema")
for f in (f"{BRAIN}/core.py", f"{BRAIN}/cli.py", f"{ATBRAINS}/service.py",
          f"{ATBRAINS}/console/server.py", f"{ATBRAINS}/cli.py"):
    r = subprocess.run([sys.executable, "-m", "py_compile", f], capture_output=True, text=True)
    chk(f"语法 {f.split('/')[-1]}", r.returncode == 0, r.stderr[:80])

from core import BrainStore, resolve_config, _memory_type
s = BrainStore(resolve_config("/root/.hermes"))
chk("schema 为 v4", s.schema_version() == 4, f"v{s.schema_version()}")
cols = [r["Field"] for r in s._exec("SHOW COLUMNS FROM brain_memories", fetch=True)]
for c in ("superseded_by", "superseded_at", "memory_type"):
    chk(f"列 {c}", c in cols)

print("=" * 62)
print("② 数据完整性")
r = s._exec("SELECT COUNT(*) t, SUM(active=1) a, SUM(superseded_by IS NOT NULL) sup FROM brain_memories", fetch=True)[0]
chk("无残留取代标记(本次未 apply)", r["sup"] == 0, f"{r['sup']} 条")
p = s._exec("SELECT COUNT(*) c FROM brain_memories WHERE content LIKE %s OR kind LIKE %s",
            ("%__brain_selftest__%", "%selftest%"), fetch=True)[0]["c"]
chk("无测试残留", p == 0, f"{p} 条")
layers = {x["memory_type"] or "(空)": x["c"] for x in
          s._exec("SELECT memory_type, COUNT(*) c FROM brain_memories WHERE active=1 GROUP BY memory_type", fetch=True)}
chk("分层无空值", "(空)" not in layers, str(layers))

print("=" * 62)
print("③ 第 2 步:分层 + 衰减 + 取代方向")
chk("_memory_type 映射", _memory_type("builtin-memory") == "profile" and _memory_type("fact") == "knowledge"
    and _memory_type("note") == "episodic", "profile/knowledge/episodic")
pairs = {p["id"]: p["superseded_by"] for p in s.detect_supersedes(dry_run=True, threshold=0.90)["pairs"]}
chk("取代方向:更新者胜", pairs.get(43) == 45 or pairs.get(105) == 108,
    f"检出 {pairs}  (期望 43→45 / 105→108)")
chk("dry_run 不写库",
    s._exec("SELECT COUNT(*) c FROM brain_memories WHERE superseded_by IS NOT NULL", fetch=True)[0]["c"] == 0)

print("=" * 62)
print("④ 第 4 步:rerank")
import pprint
D = s.cfg
chk("rerank 默认关闭", int(D.get("rerank_enabled") or 0) == 0)
try:
    cands = s.search_memories_hybrid("SSH 端口", limit=6)
    rr = s.rerank("SSH 端口为什么改成 7622", cands, limit=3)
    chk("rerank 真调用成功", len(rr) > 0, f"返回 {len(rr)} 条,分数 "
        + ",".join(f"{m.get('rerank_score', 0):.3f}" for m in rr[:3]))
except Exception as e:
    chk("rerank 真调用成功", False, f"{type(e).__name__}: {str(e)[:100]}")

print("=" * 62)
print("⑤ 第 3 步:Memory Service")
def http(method, path, body=None, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:8787{path}", method=method,
                                 headers=headers or {}, data=json.dumps(body).encode() if body else None)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, {}

tok = json.load(open(f"{ATBRAINS}/service-tokens.json"))["server"]
CT = open(f"{ATBRAINS}/console/.token").read().strip()
st, d = http("GET", "/v1/health")
chk("/v1/health", st == 200 and d.get("ok"), str(d)[:70])
st, d = http("POST", "/v1/memory/recall", {"query": "SSH 端口"},
             {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
chk("/v1/memory/recall", st == 200 and d.get("count", 0) > 0, f"count={d.get('count')} 文本{len(d.get('text') or '')}字")
st, _ = http("POST", "/v1/memory/status", None, {"Authorization": f"Bearer {tok}"})
chk("/v1/memory/status(带令牌)", st == 200)
st, _ = http("POST", "/v1/memory/status")
chk("/v1 无令牌 401", st == 401)
st, _ = http("POST", "/v1/memory/status", None, {"Authorization": f"Bearer {CT}"})
chk("/v1 用控制台令牌 401", st == 401)
st, _ = http("GET", "/api/overview", None, {"X-Auth-Token": tok})
chk("/api 用 /v1 令牌 401", st == 401)
st, _ = http("GET", "/api/overview", None, {"X-Auth-Token": CT})
chk("控制台回归(带令牌 200)", st == 200)
st, _ = http("GET", "/api/overview")
chk("控制台回归(无令牌 401)", st == 401)

print("=" * 62)
print("⑥ 第 3 步:客户端远程模式")
cfg = resolve_config("/root/.hermes")
chk("remote 默认关闭", int(cfg.get("remote_enabled") or 0) == 0)
chk("默认 key 前缀不变", s._rk("hot", "index") == "brain:hot:index", s._rk("hot", "index"))
c2 = dict(cfg); c2.update({"remote_enabled": 1, "remote_url": "http://127.0.0.1:9", "remote_token": "x"})
s2 = BrainStore(c2)
t0 = time.time(); res = s2.prefetch_block("SSH 端口", use_cache=False); dt = (time.time() - t0) * 1000
chk("远程挂掉时回落本地", res[1] > 0 and dt < 3000, f"{res[1]} 条 / {dt:.0f}ms")

print("=" * 62)
print("⑦ 检索回归(与改造前基线对比:首位应为 id=16)")
t0 = time.time(); hit = s.search_memories_hybrid("SSH 端口", limit=5)
chk("检索可用且首位 id=16", bool(hit) and hit[0]["id"] == 16,
    f"id={hit[0]['id'] if hit else '无'} / {int((time.time()-t0)*1000)}ms")

print("=" * 62)
print(f"通过 {len(PASS)} 项 · 失败 {len(FAIL)} 项")
if FAIL:
    print("失败项: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
