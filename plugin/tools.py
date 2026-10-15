"""atbrains agent 工具 —— 让 agent 直接监控/控制记忆系统,不必开网页。

两个工具:
  · atbrains_status  —— 一屏总览(三层计数、容器健康、向量覆盖、定时器)
  · atbrains_control —— 动作:回填向量 / 体检 / 新增记忆 / 停用记忆 / 恢复记忆 / 清理预演 / 查记忆
"""
from __future__ import annotations

import json
from typing import Any

from . import probe


def _s(desc: str) -> dict[str, Any]:
    return {"type": "string", "description": desc}


def _schema(name: str, description: str, properties: dict[str, Any], required=None) -> dict[str, Any]:
    params: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        params["required"] = required
    params["additionalProperties"] = False
    return {"name": name, "description": description, "parameters": params}


STATUS_SCHEMA = _schema(
    "atbrains_status",
    "Read the atbrains memory system status: MariaDB long-term store (memories/turns/sessions), "
    "Redis short-term store (keys/hot index), Qdrant vector store (points/coverage), the three "
    "Docker containers' health, and the vector-drain timer's last run. Use this to answer "
    "'is the memory system healthy?' or to diagnose missing recall.",
    {}, [],
)

CONTROL_SCHEMA = _schema(
    "atbrains_control",
    "Control the atbrains memory system: backfill vectors (drain), run a health check (doctor), "
    "write a memory (remember), deactivate a memory (forget), reactivate a deactivated memory "
    "(restore), preview archival (prune), or read recent memories/turns (recent). Writes go to the "
    "long-term MariaDB store plus the Redis hot index; new memories get vectorized by the timer "
    "within ~3 minutes (or call drain to do it now).",
    {
        "action": {"type": "string",
                   "enum": ["drain", "doctor", "remember", "forget", "restore", "prune", "recent"],
                   "description": "Which control action to run."},
        "content": _s("For action=remember: the memory text."),
        "kind": {"type": "string", "enum": ["fact", "preference", "decision", "note", "todo"],
                 "description": "For action=remember: memory category (default note)."},
        "tags": _s("For action=remember: comma-separated tags."),
        "importance": {"type": "integer", "minimum": 1, "maximum": 10,
                       "description": "For action=remember: importance 1-10 (default 5)."},
        "id": {"type": "integer", "description": "For action=forget / action=restore: the memory id."},
        "dry_run": {"type": "boolean", "description": "For action=prune: preview only (default true)."},
        "limit": {"type": "integer", "description": "For action=recent: how many entries (default 10)."},
    },
    ["action"],
)


def atbrains_status(args: dict | None = None, **_) -> str:
    try:
        d = probe.overview()
    except Exception as e:
        return json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)
    st = d.get("stats", {}) if isinstance(d.get("stats"), dict) else {}
    q = d.get("qdrant", {})
    cov = q.get("coverage", {})
    tm = d.get("timer", {})
    cov_ok = cov.get("with_vector") is not None and cov.get("with_vector") == cov.get("active_memories")
    return json.dumps({
        "ok": True, "ts": d.get("ts"),
        "mariadb": st.get("mysql", st.get("error")),
        "redis": st.get("redis", {}),
        "qdrant": {"points": q.get("points"), "dim": q.get("dim"), "status": q.get("status") or q.get("error"),
                   "coverage": {"with_vector": cov.get("with_vector"),
                                "active_memories": cov.get("active_memories"),
                                "missing": len(cov.get("missing", [])),
                                "stale": len(cov.get("stale_vectors", [])),
                                "ok": cov_ok}},
        "vector": d.get("vector"),
        "containers": {c["name"]: {"up": c["up"], "status": c["status"], "restarts": c["restarts"]}
                       for c in d.get("containers", [])},
        "timer": {"active": tm.get("ActiveState"), "enabled": tm.get("UnitFileState"),
                  "lastRun": tm.get("lastRun"), "lastExit": tm.get("lastExit")},
    }, ensure_ascii=False, default=str)


def atbrains_control(args: dict | None = None, **_) -> str:
    a = args or {}
    action = (a.get("action") or "").strip()
    try:
        if action == "drain":
            r = probe.drain()
            return json.dumps({"ok": r["ok"], "action": action, "exit": r["code"],
                               "output": (r["out"] or r["err"])[-1500:]}, ensure_ascii=False)
        if action == "doctor":
            r = probe.doctor()
            return json.dumps({"ok": r["ok"], "action": action,
                               "output": (r["out"] or r["err"])[-2500:]}, ensure_ascii=False)
        if action == "remember":
            content = (a.get("content") or "").strip()
            if not content:
                return json.dumps({"ok": False, "error": "action=remember 需要 content"}, ensure_ascii=False)
            r = probe.remember(content, kind=a.get("kind") or "note",
                               tags=a.get("tags") or "", importance=int(a.get("importance") or 5))
            return json.dumps({"ok": True, "action": action, "saved": r,
                               "note": "向量由定时器在 3 分钟内补上,或立刻调 drain"}, ensure_ascii=False, default=str)
        if action == "forget":
            if a.get("id") is None:
                return json.dumps({"ok": False, "error": "action=forget 需要 id"}, ensure_ascii=False)
            return json.dumps({"ok": True, "action": action, "result": probe.forget(int(a["id"]))},
                              ensure_ascii=False, default=str)
        if action == "restore":
            if a.get("id") is None:
                return json.dumps({"ok": False, "error": "action=restore 需要 id"}, ensure_ascii=False)
            try:
                return json.dumps({"ok": True, "action": action, "result": probe.restore(int(a["id"]))},
                                  ensure_ascii=False, default=str)
            except Exception as e:  # noqa: BLE001 —— 例如同内容已有活跃条目
                return json.dumps({"ok": False, "action": action, "error": str(e)}, ensure_ascii=False)
        if action == "prune":
            return json.dumps({"ok": True, "action": action,
                               "result": probe.prune(dry_run=bool(a.get("dry_run", True)))},
                              ensure_ascii=False, default=str)
        if action == "recent":
            n = min(int(a.get("limit") or 10), 50)
            d = probe.recent(n)
            return json.dumps({"ok": True, "action": action, "hot": d.get("hot"), "turns": d.get("turns")},
                              ensure_ascii=False, default=str)
        return json.dumps({"ok": False, "error": f"未知 action: {action!r}"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"ok": False, "action": action, "error": f"{type(e).__name__}: {e}"},
                          ensure_ascii=False)
