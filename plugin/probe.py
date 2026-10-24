"""atbrains 数据层 —— 记忆系统状态的唯一采集入口。

CLI(`hermes atbrains ...`)、agent 工具(`atbrains_status` / `atbrains_control`)
和网页控制台共用这里的函数,保证三个界面看到的数据永远一致(不漂移)。

数据来源:
  · atbrains-memory 插件(兄弟目录 ../atbrains-memory)的 core.AtbrainsStore —— 读/写 MariaDB + Redis
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
QDRANT_CONF = Path("/root/qdrant/qdrant-config/production.yaml")
QDRANT_URL = "http://127.0.0.1:6333"
QDRANT_COLLECTION = "brain_memories"
CONTAINERS = {"redis": "Redis 短期层", "mariadb": "MariaDB 长期层", "qdrant": "Qdrant 向量层"}

_store = None


class ProbeError(RuntimeError):
    pass


def hermes_home() -> str:
    return os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")


def _memory_plugin_candidates() -> list[Path]:
    """记忆引擎插件的候选位置,按优先级排序。

    正常布局 = 与 atbrains 控制台并排(同在 plugins/ 下,新名 atbrains-memory);
    兼容旧安装名 brain。也可用 ATBRAINS_MEMORY_PLUGIN 显式指定。
    """
    cands: list[Path] = []
    for var in ("ATBRAINS_MEMORY_PLUGIN", "ATBRAINS_BRAIN_PLUGIN"):
        env = os.environ.get(var)
        if env:
            cands.append(Path(env))
    for name in ("atbrains-memory", "brain"):
        cands.append(HERE.parent / name)
        cands.append(Path(hermes_home()) / "plugins" / name)
    return cands


def _find_memory_plugin() -> Path:
    for c in _memory_plugin_candidates():
        if (c / "core.py").exists():
            return c
    tried = "、".join(str(c) for c in _memory_plugin_candidates())
    raise ProbeError(f"atbrains-memory 插件不存在(找过:{tried})—— 先安装并启用记忆插件")


def store():
    """惰性拿到记忆插件的 AtbrainsStore(复用其凭据解析与读写逻辑)。"""
    global _store
    if _store is None:
        plugin_dir = _find_memory_plugin()
        if str(plugin_dir) not in sys.path:
            sys.path.insert(0, str(plugin_dir))
        from core import AtbrainsStore, resolve_config  # type: ignore
        _store = AtbrainsStore(resolve_config(hermes_home()))
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
    # 下次运行:show 的 NextElapseUSecRealtime 在本机 systemd 上取不到,改从 list-timers 解析
    d["nextRun"] = ""
    try:
        out = _run(["systemctl", "list-timers", "brain-vector-drain.timer", "--no-pager"], 10)
        for line in out.splitlines():
            m = re.search(r"([A-Za-z]{3}\s+\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\s+\S+)", line)
            if m:
                d["nextRun"] = m.group(1)
                break
    except Exception:  # noqa: BLE001
        pass
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


def plugin_version() -> str:
    """本插件版本(读 plugin.yaml)。"""
    try:
        m = re.search(r"^version:\s*[\"']?([\w.\-]+)", (HERE / "plugin.yaml").read_text(), re.M)
        return m.group(1) if m else "?"
    except Exception:
        return "?"


# ───────────────────────────── 总览 ─────────────────────────────

def overview() -> dict:
    out = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "version": plugin_version()}
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

_MEM_COLS = ("id,kind,content,tags,importance,source,machine,agent,active,created_at,updated_at")


def _where(conds: list[tuple[str, tuple]]) -> tuple[str, list]:
    """把 [(条件 SQL, 参数元组), ...] 拼成 WHERE 子句 + 参数列表。"""
    if not conds:
        return "", []
    args: list = []
    for _, vals in conds:
        args.extend(vals)
    return "WHERE " + " AND ".join(c for c, _ in conds), args


def memories(limit: int = 100, offset: int = 0, active: str = "1", q: str = "",
             kind: str = "", machine: str = "", agent: str = "") -> dict:
    st = store()
    conds: list[tuple[str, tuple]] = []
    if active in ("0", "1"):
        conds.append(("active = %s", (int(active),)))
    if q.strip():
        conds.append(("content LIKE %s", (f"%{q.strip()}%",)))
    if kind.strip():
        conds.append(("kind = %s", (kind.strip(),)))
    if machine.strip():
        conds.append(("machine = %s", (machine.strip(),)))
    if agent.strip():
        conds.append(("agent = %s", (agent.strip().lower(),)))
    where, args = _where(conds)
    rows = st._exec(f"SELECT {_MEM_COLS} FROM brain_memories {where} ORDER BY id DESC LIMIT %s OFFSET %s",
                    (*args, limit, offset), fetch=True)
    total = st._exec(f"SELECT COUNT(*) c FROM brain_memories {where}", tuple(args), fetch=True)[0]["c"]
    return {"total": total, "items": rows}


def memory_detail(mem_id: int) -> dict:
    rows = store()._exec(
        "SELECT id,kind,content,tags,importance,source,session_id,machine,agent,active,"
        "created_at,updated_at,deactivated_at FROM brain_memories WHERE id = %s",
        (int(mem_id),), fetch=True)
    return {"item": rows[0] if rows else None}


def turns(limit: int = 40, offset: int = 0, machine: str = "", session_id: str = "", q: str = "") -> dict:
    st = store()
    conds: list[tuple[str, tuple]] = []
    if machine.strip():
        conds.append(("machine = %s", (machine.strip(),)))
    if session_id.strip():
        conds.append(("session_id = %s", (session_id.strip(),)))
    if q.strip():
        conds.append(("(user_content LIKE %s OR assistant_content LIKE %s)",
                      (f"%{q.strip()}%", f"%{q.strip()}%")))
    where, args = _where(conds)
    rows = st._exec("SELECT id,session_id,platform,machine,ts,truncated,"
                    "LEFT(user_content,200) u, LEFT(assistant_content,200) a "
                    f"FROM brain_turns {where} ORDER BY id DESC LIMIT %s OFFSET %s",
                    (*args, limit, offset), fetch=True)
    total = st._exec(f"SELECT COUNT(*) c FROM brain_turns {where}", tuple(args), fetch=True)[0]["c"]
    return {"total": total, "items": rows}


def turn_detail(turn_id: int) -> dict:
    rows = store()._exec(
        "SELECT id,session_id,platform,author,machine,truncated,user_content,assistant_content,ts "
        "FROM brain_turns WHERE id = %s", (int(turn_id),), fetch=True)
    return {"item": rows[0] if rows else None}


def sessions(limit: int = 100, offset: int = 0, machine: str = "", q: str = "") -> dict:
    st = store()
    conds: list[tuple[str, tuple]] = []
    if machine.strip():
        conds.append(("machine = %s", (machine.strip(),)))
    if q.strip():
        conds.append(("session_id LIKE %s", (f"%{q.strip()}%",)))
    where, args = _where(conds)
    rows = st._exec("SELECT session_id,platform,machine,started_at,last_active,turn_count,note "
                    f"FROM brain_sessions {where} ORDER BY last_active DESC LIMIT %s OFFSET %s",
                    (*args, limit, offset), fetch=True)
    total = st._exec(f"SELECT COUNT(*) c FROM brain_sessions {where}", tuple(args), fetch=True)[0]["c"]
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


def restore(memory_id: int) -> dict:
    """恢复一条被停用的记忆(active=0 → 1),并补回 Redis 热点镜像。

    与 forget 对偶;热点镜像走 store 的 _hot_put 规则(与 atbrains 工具侧行为一致)。
    向量由定时器 ≤3 分钟自动补;同内容已有活跃条目时拒绝(库有唯一键防重)。
    """
    st = store()
    rows = st._exec("SELECT id,active,kind,content,tags,importance,source "
                    "FROM brain_memories WHERE id = %s", (int(memory_id),), fetch=True)
    if not rows:
        raise ProbeError(f"记忆 #{memory_id} 不存在")
    row = rows[0]
    if row["active"]:
        return {"id": int(memory_id), "restored": 0, "note": "该条已是活跃状态"}
    dup = st._exec("SELECT id FROM brain_memories WHERE active = 1 AND content_hash = MD5(%s) LIMIT 1",
                   (row["content"],), fetch=True)
    if dup:
        raise ProbeError(f"同内容已有活跃条目 #{dup[0]['id']},无法恢复(先停用那条,或不再恢复)")
    rowcount, _ = st._exec(
        "UPDATE brain_memories SET active = 1, deactivated_at = NULL WHERE id = %s AND active = 0",
        (int(memory_id),))
    hot_ok = False
    try:
        hot_ok = st._hot_put(int(memory_id), row["kind"], row["content"], row["tags"],
                             int(row["importance"]), str(row.get("source") or "restore"))
    except Exception:  # noqa: BLE001 —— 热点是缓存,失败不影响恢复本身
        pass
    try:
        st._vec_wake.set()   # 通知本进程 drainer(有的话)尽快向量化
    except Exception:  # noqa: BLE001
        pass
    return {"id": int(memory_id), "restored": rowcount, "hot_cache": hot_ok,
            "note": "向量由定时器 ≤3 分钟自动补回"}


def drain(force: bool = False) -> dict:
    cmd = ["hermes", "atbrains-memory", "vector-drain"] + (["--force"] if force else [])
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800,
                       env={**os.environ, "HERMES_HOME": hermes_home()})
    return {"ok": r.returncode == 0, "code": r.returncode,
            "out": (r.stdout or "")[-3000:], "err": (r.stderr or "")[-1000:]}


def doctor() -> dict:
    r = subprocess.run(["hermes", "atbrains-memory", "doctor"], capture_output=True, text=True, timeout=180,
                       env={**os.environ, "HERMES_HOME": hermes_home()})
    return {"ok": r.returncode == 0, "out": r.stdout, "err": r.stderr}


def prune(dry_run: bool = True) -> dict:
    return store().prune(dry_run=bool(dry_run))
