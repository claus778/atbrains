"""atbrains 数据层 —— 记忆系统状态的唯一采集入口。

CLI(`hermes atbrains ...`)、agent 工具(`atbrains_status` / `atbrains_control`)
和网页控制台共用这里的函数,保证三个界面看到的数据永远一致(不漂移)。

数据来源:
  · brain 插件(兄弟目录 ../brain)的 core.BrainStore —— 读/写 MariaDB + Redis
  · Qdrant HTTP API(取 api_key 自其配置文件)
  · docker / systemctl —— 容器与定时器状态
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
BRAIN_PLUGIN = HERE.parent / "brain"
QDRANT_CONF = Path("/root/qdrant/qdrant-config/production.yaml")
QDRANT_URL = "http://127.0.0.1:6333"
QDRANT_COLLECTION = "brain_memories"
CONTAINERS = {"redis": "Redis 短期层", "mariadb": "MariaDB 长期层", "qdrant": "Qdrant 向量层"}

_store = None


class ProbeError(RuntimeError):
    pass


def hermes_home() -> str:
    return os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")


def store():
    """惰性拿到 brain 插件的 BrainStore(复用其凭据解析与读写逻辑)。"""
    global _store
    if _store is None:
        if not (BRAIN_PLUGIN / "core.py").exists():
            raise ProbeError(f"brain 插件不存在:{BRAIN_PLUGIN}/core.py —— 先装并启用 brain 插件")
        if str(BRAIN_PLUGIN) not in sys.path:
            sys.path.insert(0, str(BRAIN_PLUGIN))
        from core import BrainStore, resolve_config  # type: ignore
        _store = BrainStore(resolve_config(hermes_home()))
    return _store


def _run(cmd: list[str], timeout: int = 20) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


# ───────────────────────────── 基础设施 ─────────────────────────────

def docker_state() -> list[dict]:
    raw = _run(["docker", "ps", "-a", "--format", "{{.Names}}\t{{.Status}}\t{{.Image}}"])
    out = []
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) < 3 or parts[0] not in CONTAINERS:
            continue
        name, status, image = parts
        restarts = _run(["docker", "inspect", name, "--format", "{{.RestartCount}}"], 10).strip() or "?"
        out.append({"name": name, "role": CONTAINERS[name], "status": status, "image": image,
                    "up": status.startswith("Up"), "restarts": restarts})
    return out


def timer_state() -> dict:
    d = {}
    for prop in ("ActiveState", "UnitFileState", "LastTriggerUSec", "NextElapseUSecRealtime"):
        d[prop] = _run(["systemctl", "show", "brain-vector-drain.timer", "-p", prop, "--value"], 10).strip()
    d["lastRun"] = _run(["systemctl", "show", "brain-vector-drain.service",
                         "-p", "ExecMainStartTimestamp", "--value"], 10).strip()
    d["lastExit"] = _run(["systemctl", "show", "brain-vector-drain.service",
                          "-p", "ExecMainStatus", "--value"], 10).strip()
    return d


def qdrant_key() -> str:
    try:
        for line in QDRANT_CONF.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"\s*api_key:\s*(\S+)", line)
            if m:
                return m.group(1).strip("\"'")
    except FileNotFoundError:
        pass
    return ""


def qdrant_info() -> dict:
    res = {"url": QDRANT_URL, "collection": QDRANT_COLLECTION}
    key = qdrant_key()
    try:
        req = urllib.request.Request(f"{QDRANT_URL}/collections/{QDRANT_COLLECTION}",
                                     headers={"api-key": key})
        with urllib.request.urlopen(req, timeout=10) as r:
            rr = json.loads(r.read().decode())["result"]
        params = rr.get("config", {}).get("params", {}).get("vectors", {})
        res.update({"status": rr.get("status"), "optimizer": rr.get("optimizer_status"),
                    "points": rr.get("points_count"), "indexed": rr.get("indexed_vectors_count"),
                    "segments": rr.get("segments_count"),
                    "dim": params.get("size"), "distance": params.get("distance")})
        req2 = urllib.request.Request(
            f"{QDRANT_URL}/collections/{QDRANT_COLLECTION}/points/scroll",
            data=json.dumps({"limit": 10000, "with_payload": False, "with_vector": False}).encode(),
            headers={"api-key": key, "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req2, timeout=20) as r2:
            ids = {int(p["id"]) for p in json.loads(r2.read().decode())["result"]["points"]}
        rows = store()._exec("SELECT id, active FROM brain_memories", fetch=True)
        active = {int(x["id"]) for x in rows if x["active"]}
        res["coverage"] = {"active_memories": len(active), "with_vector": len(active & ids),
                           "missing": sorted(active - ids)[:50], "stale_vectors": sorted(ids - active)[:50]}
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {e}"
    return res


# ───────────────────────────── 总览 ─────────────────────────────

def overview() -> dict:
    out = {"ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    for key, fn in (("stats", lambda: store().stats()), ("ping", lambda: store().ping()),
                    ("vector", lambda: store().vector_status())):
        try:
            out[key] = fn()
        except Exception as e:
            out[key] = {"error": f"{type(e).__name__}: {e}"}
    out["containers"] = docker_state()
    out["timer"] = timer_state()
    out["qdrant"] = qdrant_info()
    return out


# ───────────────────────────── 查询 ─────────────────────────────

_MEM_COLS = ("id,kind,content,tags,importance,source,machine,active,created_at,updated_at")


def memories(limit: int = 100, offset: int = 0, active: str = "1", q: str = "") -> dict:
    st = store()
    where, args = "", []
    if active in ("0", "1"):
        where = "WHERE active = %s"
        args.append(int(active))
    if q.strip():
        where = (where + " AND" if where else "WHERE") + " content LIKE %s"
        args.append(f"%{q.strip()}%")
    rows = st._exec(f"SELECT {_MEM_COLS} FROM brain_memories {where} ORDER BY id DESC LIMIT %s OFFSET %s",
                    (*args, limit, offset), fetch=True)
    total = st._exec(f"SELECT COUNT(*) c FROM brain_memories {where}", tuple(args), fetch=True)[0]["c"]
    return {"total": total, "items": rows}


def turns(limit: int = 40) -> dict:
    st = store()
    rows = st._exec("SELECT id,session_id,platform,machine,ts,LEFT(user_content,200) u,"
                    "LEFT(assistant_content,200) a FROM brain_turns ORDER BY id DESC LIMIT %s",
                    (limit,), fetch=True)
    total = st._exec("SELECT COUNT(*) c FROM brain_turns", fetch=True)[0]["c"]
    return {"total": total, "items": rows}


def search(q: str, limit: int = 10) -> dict:
    st = store()
    if not q.strip():
        return {"query": q, "memories": [], "turns": []}
    try:
        mem = st.search_memories_hybrid(q, limit=limit)
    except Exception:
        mem = st.search_memories(q, limit=limit)
    return {"query": q, "memories": mem, "turns": st.search_turns(q, limit=min(limit, 10))}


def recent(limit: int = 20) -> dict:
    st = store()
    return {"hot": st.hot_recent(limit), "turns": st.recent_turns(limit)}


# ───────────────────────────── 控制 ─────────────────────────────

def remember(content: str, kind: str = "note", tags: str = "", importance: int = 5) -> dict:
    if not content.strip():
        raise ProbeError("content 不能为空")
    return store().remember(content, kind=kind, tags=tags, importance=int(importance))


def forget(memory_id: int) -> dict:
    return store().forget(int(memory_id))


def drain(force: bool = False) -> dict:
    cmd = ["hermes", "brain", "vector-drain"] + (["--force"] if force else [])
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800,
                       env={**os.environ, "HERMES_HOME": hermes_home()})
    return {"ok": r.returncode == 0, "code": r.returncode,
            "out": (r.stdout or "")[-3000:], "err": (r.stderr or "")[-1000:]}


def doctor() -> dict:
    r = subprocess.run(["hermes", "brain", "doctor"], capture_output=True, text=True, timeout=180,
                       env={**os.environ, "HERMES_HOME": hermes_home()})
    return {"ok": r.returncode == 0, "out": r.stdout, "err": r.stderr}


def prune(dry_run: bool = True) -> dict:
    return store().prune(dry_run=bool(dry_run))
