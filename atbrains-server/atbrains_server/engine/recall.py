"""读路径与召回组装（关键词检索、prefetch_block、注入文本）。"""
from __future__ import annotations

import logging
from typing import Any
from typing import Optional
import hashlib
import json
import re

from ..context import (
    RequestContext,
    owns_record,
)
from ..util import (  # noqa: F401
    _cfg_flag,
    _clip,
    _esc_like,
    _regexp_pattern,
    _regexp_target,
    _utc_epoch,
    search_terms,
)

logger = logging.getLogger("atbrains")

class RecallMixin:
    """读路径与召回组装（关键词检索、prefetch_block、注入文本）。"""

    def _search_where(self, columns: tuple[str, ...], terms: list[str]) -> tuple[str, list]:
        if _cfg_flag(self.cfg, "search_regexp_enabled", 1):
            pattern = _regexp_pattern(terms)
            # 一次 REGEXP 扫完整列集合；无有效词时回退下面的旧 SQL（行为完全不变）。
            if pattern:
                return f"{_regexp_target(columns)} REGEXP %s", [pattern]
        clauses, params = [], []
        for t in terms:
            sub = " OR ".join(f"{c} LIKE %s" for c in columns)
            clauses.append(f"({sub})")
            params.extend([f"%{_esc_like(t)}%"] * len(columns))
        return " OR ".join(clauses), params

    def search_memories(self, query: str, *, limit: int = 5,
                        kind: Optional[str] = None,
                        agent: Optional[str] = None) -> list[dict[str, Any]]:
        terms = search_terms(query)
        q_clean = " ".join(str(query or "").split())
        if not terms and len(q_clean) < 4:
            return []
        where, params = self._search_where(("content", "tags"), terms or [q_clean])
        sql = (
            "SELECT id, kind, memory_type, content, tags, importance, source, session_id, machine, agent, "
            "created_at, updated_at, access_count, last_accessed_at, expires_at "
            f"FROM brain_memories WHERE active = 1 AND superseded_by IS NULL AND ({where}) "
        )
        if kind:
            sql += "AND kind = %s "
            params.append(kind)
        if agent:
            sql += "AND agent = %s "
            params.append(str(agent).strip().lower()[:32])
        if len(q_clean) >= 4:
            sql += "ORDER BY (content LIKE %s) DESC, importance DESC, updated_at DESC, id DESC LIMIT %s"
            params.append(f"%{_esc_like(q_clean)}%")
        else:
            sql += "ORDER BY importance DESC, updated_at DESC, id DESC LIMIT %s"
        params.append(int(limit))
        self.ensure_schema()
        return list(self._exec(sql, tuple(params), fetch=True))

    def search_turns(self, query: str, *, limit: int = 3,
                     ctx: Optional[RequestContext] = None) -> list[dict[str, Any]]:
        terms = search_terms(query)
        if not terms:
            return []
        where, params = self._search_where(("user_content", "assistant_content"), terms)
        sql = (
            "SELECT id, session_id, platform, machine, agent, user_content, assistant_content, ts "
            f"FROM brain_turns WHERE ({where}) "
        )
        mach, ag, role = self._identity(ctx)
        if role != "admin":
            sql += "AND (agent = %s OR (agent = '' AND machine = %s)) "
            params.extend([ag, mach])
        window_days = int(self.cfg.get("search_turns_days") or 0)
        if window_days > 0:
            sql += "AND ts >= (NOW() - INTERVAL %s DAY) "
            params.append(window_days)
        sql += "ORDER BY ts DESC, id DESC LIMIT %s"
        params.append(int(limit))
        self.ensure_schema()
        return list(self._exec(sql, tuple(params), fetch=True))

    def recent_turns(self, limit: int = 10, *, source: str = "redis",
                     ctx: Optional[RequestContext] = None) -> list[dict[str, Any]]:
        mach, ag, role = self._identity(ctx)
        want_limit = max(1, int(limit))
        if source == "redis":
            try:
                fetch_n = want_limit if role == "admin" else min(200, max(want_limit * 4, 40))
                entries = self.redis().xrevrange(self._rk("recent"), count=fetch_n)
                out = []
                for entry_id, f in entries:
                    if role != "admin" and not owns_record(ctx, f):
                        continue
                    out.append({
                        "stream_id": entry_id,
                        "session": f.get("sid", ""),
                        "platform": f.get("platform", ""),
                        "author": f.get("author", ""),
                        "machine": f.get("machine", ""),
                        "agent": f.get("agent", ""),
                        "user": f.get("user", ""),
                        "assistant": f.get("assistant", ""),
                        "ts": float(f.get("ts") or 0),
                    })
                    if len(out) >= want_limit:
                        break
                return out
            except Exception as exc:  # noqa: BLE001
                logger.debug("atbrains: Redis 最近对话读取失败，回退 MariaDB: %s", exc)
                source = "db"
        self.ensure_schema()
        if role == "admin":
            rows = self._exec(
                "SELECT id, session_id, platform, author, machine, agent, user_content, assistant_content, ts "
                "FROM brain_turns ORDER BY ts DESC LIMIT %s", (want_limit,), fetch=True)
        else:
            rows = self._exec(
                "SELECT id, session_id, platform, author, machine, agent, user_content, assistant_content, ts "
                "FROM brain_turns WHERE (agent = %s OR (agent = '' AND machine = %s)) "
                "ORDER BY ts DESC LIMIT %s", (ag, mach, want_limit), fetch=True)
        return [{
            "stream_id": str(r["id"]), "session": r["session_id"], "platform": r["platform"],
            "author": r.get("author", ""), "machine": r.get("machine", ""),
            "agent": r.get("agent", ""), "user": r["user_content"],
            # 库内 DATETIME 是 UTC 口径；naive .timestamp() 会按本机时区解释
            "assistant": r["assistant_content"], "ts": _utc_epoch(r["ts"]) or 0,
        } for r in rows]

    def hot_recent(self, limit: int = 5) -> list[dict[str, Any]]:
        try:
            r = self.redis()
            ids = r.zrevrange(self._rk("hot", "index"), 0, max(0, int(limit) - 1))
            if not ids:
                return []
            # 一次 MGET 取代 N 次 GET（控制台「动态」页每 5 秒轮询，N 可达 100）
            vals = r.mget([self._rk("hot", mid) for mid in ids])
            out = []
            for raw in vals or []:
                if not raw:
                    continue
                try:
                    out.append(json.loads(raw))
                except ValueError:
                    continue
            return out
        except Exception:  # noqa: BLE001
            return []

    @staticmethod
    def format_hits(mems: list[dict[str, Any]], limit: int = 20) -> list[dict[str, Any]]:
        """把 search_memories_hybrid 的原始命中裁成 /v1 items 形状（可随查询缓存）。"""
        out: list[dict[str, Any]] = []
        for m in list(mems or [])[: max(1, int(limit))]:
            if not isinstance(m, dict):
                continue
            vscore = m.get("vscore")
            preview = m.get("preview")
            if preview is None:
                preview = m.get("content")
            out.append({
                "id": m.get("id"),
                "kind": m.get("kind"),
                "memory_type": m.get("memory_type"),
                "importance": m.get("importance"),
                "machine": m.get("machine"),
                "agent": m.get("agent"),
                "tags": m.get("tags") or "",
                "via": m.get("via") or "",
                "vscore": round(float(vscore), 4) if isinstance(vscore, (int, float)) else None,
                "preview": str(preview or "")[:240],
            })
        return out

    def prefetch_block(self, query: str, *, session_id: str = "",
                       use_cache: bool = True,
                       with_hits: bool = False,
                       with_turns: bool = False,
                       limit: Optional[int] = None,
                       kind: Optional[str] = None,
                       agent: Optional[str] = None,
                       include_turns: bool = True,
                       ctx: Optional[RequestContext] = None):
        """组装本轮注入文本；结果带版本号 (qver) 缓存进 Redis，写入后 O(1) 自动失效。"""
        query = (query or "").strip()
        if not query:
            if with_turns:
                return "", 0, [], []
            return ("", 0, []) if with_hits else ("", 0)
        eff_limit = max(1, min(int(limit or self.cfg["prefetch_memories"]), 20))
        norm = re.sub(r"[\s\W_]+", "", query.lower(), flags=re.UNICODE)
        raw_key = ("n:" + norm) if (norm and norm != query) else query
        mach, ag, role = self._identity(ctx)
        turn_scope = "admin" if role == "admin" else f"{ag}|{mach}"
        cache_scope = (
            f"{self._qcache_ver}\x00{raw_key}\x00{eff_limit}\x00{kind or ''}\x00"
            f"{agent or ''}\x00{int(bool(include_turns))}\x00{turn_scope}"
        )
        cache_key = self._rk("qcache", hashlib.sha1(cache_scope.encode("utf-8")).hexdigest())
        qver = 0
        if use_cache:
            try:
                ver_raw, raw = self.redis().mget([self._rk("qver"), cache_key])
                qver = int(ver_raw or 0)
                if raw:
                    data = json.loads(raw)
                    if int(data.get("qver") or 0) == qver:
                        text, count = str(data.get("text", "")), int(data.get("count", 0))
                        hits = list(data.get("hits") or [])
                        turns_cached = list(data.get("turns") or [])
                        if with_turns:
                            return text, count, hits, turns_cached
                        if with_hits:
                            return text, count, hits
                        return text, count
            except Exception:  # noqa: BLE001
                pass
        mems = self.search_memories_hybrid(query, limit=eff_limit, kind=kind, agent=agent)
        turns = self.search_turns(query, limit=int(self.cfg["prefetch_turns"]), ctx=ctx) if include_turns else []
        text = self._compose(mems, turns)
        count = len(mems) + len(turns)
        hits = self.format_hits(mems, eff_limit)
        self._bump_access([int(m["id"]) for m in mems if m.get("id")])
        self._write_qcache(cache_key, text, count, hits, turns=turns, qver=qver)
        if with_turns:
            return text, count, hits, turns
        if with_hits:
            return text, count, hits
        return text, count

    def top_memories(self, limit: int = 8, *, kind: Optional[str] = None,
                     agent: Optional[str] = None) -> list[dict[str, Any]]:
        """无焦点检索词时的重点记忆列表：高重要度 + 最近更新。"""
        self.ensure_schema()
        sql = ("SELECT id, kind, memory_type, content, tags, importance, agent, machine, updated_at "
               "FROM brain_memories WHERE active = 1 AND superseded_by IS NULL ")
        params: list[Any] = []
        if kind:
            sql += "AND kind = %s "
            params.append(kind)
        if agent:
            sql += "AND agent = %s "
            params.append(str(agent).strip().lower()[:32])
        sql += "ORDER BY importance DESC, updated_at DESC, id DESC LIMIT %s"
        params.append(max(1, int(limit)))
        return [dict(r) for r in self._exec(sql, tuple(params), fetch=True)]

    def recall_memories(self, query: str, *, limit: int = 5,
                        kind: Optional[str] = None, agent: Optional[str] = None) -> list[dict[str, Any]]:
        """混合召回（整句精确子串已在 search_memories 排序置顶，不再二次扫表）。"""
        return self.search_memories_hybrid(query, limit=int(limit), kind=kind, agent=agent)[:int(limit)]

    def _write_qcache(self, cache_key: str, text: str, count: int, hits: list,
                      *, turns: Optional[list] = None, qver: int = 0,
                      ttl: Optional[int] = None) -> None:
        """写查询缓存；空结果也写（短 TTL）。"""
        ex = int(ttl if ttl is not None else self.cfg["search_cache_ttl"])
        if not count:
            ex = min(ex, 120)
        try:
            self.redis().set(
                cache_key,
                json.dumps(
                    {"qver": int(qver), "text": text, "count": count, "hits": hits, "turns": turns or []},
                    ensure_ascii=False,
                    default=str,
                ),
                ex=ex,
            )
        except Exception:  # noqa: BLE001
            pass

    def _compose(self, mems: list[dict[str, Any]], turns: list[dict[str, Any]]) -> str:
        budget = int(self.cfg["prefetch_max_chars"])
        blocks: list[str] = []
        if mems:
            lines = ["[atbrains 长期记忆 · MariaDB]"]
            for m in mems:
                line = f"- [{m['kind']}·重要度{m['importance']}] {_clip(str(m['content']), 480)}"
                if m.get("tags"):
                    line += f" (tags: {m['tags']})"
                if m.get("machine"):
                    line += f" [{m['machine']}]"
                lines.append(line)
            blocks.append("\n".join(lines))
        if turns:
            lines = ["[atbrains 相关历史对话 · 归档]"]
            for t in turns:
                ts = t.get("ts")
                ts_s = ts.strftime("%Y-%m-%d") if hasattr(ts, "strftime") else str(ts)[:16]
                snippet = _clip(str(t.get("user_content") or "").replace("\n", " "), 150)
                if snippet:
                    mch = str(t.get("machine") or "")
                    tag = f" [{mch}]" if mch else ""
                    lines.append(f"- {ts_s}{tag}: {snippet}")
            blocks.append("\n".join(lines))
        text = "\n".join(b for b in blocks if len(b.splitlines()) > 1)
        if len(text) > budget:
            text = text[:budget].rstrip() + " …"
        return text
