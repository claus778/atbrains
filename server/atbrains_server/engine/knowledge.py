"""知识层：结构化主张（claim）/ 证据 / 情节头（episode）。"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from typing import Optional
import hashlib
import json
import time

from ..context import (
    RequestContext,
    owns_record,
)
from ..schema import (
    _is_duplicate_error,
)
from ..util import (  # noqa: F401
    BrainError,
    BrainForbidden,
    _cfg_flag,
    _db_safe,
    _regexp_pattern,
    _regexp_target,
    normalize_platform,
    search_terms,
)

logger = logging.getLogger("atbrains")

class KnowledgeMixin:
    """知识层：结构化主张（claim）/ 证据 / 情节头（episode）。"""

    @staticmethod
    def claim_slot_key(machine: str, subject: str, predicate: str, kind: str) -> str:
        """一条主张的唯一槽位：同机 + 同主语 + 同谓词 + 同类 = 同一条主张的不同版本。"""
        raw = "\x00".join([str(machine or ""), str(subject or ""), str(predicate or ""), str(kind or "")])
        return hashlib.md5(raw.encode("utf-8", "replace")).hexdigest()

    @staticmethod
    def _json_list(value: Any) -> str:
        """把 list / JSON 字符串统一成 JSON 数组文本（落库前校验）。"""
        if value is None:
            return "[]"
        if isinstance(value, (list, tuple)):
            return json.dumps([str(v) for v in value if str(v).strip()], ensure_ascii=False)
        text = str(value).strip()
        if not text:
            return "[]"
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return json.dumps([str(v) for v in parsed], ensure_ascii=False)
        except Exception:  # noqa: BLE001
            pass
        return json.dumps([text], ensure_ascii=False)

    def claim_remember(self, subject: str, predicate: str, value: str, *,
                       kind: str = "fact", basis: str = "direct_report",
                       valid_from: Optional[str] = None, valid_to: Optional[str] = None,
                       turn_id: Optional[int] = None, seq: Optional[int] = None,
                       quote: str = "", confidence: int = 80, importance: int = 5,
                       machine: str = "",
                       ctx: Optional[RequestContext] = None) -> dict[str, Any]:
        """记一条结构化主张（subject/predicate/value），老版本自动封版。

        与 brain_memories 的区别：这里是「一个槽位的当前值 + 历史版本」，
        同槽位再写只递增 revision，不新增行；每条版本可挂证据（turn_id + seq + quote）。
        """
        subject = str(subject or "").strip()[:191]
        predicate = str(predicate or "").strip()[:96]
        value = str(value or "").strip()
        if not subject or not predicate or not value:
            raise BrainError("subject / predicate / value 都不能为空")
        kind = (str(kind or "fact").strip() or "fact")[:24]
        basis = (str(basis or "direct_report").strip() or "direct_report")[:16]
        ctx_mach, ag, _role = self._identity(ctx)
        mach = "shared" if machine == "shared" else (str(machine or ctx_mach)[:48])
        slot = self.claim_slot_key(mach, subject, predicate, kind)
        try:
            importance = max(1, min(10, int(importance)))
            confidence = max(0, min(100, int(confidence)))
        except (TypeError, ValueError):
            importance, confidence = 5, 80
        self.ensure_schema()
        val_safe = _db_safe(value[:16000])
        quote_safe = _db_safe(str(quote or "")[:2000])
        subj_safe = _db_safe(subject)
        pred_safe = _db_safe(predicate)
        with self.transaction() as cur:
            cur.execute(
                "SELECT id, current_revision FROM brain_claims WHERE slot_key = %s FOR UPDATE",
                (slot,),
            )
            row = cur.fetchone()
            if row:
                claim_id = int(row["id"])
                revision = int(row["current_revision"] or 0) + 1
                cur.execute(
                    "UPDATE brain_claims SET current_revision = %s, state = 'active', importance = %s, "
                    "agent = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                    (revision, importance, ag, claim_id),
                )
                cur.execute(
                    "UPDATE brain_claim_versions SET recorded_to = CURRENT_TIMESTAMP "
                    "WHERE claim_id = %s AND recorded_to IS NULL AND revision < %s",
                    (claim_id, revision),
                )
                created = False
            else:
                try:
                    cur.execute(
                        "INSERT INTO brain_claims (machine, agent, subject, predicate, kind, slot_key, "
                        "current_revision, state, importance) VALUES (%s, %s, %s, %s, %s, %s, 1, 'active', %s)",
                        (mach, ag, subj_safe, pred_safe, kind, slot, importance),
                    )
                    claim_id, revision, created = int(cur.lastrowid), 1, True
                except Exception as exc:  # noqa: BLE001
                    if not _is_duplicate_error(exc):
                        raise
                    cur.execute(
                        "SELECT id, current_revision FROM brain_claims WHERE slot_key = %s FOR UPDATE",
                        (slot,),
                    )
                    row = cur.fetchone()
                    if not row:
                        raise
                    claim_id = int(row["id"])
                    revision = int(row["current_revision"] or 0) + 1
                    cur.execute(
                        "UPDATE brain_claims SET current_revision = %s, state = 'active', importance = %s, "
                        "agent = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                        (revision, importance, ag, claim_id),
                    )
                    cur.execute(
                        "UPDATE brain_claim_versions SET recorded_to = CURRENT_TIMESTAMP "
                        "WHERE claim_id = %s AND recorded_to IS NULL AND revision < %s",
                        (claim_id, revision),
                    )
                    created = False
            cur.execute(
                "INSERT INTO brain_claim_versions (claim_id, revision, agent, value_text, basis, valid_from, "
                "valid_to, replaces_revision, source_turn_id, source_seq, quote, confidence) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    claim_id, revision, ag, val_safe, basis, valid_from or None, valid_to or None,
                    revision - 1 if revision > 1 else None, int(turn_id) if turn_id else None,
                    int(seq) if seq is not None else None, quote_safe, confidence,
                ),
            )
            if turn_id:
                cur.execute(
                    "INSERT IGNORE INTO brain_claim_evidence (claim_id, revision, turn_id, seq, relation, quote) "
                    "VALUES (%s, %s, %s, %s, 'supports', %s)",
                    (claim_id, revision, int(turn_id), int(seq or 0), quote_safe),
                )
        return {"claim_id": claim_id, "revision": revision, "slot_key": slot,
                "subject": subject, "predicate": predicate, "kind": kind, "created": created}

    def claim_recall(self, *, query: str = "", subject: str = "", predicate: str = "",
                     limit: int = 10, history: bool = False) -> list[dict[str, Any]]:
        """取当前有效主张（history=True 返回全部版本，按时间倒序）。"""
        limit = max(1, min(50, int(limit or 10)))
        self.ensure_schema()
        if history:
            sql = ("SELECT c.id AS claim_id, c.subject, c.predicate, c.kind, c.machine, v.agent AS agent, v.revision, "
                   "v.value_text, v.basis, v.confidence, v.valid_from, v.valid_to, "
                   "v.recorded_from, v.recorded_to, v.source_turn_id, v.source_seq, v.quote "
                   "FROM brain_claims c JOIN brain_claim_versions v ON v.claim_id = c.id WHERE 1=1 ")
        else:
            sql = ("SELECT c.id AS claim_id, c.subject, c.predicate, c.kind, c.machine, v.agent AS agent, c.state, "
                   "v.revision, v.value_text, v.basis, v.confidence, v.valid_from, v.valid_to, "
                   "v.recorded_from, v.source_turn_id, v.source_seq, v.quote "
                   "FROM brain_claims c JOIN brain_claim_versions v "
                   "ON v.claim_id = c.id AND v.revision = c.current_revision "
                   "WHERE c.state = 'active' ")
        params: list[Any] = []
        if subject:
            sql += "AND c.subject LIKE %s "
            params.append(f"%{subject}%")
        if predicate:
            sql += "AND c.predicate LIKE %s "
            params.append(f"%{predicate}%")
        if query:
            terms = search_terms(query, max_terms=4) or [str(query)]
            pattern = _regexp_pattern(terms) if _cfg_flag(self.cfg, "search_regexp_enabled", 1) else ""
            if pattern:
                cols = ("c.subject", "c.predicate", "v.value_text")
                sql += f"AND ({_regexp_target(cols)} REGEXP %s) "
                params.append(pattern)
            else:
                conds = []
                for term in terms:
                    conds.append("(c.subject LIKE %s OR c.predicate LIKE %s OR v.value_text LIKE %s)")
                    like = f"%{term}%"
                    params += [like, like, like]
                sql += "AND (" + " OR ".join(conds) + ") "
        sql += "ORDER BY v.recorded_from DESC, c.id DESC LIMIT %s"
        params.append(limit)
        rows = [dict(r) for r in self._exec(sql, tuple(params), fetch=True)]
        for r in rows:
            for k, v in list(r.items()):
                if isinstance(v, datetime):
                    r[k] = v.isoformat(sep=" ", timespec="seconds")
        return rows

    def claim_retire(self, claim_id: int, reason: str = "",
                     *, ctx: Optional[RequestContext] = None) -> dict[str, Any]:
        """撤回一条主张（保留版本与证据，只是不再算当前值）。"""
        self.ensure_schema()
        cid = int(claim_id)
        if ctx is not None and ctx.role != "admin":
            rows = self._exec("SELECT id, machine, agent FROM brain_claims WHERE id = %s", (cid,), fetch=True)
            if rows and not owns_record(ctx, rows[0], allow_shared=True):
                raise BrainForbidden("无权撤回其他身份的主张")
        rowcount, _ = self._exec(
            "UPDATE brain_claims SET state = 'retracted', updated_at = CURRENT_TIMESTAMP "
            "WHERE id = %s AND state <> 'retracted'", (cid,))
        self._exec(
            "UPDATE brain_claim_versions SET recorded_to = CURRENT_TIMESTAMP "
            "WHERE claim_id = %s AND recorded_to IS NULL", (cid,))
        return {"claim_id": cid, "retracted": rowcount, "reason": str(reason or "")[:200]}

    def episode_set(self, turn_id: int, *, goal: Optional[str] = None,
                    decisions: Any = None, open_items: Any = None, blockers: Any = None,
                    next_step: Optional[str] = None, status: Optional[str] = None,
                    session_id: str = "",
                    ctx: Optional[RequestContext] = None) -> dict[str, Any]:
        """给某一轮挂一个情节头（目标/决策/未完成/阻碍/下一步）；只更新传入的字段。"""
        tid = int(turn_id)
        self.ensure_schema()
        mach, ag, role = self._identity(ctx)
        if role != "admin":
            existing = self._exec("SELECT machine, agent FROM brain_episodes WHERE turn_id = %s", (tid,), fetch=True)
            if existing and not owns_record(ctx, existing[0]):
                raise BrainForbidden("无权修改其他身份的情节")
            trow = self._exec("SELECT machine, agent FROM brain_turns WHERE id = %s", (tid,), fetch=True)
            if trow and not owns_record(ctx, trow[0]):
                raise BrainForbidden("无权为其他身份的轮次挂情节")
        vals = {
            "goal": None if goal is None else str(goal)[:4000],
            "decisions": None if decisions is None else self._json_list(decisions),
            "open_items": None if open_items is None else self._json_list(open_items),
            "blockers": None if blockers is None else self._json_list(blockers),
            "next_step": None if next_step is None else str(next_step)[:4000],
            "status": None if status is None else str(status)[:16],
        }
        self._exec(
            "INSERT INTO brain_episodes (turn_id, session_id, machine, agent, goal, decisions, open_items, "
            "blockers, next_step, status) VALUES (%s, %s, %s, %s, COALESCE(%s,''), COALESCE(%s,'[]'), "
            "COALESCE(%s,'[]'), COALESCE(%s,'[]'), COALESCE(%s,''), COALESCE(%s,'open')) "
            "ON DUPLICATE KEY UPDATE goal = COALESCE(%s, goal), decisions = COALESCE(%s, decisions), "
            "open_items = COALESCE(%s, open_items), blockers = COALESCE(%s, blockers), "
            "next_step = COALESCE(%s, next_step), status = COALESCE(%s, status), agent = VALUES(agent)",
            (tid, str(session_id or "")[:96], mach, ag,
             vals["goal"], vals["decisions"], vals["open_items"], vals["blockers"],
             vals["next_step"], vals["status"],
             vals["goal"], vals["decisions"], vals["open_items"], vals["blockers"],
             vals["next_step"], vals["status"]))
        return {"turn_id": tid, "updated": True}

    def episode_get(self, turn_id: int = 0, *, session_id: str = "",
                    ctx: Optional[RequestContext] = None) -> dict[str, Any]:
        """读某一轮的情节头（共享协作用只读接口）；不传 turn_id 时取该会话最后一轮。"""
        self.ensure_schema()
        tid = int(turn_id or 0)
        if not tid and session_id:
            rows = self._exec(
                "SELECT turn_id FROM brain_episodes WHERE session_id = %s ORDER BY turn_id DESC LIMIT 1",
                (str(session_id)[:96],), fetch=True)
            tid = int(rows[0]["turn_id"]) if rows else 0
        if not tid:
            return {"found": False, "reason": "需要 turn_id 或可用的 session_id"}
        rows = self._exec(
            "SELECT turn_id, session_id, machine, agent, goal, decisions, open_items, blockers, next_step, "
            "status, created_at, updated_at FROM brain_episodes WHERE turn_id = %s", (tid,), fetch=True)
        if not rows:
            return {"found": False, "turn_id": tid}
        out = dict(rows[0])
        for key in ("decisions", "open_items", "blockers"):
            try:
                out[key] = json.loads(out.get(key) or "[]")
            except Exception:  # noqa: BLE001
                out[key] = []
        for k, v in list(out.items()):
            if isinstance(v, datetime):
                out[k] = v.isoformat(sep=" ", timespec="seconds")
        out["found"] = True
        return out

    def _resolve_author(self, author: str, platform: str, *, machine: str = "") -> str:
        """规范化/回填 author，brain_author_defaults 走 60 秒进程内缓存，避免每轮查询。"""
        a = str(author or "").strip()
        if a.endswith("@im.wechat"):
            return ("wx:" + a[:-10])[:64]
        if a:
            return a[:64]
        p = str(platform or "").strip().lower()
        if p in ("cron", "oneshot", "subagent"):
            return f"sys:{p}"[:64]
        mach = (machine or self.machine)[:48]
        try:
            now = time.monotonic()
            cached = self._author_defaults_cache
            if cached is not None and now - cached[0] < 60.0:
                rows = cached[1]
            else:
                rows = list(self._exec(
                    "SELECT machine, platform, author FROM brain_author_defaults", fetch=True))
                self._author_defaults_cache = (now, rows)
            best_score, best_author = -1, ""
            p_short = p[:32]
            for r in rows:
                rm, rp = r.get("machine"), r.get("platform")
                if rm not in (mach, "*") or rp not in (p_short, "*"):
                    continue
                sc = (2 if rm != "*" else 0) + (1 if rp != "*" else 0)
                if sc > best_score and r.get("author"):
                    best_score, best_author = sc, str(r["author"])
            if best_author:
                return best_author[:64]
        except Exception:  # noqa: BLE001
            pass
        return "unknown"

    def open_episodes(self, limit: int = 5, *, ctx: Optional[RequestContext] = None) -> list[dict[str, Any]]:
        """未完成情节（status=open），跨 agent 交接可见，最近的在前。"""
        self.ensure_schema()
        try:
            rows = self._exec(
                "SELECT turn_id, session_id, machine, agent, goal, decisions, open_items, "
                "blockers, next_step, status, updated_at FROM brain_episodes "
                "WHERE status = 'open' ORDER BY updated_at DESC, turn_id DESC LIMIT %s",
                (max(1, int(limit)),), fetch=True)
        except Exception:  # noqa: BLE001
            return []
        return [dict(r) for r in rows]

    def record_turn(self, session_id: str, user_content: str, assistant_content: str,
                    *, platform: str = "", author: str = "",
                    messages: Optional[list[dict[str, Any]]] = None,
                    total_messages: Optional[int] = None,
                    ctx: Optional[RequestContext] = None) -> int:
        platform = normalize_platform(platform)[:48]
        sid = _db_safe(str(session_id or ""))[:96]
        mach, ag, _role = self._identity(ctx)
        u_raw, a_raw = str(user_content or ""), str(assistant_content or "")
        u, a = _db_safe(u_raw[:16000]), _db_safe(a_raw[:16000])
        truncated = 1 if (len(u_raw) > 16000 or len(a_raw) > 16000) else 0
        self.ensure_schema()
        resolved_author = _db_safe(self._resolve_author(author, platform, machine=mach))
        delta: list[dict[str, Any]] = []
        with self.transaction() as cur:
            cur.execute(
                "INSERT INTO brain_turns (session_id, platform, author, machine, agent, truncated, "
                "user_content, assistant_content) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (sid, platform, resolved_author, mach, ag, truncated, u, a),
            )
            turn_id = int(cur.lastrowid)
            cur.execute(
                "INSERT INTO brain_sessions (session_id, platform, machine, started_at, last_active, turn_count) "
                "VALUES (%s, %s, %s, NOW(), NOW(), 1) "
                "ON DUPLICATE KEY UPDATE last_active = NOW(), turn_count = turn_count + 1, "
                "platform = VALUES(platform), machine = VALUES(machine)",
                (sid, platform, mach),
            )
            if messages:
                try:
                    stored, stored_bytes, delta = self._store_turn_messages_in_tx(
                        cur, turn_id, sid, messages, total_messages
                    )
                    if stored:
                        cur.execute(
                            "UPDATE brain_turns SET message_count = %s, message_bytes = %s WHERE id = %s",
                            (min(stored, 65535), min(stored_bytes, 4294967295), turn_id),
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("atbrains: 转录层写入失败（正文已存）: %s", str(exc)[:200])
        if delta and _cfg_flag(self.cfg, "projection_tables_enabled", 0):
            try:
                self._index_turn_details(turn_id, delta)
            except Exception as exc:  # noqa: BLE001
                logger.debug("atbrains: 思考/工具调用索引失败（原文已存）: %s", str(exc)[:200])
        try:
            r = self.redis()
            stream = self._rk("recent")
            skey = self._rk("session", sid)
            ttl = int(self.cfg["short_term_ttl_days"]) * 86400
            now_s = time.time()
            pipe = r.pipeline()
            pipe.xadd(
                stream,
                {"sid": sid, "platform": platform, "author": author, "machine": mach,
                 "agent": ag, "user": u[:600], "assistant": a[:600], "ts": f"{now_s:.3f}"},
                maxlen=int(self.cfg["stream_maxlen"]), approximate=True,
            )
            pipe.expire(stream, ttl)
            pipe.hset(skey, mapping={"platform": platform, "last_active": str(int(now_s))})
            pipe.hincrby(skey, "turns", 1)
            pipe.expire(skey, ttl)
            pipe.incr(self._rk("qver"))
            pipe.execute()
            self._invalidate_caches(bump_qver=False)
        except Exception as exc:  # noqa: BLE001
            self._invalidate_caches(bump_qver=True)
            logger.debug("atbrains: Redis 回合写入失败: %s", exc)
        return turn_id
