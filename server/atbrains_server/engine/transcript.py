"""对话归档与全量转录层（brain_turns / brain_turn_messages / sessions / archive）。"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from typing import Optional
import json
import time
import zlib

from ..context import (
    RequestContext,
    owns_record,
)
from ..util import (  # noqa: F401
    BrainForbidden,
    _as_text,
    _as_tool_calls,
    _cfg_flag,
    _db_safe,
    _epoch_to_dt,
    _window_start,
    normalize_platform,
)

logger = logging.getLogger("atbrains")

class TranscriptMixin:
    """对话归档与全量转录层（brain_turns / brain_turn_messages / sessions / archive）。"""

    def store_turn_messages(self, turn_id: int, session_id: str,
                            messages: list[dict[str, Any]],
                            total_messages: Optional[int] = None,
                            *, ctx: Optional[RequestContext] = None) -> tuple[int, int]:
        """把本轮的完整消息链（含工具调用/工具输出/推理）压进 brain_turn_messages。"""
        msgs = [m for m in (messages or []) if isinstance(m, dict)]
        if not msgs or not int(self.cfg.get("turn_messages_enabled") or 0):
            return 0, 0
        sid = _db_safe(str(session_id or ""))[:96]
        mach, _ag, _role = self._identity(ctx)
        max_msg = int(self.cfg.get("turn_msg_max_bytes") or 1048576)
        max_total = int(self.cfg.get("turn_total_max_bytes") or 16777216)
        rows: list[tuple] = []
        delta: list[dict[str, Any]] = []
        total = 0
        with self.transaction() as cur:
            cur.execute(
                "INSERT INTO brain_sessions (session_id, platform, machine, started_at, "
                "last_active, turn_count) VALUES (%s, '', %s, NOW(), NOW(), 0) "
                "ON DUPLICATE KEY UPDATE session_id = VALUES(session_id)",
                (sid, mach))
            cur.execute(
                "SELECT archived_messages FROM brain_sessions WHERE session_id = %s FOR UPDATE",
                (sid,))
            row = cur.fetchone()
            start, cursor = _window_start(
                msgs, int(row["archived_messages"] or 0) if row else 0, total_messages)
            delta = msgs[start:]
            for seq, m in enumerate(delta):
                role = _db_safe(str(m.get("role") or ""))[:16]
                tool_name = _db_safe(str(m.get("tool_name") or m.get("name") or ""))[:64]
                tool_call_id = _db_safe(str(m.get("tool_call_id") or ""))[:128]
                payload = {k: v for k, v in m.items()
                           if k not in ("role", "tool_name", "name", "tool_call_id")}
                try:
                    raw = json.dumps(payload, ensure_ascii=False,
                                     default=str).encode("utf-8", "replace")
                except Exception:  # noqa: BLE001
                    raw = json.dumps({"_unserializable": str(payload)[:4096]},
                                     ensure_ascii=False).encode("utf-8", "replace")
                if len(raw) > max_msg:
                    content = payload.get("content")
                    payload = {"_truncated_bytes": len(raw),
                               "content": str(content if content is not None else "")[:max_msg // 4]}
                    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8", "replace")
                if total + len(raw) > max_total:
                    logger.warning("atbrains: 单轮转录超过 %s 字节上限，剩余 %s 条将在下一轮续存",
                                   max_total, len(delta) - seq)
                    break
                total += len(raw)
                rows.append((turn_id, seq, role, tool_name, tool_call_id, len(raw),
                             zlib.compress(raw, 1)))
            if rows:
                cur.executemany(
                    "INSERT INTO brain_turn_messages (turn_id, seq, role, tool_name, "
                    "tool_call_id, raw_bytes, body) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)", rows)
                cur.execute(
                    "UPDATE brain_sessions SET archived_messages = %s WHERE session_id = %s",
                    (cursor + len(rows), sid))
        if rows and _cfg_flag(self.cfg, "projection_tables_enabled", 0):
            try:
                self._index_turn_details(turn_id, delta)
            except Exception as exc:  # noqa: BLE001
                logger.debug("atbrains: 思考/工具调用索引失败（原文已存）: %s", str(exc)[:200])
        return len(rows), total

    def _detail_cap(self, key: str, default: int) -> int:
        try:
            return max(0, int(self.cfg.get(key) or default))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _detail_clip(text: str, cap: int) -> str:
        """投影层只留前缀：全文权威在 brain_turn_messages 的 zlib blob 里。cap<=0 = 不截。"""
        if cap <= 0 or len(text) <= cap:
            return text
        return text[:cap] + f"\n…[truncated {len(text) - cap} chars]"

    def _index_turn_details(self, turn_id: int, messages: list[dict[str, Any]]) -> tuple[int, int]:
        """把一轮里的「思考」与「工具调用」投影成结构化行（schema v9）。

        原文仍以 brain_turn_messages（zlib blob）为权威；这里只是可查询投影：
          brain_thoughts   一条 assistant 消息一行（reasoning / reasoning_content）
          brain_tool_calls 一个 tool-call 一行（参数、结果、状态、起止、耗时）
        幂等键：(turn_id, seq) 与 (turn_id, seq, call_index)，重放/回填不会写重。
        """
        cap_args = self._detail_cap("detail_args_max_chars", 512)
        cap_res = self._detail_cap("detail_result_max_chars", 1024)
        cap_reason = self._detail_cap("detail_reasoning_max_chars", 2000)
        thoughts: list[tuple] = []
        calls: list[dict[str, Any]] = []
        for seq, m in enumerate(messages or []):
            if not isinstance(m, dict):
                continue
            role = str(m.get("role") or "")
            ts = _epoch_to_dt(m.get("timestamp"))
            if role == "assistant":
                reasoning = m.get("reasoning") or m.get("reasoning_content") or ""
                if isinstance(reasoning, str) and reasoning.strip():
                    thoughts.append((
                        turn_id, seq, _db_safe(str(m.get("message_uid") or "")[:96]),
                        _db_safe(self._detail_clip(reasoning, cap_reason)), len(reasoning),
                        _db_safe(str(m.get("finish_reason") or "")[:32]), ts))
                for idx, c in enumerate(_as_tool_calls(m.get("tool_calls"))):
                    if not isinstance(c, dict):
                        continue
                    fn = c.get("function") if isinstance(c.get("function"), dict) else {}
                    calls.append({
                        "seq": seq, "idx": idx,
                        "call_id": _db_safe(str(c.get("id") or "")[:128]),
                        "name": _db_safe(str(fn.get("name") or c.get("name") or "")[:64]),
                        "args": _db_safe(self._detail_clip(
                            _as_text(fn.get("arguments") if fn else c.get("arguments")), cap_args)),
                        "started": ts, "ended": None, "result": None, "result_len": 0,
                        "status": "pending",
                    })
            elif role == "tool":
                cid = _db_safe(str(m.get("tool_call_id") or m.get("toolCallId") or "")[:128])
                text = _db_safe(_as_text(m.get("content")))
                failed = "error" if (m.get("is_error") or m.get("isError")) else "ok"
                target = next((c for c in reversed(calls) if cid and c["call_id"] == cid), None)
                if target is None:
                    # 结果先于调用到达（跨轮续传/回填缺口）：单独记一行，不留空洞
                    calls.append({"seq": seq, "idx": 0, "call_id": cid,
                                  "name": _db_safe(str(m.get("tool_name") or m.get("name") or "")[:64]),
                                  "args": "", "started": None, "ended": ts,
                                  "result": self._detail_clip(text, cap_res),
                                  "result_len": len(text), "status": failed})
                else:
                    target.update({"result": self._detail_clip(text, cap_res),
                                   "result_len": len(text), "ended": ts, "status": failed})
        if not thoughts and not calls:
            return 0, 0
        with self.transaction() as cur:
            if thoughts:
                cur.executemany(
                    "INSERT INTO brain_thoughts (turn_id, seq, message_uid, reasoning, "
                    "reasoning_chars, finish_reason, ts) VALUES (%s, %s, %s, %s, %s, %s, %s) "
                    "ON DUPLICATE KEY UPDATE reasoning = VALUES(reasoning), "
                    "reasoning_chars = VALUES(reasoning_chars), "
                    "finish_reason = VALUES(finish_reason), "
                    # 回放/回填常常不带消息级 timestamp；用 COALESCE 保住已有时间，别把它擦成 NULL
                    "ts = COALESCE(VALUES(ts), ts)", thoughts)
            if calls:
                rows_c = []
                for c in calls:
                    dur = None
                    if c["started"] and c["ended"]:
                        dur = max(0, int((c["ended"] - c["started"]).total_seconds() * 1000))
                    rows_c.append((turn_id, c["seq"], c["idx"], c["call_id"], c["name"],
                                   c["args"] or None, c["result"], int(c.get("result_len") or 0),
                                   c["status"], c["started"], c["ended"], dur))
                cur.executemany(
                    "INSERT INTO brain_tool_calls (turn_id, seq, call_index, call_id, tool_name, "
                    "args, result, result_chars, status, started_at, ended_at, duration_ms) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON DUPLICATE KEY UPDATE call_id = VALUES(call_id), tool_name = VALUES(tool_name), "
                    "args = VALUES(args), result = VALUES(result), result_chars = VALUES(result_chars), "
                    "status = VALUES(status), started_at = COALESCE(VALUES(started_at), started_at), "
                    "ended_at = COALESCE(VALUES(ended_at), ended_at), "
                    "duration_ms = COALESCE(VALUES(duration_ms), duration_ms)", rows_c)
        return len(thoughts), len(calls)

    def turn_detail(self, turn_id: int = 0, *, session_id: str = "",
                    max_chars: int = 1500, latest: bool = False,
                    ctx: Optional[RequestContext] = None) -> dict[str, Any]:
        """按 turn_id（或 session_id+latest）取回一轮的完整过程（工具调用/输出/推理）。"""
        self.ensure_schema()
        mach, ag, role = self._identity(ctx)
        tid = int(turn_id or 0)
        if tid <= 0 and session_id:
            if role == "admin":
                rows = self._exec(
                    "SELECT id FROM brain_turns WHERE session_id = %s ORDER BY ts DESC, id DESC LIMIT 1",
                    (str(session_id)[:96],), fetch=True)
            else:
                rows = self._exec(
                    "SELECT id FROM brain_turns WHERE session_id = %s "
                    "AND (agent = %s OR (agent = '' AND machine = %s)) ORDER BY ts DESC, id DESC LIMIT 1",
                    (str(session_id)[:96], ag, mach), fetch=True)
            tid = int(rows[0]["id"]) if rows else 0
        if tid <= 0:
            return {"found": False, "reason": "需要 turn_id，或可用的 session_id",
                    "hint": "atbrains_recent 可看到最近轮次"}
        head = self._exec(
            "SELECT id, session_id, platform, author, machine, agent, truncated, message_count, "
            "message_bytes, ts, user_content, assistant_content FROM brain_turns WHERE id = %s",
            (tid,), fetch=True)
        if not head:
            return {"found": False, "turn_id": tid}
        if role != "admin" and not owns_record(ctx, head[0]):
            raise BrainForbidden("无权读取其他身份的对话转录")
        msgs = self._exec(
            "SELECT seq, role, tool_name, tool_call_id, raw_bytes, body "
            "FROM brain_turn_messages WHERE turn_id = %s ORDER BY seq", (tid,), fetch=True)
        cap = max(200, min(20000, int(max_chars or 1500)))
        out_msgs: list[dict[str, Any]] = []
        for r in msgs:
            try:
                payload = json.loads(zlib.decompress(r["body"]).decode("utf-8", "replace"))
                if not isinstance(payload, dict):
                    payload = {"content": payload}
            except Exception:  # noqa: BLE001
                payload = {"_decode_error": True}
            content = payload.get("content")
            if isinstance(content, list):
                content = " ".join(str(p.get("text") or "") for p in content
                                   if isinstance(p, dict) and p.get("type") == "text")
            text = str(content or "")
            cut = len(text) - cap
            if cut > 0:
                text = text[:cap] + f"...[+{cut} 字符]"
            item: dict[str, Any] = {"seq": int(r["seq"]), "role": r["role"],
                                    "tool_name": r["tool_name"] or "",
                                    "raw_bytes": int(r["raw_bytes"] or 0), "content": text}
            if payload.get("tool_calls"):
                item["tool_calls"] = payload["tool_calls"]
            if payload.get("reasoning"):
                item["reasoning"] = str(payload["reasoning"])[:cap]
            if payload.get("_truncated_bytes"):
                item["truncated_bytes"] = payload["_truncated_bytes"]
            out_msgs.append(item)
        turn = dict(head[0])
        for k, v in list(turn.items()):
            if isinstance(v, datetime):
                turn[k] = v.isoformat(sep=" ", timespec="seconds")
        return {"found": True, "turn": turn, "messages": out_msgs, "stored": len(out_msgs)}

    def touch_session(self, session_id: str, platform: str = "",
                      *, ctx: Optional[RequestContext] = None) -> None:
        session_id = str(session_id or "")[:96]
        if not session_id:
            return
        mach, _ag, _role = self._identity(ctx)
        try:
            self.ensure_schema()
            self._exec(
                "INSERT INTO brain_sessions (session_id, platform, machine, started_at, last_active, turn_count) "
                "VALUES (%s, %s, %s, NOW(), NOW(), 0) "
                "ON DUPLICATE KEY UPDATE last_active = NOW(), platform = VALUES(platform), machine = VALUES(machine)",
                (session_id, normalize_platform(platform)[:48], mach),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("atbrains: touch_session 失败: %s", exc)
        try:
            skey = self._rk("session", session_id)
            self.redis().hset(skey, "last_active", str(int(time.time())))
            self.redis().expire(skey, int(self.cfg["short_term_ttl_days"]) * 86400)
        except Exception:  # noqa: BLE001
            pass

    def archive(self, session_id: str, kind: str, payload: str, *, platform: str = "",
                ctx: Optional[RequestContext] = None) -> int:
        payload = str(payload or "")
        if not payload:
            return 0
        if len(payload) > 400_000:
            payload = payload[:400_000] + "\n[truncated]"
        mach, _ag, _role = self._identity(ctx)
        self.ensure_schema()
        _, row_id = self._exec(
            "INSERT INTO brain_archive (session_id, machine, platform, kind, payload) "
            "VALUES (%s, %s, %s, %s, %s)",
            (str(session_id or "")[:96], mach, normalize_platform(platform)[:48],
             str(kind or "precompress")[:24], payload),
        )
        return row_id

    def get_session_cursor(self, session_id: str) -> int:
        self.ensure_schema()
        rows = self._exec(
            "SELECT archived_messages FROM brain_sessions WHERE session_id = %s",
            (str(session_id or "")[:96],), fetch=True)
        return int(rows[0]["archived_messages"] or 0) if rows else 0

    def list_turns(self, *, limit: int = 40, offset: int = 0, machine: str = "",
                   session_id: str = "", q: str = "") -> dict[str, Any]:
        self.ensure_schema()
        conds: list[tuple[str, tuple]] = []
        if machine.strip():
            conds.append(("machine = %s", (machine.strip(),)))
        if session_id.strip():
            conds.append(("session_id = %s", (session_id.strip(),)))
        if q.strip():
            conds.append(("(user_content LIKE %s OR assistant_content LIKE %s)",
                          (f"%{q.strip()}%", f"%{q.strip()}%")))
        where = "WHERE " + " AND ".join(c for c, _ in conds) if conds else ""
        args: list[Any] = []
        for _, vals in conds:
            args.extend(vals)
        rows = self._exec(
            "SELECT id, session_id, platform, author, machine, agent, ts, truncated, "
            "LEFT(user_content, 200) AS u, LEFT(assistant_content, 200) AS a "
            f"FROM brain_turns {where} ORDER BY id DESC LIMIT %s OFFSET %s",
            (*args, int(limit), int(offset)), fetch=True)
        total = self._exec(
            f"SELECT COUNT(*) AS c FROM brain_turns {where}", tuple(args), fetch=True)[0]["c"]
        return {"total": int(total or 0), "items": [dict(r) for r in rows]}

    def get_turn(self, turn_id: int) -> Optional[dict[str, Any]]:
        self.ensure_schema()
        rows = self._exec(
            "SELECT id, session_id, platform, author, machine, agent, truncated, "
            "user_content, assistant_content, ts FROM brain_turns WHERE id = %s",
            (int(turn_id),), fetch=True)
        return dict(rows[0]) if rows else None

    def list_sessions(self, *, limit: int = 100, offset: int = 0,
                      machine: str = "", q: str = "") -> dict[str, Any]:
        self.ensure_schema()
        conds: list[tuple[str, tuple]] = []
        if machine.strip():
            conds.append(("machine = %s", (machine.strip(),)))
        if q.strip():
            conds.append(("session_id LIKE %s", (f"%{q.strip()}%",)))
        where = "WHERE " + " AND ".join(c for c, _ in conds) if conds else ""
        args: list[Any] = []
        for _, vals in conds:
            args.extend(vals)
        rows = self._exec(
            "SELECT session_id, platform, machine, started_at, last_active, turn_count, note "
            f"FROM brain_sessions {where} ORDER BY last_active DESC LIMIT %s OFFSET %s",
            (*args, int(limit), int(offset)), fetch=True)
        total = self._exec(
            f"SELECT COUNT(*) AS c FROM brain_sessions {where}", tuple(args), fetch=True)[0]["c"]
        return {"total": int(total or 0), "items": [dict(r) for r in rows]}
