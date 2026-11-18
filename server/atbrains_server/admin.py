"""atbrains 服务端管理与运维查询层（不依赖任何外部 Agent CLI，直调存储引擎）。"""
from __future__ import annotations

import re
import subprocess
import time
from typing import Any, Optional

from . import PROTOCOL_VERSION, __version__
from .config import resolve_config
from .store import AtbrainsStore

CONTAINERS = {"redis": "Redis 短期层", "mariadb": "MariaDB 长期层", "qdrant": "Qdrant 向量层"}

_OVERVIEW_CACHE: dict[str, tuple[float, object]] = {}
_SECTION_TTL = {"containers": 30.0, "timer": 30.0, "qdrant": 15.0}


class AdminError(RuntimeError):
    pass


ProbeError = AdminError


def _run(cmd: list[str], timeout: int = 20) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:  # noqa: BLE001
        return ""


def docker_state() -> list[dict]:
    """可选的宿主机 Docker 容器状态探测（无 docker 命令或权限时返回空列表）。"""
    raw = _run(["docker", "ps", "-a", "--format", "{{.Names}}\t{{.Status}}\t{{.Image}}"])
    rows: list[tuple[str, str, str]] = []
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) < 3 or parts[0] not in CONTAINERS:
            continue
        rows.append((parts[0], parts[1], parts[2]))
    restarts: dict[str, str] = {}
    if rows:
        inspected = _run(["docker", "inspect", *[r[0] for r in rows],
                          "--format", "{{.Name}}\t{{.RestartCount}}"], 10)
        for line in inspected.splitlines():
            name, _, count = line.partition("\t")
            restarts[name.strip().lstrip("/")] = count.strip() or "?"
    return [
        {
            "name": name,
            "role": CONTAINERS[name],
            "status": status,
            "image": image,
            "up": status.startswith("Up"),
            "restarts": restarts.get(name, "?"),
        }
        for name, status, image in rows
    ]


def timer_state() -> dict[str, str]:
    """可选的 systemd 定时器探测（无 systemd 时返回空字典）。"""
    d: dict[str, str] = {}
    for unit, props in (
        ("brain-vector-drain.timer",
         ("ActiveState", "UnitFileState", "LastTriggerUSec", "NextElapseUSecRealtime")),
        ("brain-vector-drain.service",
         ("ExecMainStartTimestamp", "ExecMainStatus")),
    ):
        out = _run(["systemctl", "show", unit, *[x for p in props for x in ("-p", p)]], 10)
        for line in out.splitlines():
            key, _, val = line.partition("=")
            d[key.strip()] = val.strip()
    if not d:
        return {}
    d["lastRun"] = d.get("ExecMainStartTimestamp", "")
    d["lastExit"] = d.get("ExecMainStatus", "")
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


def _cached_section(key: str, fn, *, fresh: bool = False):
    now = time.monotonic()
    hit = _OVERVIEW_CACHE.get(key)
    if not fresh and hit is not None and now - hit[0] < _SECTION_TTL.get(key, 15.0):
        return hit[1]
    value = fn()
    _OVERVIEW_CACHE[key] = (now, value)
    return value


def invalidate_overview() -> None:
    _OVERVIEW_CACHE.clear()


class AdminOps:
    """绑定到一个 AtbrainsStore 的服务端运维操作集合。"""

    def __init__(self, store: Optional[AtbrainsStore] = None, *, config_home: Optional[str] = None) -> None:
        self._store = store
        self._config_home = config_home

    @property
    def store(self) -> AtbrainsStore:
        if self._store is None:
            self._store = AtbrainsStore(resolve_config(self._config_home))
        return self._store

    def overview(self, *, fresh: bool = False) -> dict[str, Any]:
        st = self.store
        out: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "version": PROTOCOL_VERSION,
            "server_version": __version__,
        }
        for key, fn in (
            ("stats", lambda: st.stats(force=fresh)),
            ("ping", lambda: st.ping()),
            ("vector", lambda: st.vector_status(force=fresh)),
        ):
            try:
                out[key] = fn()
            except Exception as exc:  # noqa: BLE001
                out[key] = {"error": f"{type(exc).__name__}: {exc}"}
        out["containers"] = _cached_section("containers", docker_state, fresh=fresh)
        out["timer"] = _cached_section("timer", timer_state, fresh=fresh)
        out["qdrant"] = _cached_section("qdrant", st.qdrant_info, fresh=fresh)
        return out

    def memories(self, limit: int = 100, offset: int = 0, active: str = "1",
                 q: str = "", kind: str = "", machine: str = "", agent: str = "") -> dict[str, Any]:
        return self.store.list_memories(
            limit=limit, offset=offset, active=active, q=q, kind=kind, machine=machine, agent=agent)

    def memory_detail(self, mem_id: int) -> dict[str, Any]:
        return {"item": self.store.get_memory(mem_id)}

    def turns(self, limit: int = 40, offset: int = 0, machine: str = "",
              session_id: str = "", q: str = "") -> dict[str, Any]:
        return self.store.list_turns(
            limit=limit, offset=offset, machine=machine, session_id=session_id, q=q)

    def turn_detail(self, turn_id: int) -> dict[str, Any]:
        return {"item": self.store.get_turn(turn_id)}

    def sessions(self, limit: int = 100, offset: int = 0, machine: str = "", q: str = "") -> dict[str, Any]:
        return self.store.list_sessions(limit=limit, offset=offset, machine=machine, q=q)

    def search(self, q: str, limit: int = 10) -> dict[str, Any]:
        st = self.store
        if not q.strip():
            return {"query": q, "memories": [], "turns": []}
        try:
            mem = st.search_memories_hybrid(q, limit=limit)
        except Exception:  # noqa: BLE001
            mem = st.search_memories(q, limit=limit)
        return {"query": q, "memories": mem, "turns": st.search_turns(q, limit=min(limit, 10))}

    def recent(self, limit: int = 20) -> dict[str, Any]:
        st = self.store
        return {"hot": st.hot_recent(limit), "turns": st.recent_turns(limit)}

    def remember(self, content: str, kind: str = "note", tags: str = "", importance: int = 5) -> dict[str, Any]:
        if not str(content or "").strip():
            raise AdminError("content 不能为空")
        res = self.store.remember(content, kind=kind, tags=tags, importance=int(importance))
        invalidate_overview()
        return res

    def forget(self, memory_id: int) -> dict[str, Any]:
        res = self.store.forget(int(memory_id))
        invalidate_overview()
        return res

    def restore(self, memory_id: int) -> dict[str, Any]:
        res = self.store.restore(int(memory_id))
        invalidate_overview()
        return res

    def drain(self, force: bool = False, max_items: int = 0) -> dict[str, Any]:
        res = self.store.drain_vectors(max_items=max_items, force=bool(force))
        invalidate_overview()
        return {"ok": True, **res}

    def doctor(self) -> dict[str, Any]:
        res = self.store.doctor()
        invalidate_overview()
        return res

    def prune(self, dry_run: bool = True) -> dict[str, Any]:
        res = self.store.prune(dry_run=bool(dry_run))
        if not dry_run:
            invalidate_overview()
        return res

    def qdrant_info(self) -> dict[str, Any]:
        return self.store.qdrant_info()
