"""长期记忆写路径：remember / forget / 热点镜像。"""
from __future__ import annotations

import logging
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from typing import Optional
import json
import time

from ..config import (  # noqa: F401
    KEY_PREFIX,
)
from ..context import (
    RequestContext,
    owns_record,
)
from ..schema import (  # noqa: F401
    _is_duplicate_error,
)
from ..util import (  # noqa: F401
    BrainError,
    BrainForbidden,
    _clip,
    _memory_type,
)

logger = logging.getLogger("atbrains")

class MemoriesMixin:
    """长期记忆写路径：remember / forget / 热点镜像。"""

    def _rk(self, *parts) -> str:
        # redis_key_prefix 空 → KEY_PREFIX（"brain:"），键名与历史版本逐字节一致；
        # 非空 → 用它替换 "atbrains"（去尾部冒号），实现「统一 db0 + 每实例一前缀」。
        pfx = str(self.cfg.get("redis_key_prefix") or "").strip().strip(":") or KEY_PREFIX.rstrip(":")
        return pfx + ":" + ":".join(str(p) for p in parts)

    def _hot_put(self, mem_id: int, kind: str, content: str, tags: str,
                 importance: int, source: str,
                 *, machine: str = "", agent: str = "") -> bool:
        r = self.redis()
        ttl = int(self.cfg["hot_ttl_days"]) * 86400
        payload = json.dumps({
            "id": mem_id, "kind": kind, "content": _clip(content, 1000),
            "tags": tags, "importance": importance, "source": source,
            "machine": (machine or self.machine)[:48],
            "agent": agent or self.agent,
            "ts": int(time.time()),
        }, ensure_ascii=False)
        pipe = r.pipeline()
        pipe.set(self._rk("hot", mem_id), payload, ex=ttl)
        pipe.zadd(self._rk("hot", "index"), {str(mem_id): time.time()})
        pipe.zremrangebyrank(self._rk("hot", "index"), 0, -501)
        pipe.expire(self._rk("hot", "index"), ttl)
        pipe.execute()
        return True

    def _expires_at_for(self, kind: str, mtype: str) -> Optional[str]:
        """按类型算到期时间；None = 永不到期（knowledge / profile 与关闭 TTL 时）。"""
        if not int(self.cfg.get("ttl_enabled") or 0):
            return None
        days = 0
        if (str(kind or "").strip().lower() == "todo"):
            days = int(self.cfg.get("ttl_days_todo") or 0)
        elif str(mtype or "") == "episodic":
            days = int(self.cfg.get("ttl_days_episodic") or 0)
        if days <= 0:
            return None
        return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    def remember(self, content: str, *, kind: str = "note", tags: str = "",
                 importance: int = 5, source: str = "tool",
                 session_id: str = "", memory_type: str = "",
                 ctx: Optional[RequestContext] = None) -> dict[str, Any]:
        # list/dict 直接 .strip() 会抛 AttributeError（调用方只看到模糊异常）；
        # 统一转成字符串，空值仍按 BrainError 处理。
        content = str(content or "").strip()
        if not content:
            raise BrainError("content 不能为空")
        try:
            importance = max(1, min(10, int(importance)))
        except (TypeError, ValueError):
            importance = 5
        kind = (str(kind or "note").strip() or "note")[:32]
        mtype = _memory_type(kind, memory_type)
        tags = str(tags or "").strip()[:255]
        mach, ag, _role = self._identity(ctx)
        self.ensure_schema()
        mem_id = None
        mem_vals: tuple = (kind, content, tags, importance)
        deduped = False
        expires_at = self._expires_at_for(kind, mtype)
        for attempt in (0, 1):
            try:
                _, mem_id = self._exec(
                    "INSERT INTO brain_memories (kind, memory_type, content, tags, importance, "
                    "source, session_id, machine, agent, expires_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (kind, mtype, content, tags, importance, str(source or "")[:64],
                     str(session_id or "")[:96], mach, ag, expires_at),
                )
                break
            except Exception as exc:  # noqa: BLE001
                if attempt or not _is_duplicate_error(exc):
                    raise
                # 与某条活跃记忆内容完全相同（跨机器镜像/回填/手工重复）：
                # 不新建行，改为刷新既有行（重要度取大、时间前移、空分层补全）。
                row = self._exec(
                    "SELECT id FROM brain_memories WHERE active = 1 AND content_hash = MD5(%s) LIMIT 1",
                    (content,), fetch=True)
                if not row:
                    continue  # 竞态：恰被停用；重试一次插入
                mem_id = int(row[0]["id"])
                deduped = True
                self._exec(
                    "UPDATE brain_memories SET importance = GREATEST(importance, %s), "
                    "memory_type = IF(memory_type = '', %s, memory_type), "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = %s AND active = 1",
                    (importance, mtype, mem_id))
                row2 = self._exec(
                    "SELECT kind, content, tags, importance FROM brain_memories WHERE id = %s",
                    (mem_id,), fetch=True)[0]
                mem_vals = (row2["kind"], row2["content"], row2["tags"], row2["importance"])
                break
        hot_ok = False
        try:
            hot_ok = self._hot_put(int(mem_id), mem_vals[0], mem_vals[1], mem_vals[2], mem_vals[3],
                                   str(source or ""), machine=mach, agent=ag)
        except Exception as exc:  # noqa: BLE001
            logger.debug("atbrains: Redis 热点镜像失败: %s", exc)
        self._invalidate_caches(vec_dirty=True)
        self._wake_drainer()
        return {"id": mem_id, "kind": mem_vals[0], "importance": mem_vals[3],
                "memory_type": mtype, "hot_cache": hot_ok, "deduped": deduped}

    def deactivate_by_content(self, content: str, *, ctx: Optional[RequestContext] = None) -> int:
        content = str(content or "")
        if not content.strip():
            return 0
        self.ensure_schema()
        mach, ag, role = self._identity(ctx)
        if role == "admin":
            rows = self._exec(
                "SELECT id FROM brain_memories WHERE active = 1 AND content = %s", (content,), fetch=True)
        else:
            rows = self._exec(
                "SELECT id FROM brain_memories WHERE active = 1 AND content = %s "
                "AND agent = %s AND (machine = %s OR machine = '' OR machine = 'server')",
                (content, ag, mach), fetch=True)
        ids = [r["id"] for r in rows]
        if not ids:
            return 0
        marks = ", ".join(["%s"] * len(ids))
        rowcount, _ = self._exec(
            f"UPDATE brain_memories SET active = 0, deactivated_at = CURRENT_TIMESTAMP "
            f"WHERE id IN ({marks})", tuple(ids))
        try:
            r = self.redis()
            pipe = r.pipeline()
            for mid in ids:
                pipe.delete(self._rk("hot", mid))
                pipe.zrem(self._rk("hot", "index"), str(mid))
            pipe.execute()
        except Exception:  # noqa: BLE001
            pass
        self._invalidate_caches(vec_dirty=True)
        self._wake_drainer()
        return rowcount

    def forget(self, mem_id: int, *, ctx: Optional[RequestContext] = None) -> dict[str, Any]:
        self.ensure_schema()
        mid = int(mem_id)
        if ctx is not None and ctx.role != "admin":
            rows = self._exec(
                "SELECT id, machine, agent FROM brain_memories WHERE id = %s", (mid,), fetch=True)
            if rows and not owns_record(ctx, rows[0]):
                raise BrainForbidden("无权停用其他身份的记忆")
        rowcount, _ = self._exec(
            "UPDATE brain_memories SET active = 0, deactivated_at = CURRENT_TIMESTAMP "
            "WHERE id = %s AND active = 1", (mid,))
        try:
            r = self.redis()
            r.delete(self._rk("hot", mid))
            r.zrem(self._rk("hot", "index"), str(mid))
        except Exception:  # noqa: BLE001
            pass
        self._invalidate_caches(vec_dirty=True)
        self._wake_drainer()
        return {"id": mid, "deactivated": rowcount}

    def restore(self, mem_id: int, *, ctx: Optional[RequestContext] = None) -> dict[str, Any]:
        """恢复一条被停用的记忆（active=0 → 1），并补回 Redis 热点镜像与唤醒向量化。"""
        self.ensure_schema()
        mid = int(mem_id)
        rows = self._exec(
            "SELECT id, active, kind, content, tags, importance, source, machine, agent "
            "FROM brain_memories WHERE id = %s", (mid,), fetch=True)
        if not rows:
            raise BrainError(f"记忆 #{mid} 不存在")
        row = rows[0]
        if ctx is not None and ctx.role != "admin" and not owns_record(ctx, row):
            raise BrainForbidden("无权恢复其他身份的记忆")
        if row["active"]:
            return {"id": mid, "restored": 0, "note": "该条已是活跃状态"}
        dup = self._exec(
            "SELECT id FROM brain_memories WHERE active = 1 AND content_hash = MD5(%s) LIMIT 1",
            (row["content"],), fetch=True)
        if dup:
            raise BrainError(f"同内容已有活跃条目 #{dup[0]['id']}，无法恢复（先停用那条，或不再恢复）")
        rowcount, _ = self._exec(
            "UPDATE brain_memories SET active = 1, deactivated_at = NULL WHERE id = %s AND active = 0",
            (mid,))
        hot_ok = False
        try:
            hot_ok = self._hot_put(
                mid, row["kind"], row["content"], row["tags"],
                int(row["importance"]), str(row.get("source") or "restore"),
                machine=str(row.get("machine") or ""), agent=str(row.get("agent") or ""),
            )
        except Exception:  # noqa: BLE001
            pass
        self._invalidate_caches(vec_dirty=True)
        self._wake_drainer()
        return {"id": mid, "restored": rowcount, "hot_cache": hot_ok,
                "note": "向量将自动补回"}

    def list_memories(self, *, limit: int = 100, offset: int = 0, active: str = "1",
                      q: str = "", kind: str = "", machine: str = "", agent: str = "") -> dict[str, Any]:
        """分页查询记忆列表（供管理台 / admin API 使用）。"""
        self.ensure_schema()
        conds: list[tuple[str, tuple]] = []
        if str(active) in ("0", "1"):
            conds.append(("active = %s", (int(active),)))
        if q.strip():
            conds.append(("content LIKE %s", (f"%{q.strip()}%",)))
        if kind.strip():
            conds.append(("kind = %s", (kind.strip(),)))
        if machine.strip():
            conds.append(("machine = %s", (machine.strip(),)))
        if agent.strip():
            conds.append(("agent = %s", (agent.strip().lower(),)))
        where = "WHERE " + " AND ".join(c for c, _ in conds) if conds else ""
        args: list[Any] = []
        for _, vals in conds:
            args.extend(vals)
        cols = "id, kind, memory_type, content, tags, importance, source, machine, agent, active, created_at, updated_at"
        rows = self._exec(
            f"SELECT {cols} FROM brain_memories {where} ORDER BY id DESC LIMIT %s OFFSET %s",
            (*args, int(limit), int(offset)), fetch=True)
        total = self._exec(
            f"SELECT COUNT(*) AS c FROM brain_memories {where}", tuple(args), fetch=True)[0]["c"]
        return {"total": int(total or 0), "items": [dict(r) for r in rows]}

    def get_memory(self, mem_id: int) -> Optional[dict[str, Any]]:
        self.ensure_schema()
        rows = self._exec(
            "SELECT id, kind, memory_type, content, tags, importance, source, session_id, "
            "machine, agent, active, created_at, updated_at, deactivated_at "
            "FROM brain_memories WHERE id = %s", (int(mem_id),), fetch=True)
        return dict(rows[0]) if rows else None
