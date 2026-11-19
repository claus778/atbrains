"""atbrains Memory Service(/v1)—— 通用多 Agent 记忆服务端核心。

所有客户端（Hermes / AstrBot / dsh / Codex / Claude / 自定义脚本）一律走 HTTP /v1 或 /mcp，
只有本服务端持有 MariaDB / Redis / Qdrant 凭据与写库逻辑。

设计要点:
  · 直接复用 `atbrains_server.store.AtbrainsStore`，不依赖任何外部 agent 插件目录；
  · 进程内只启动一套后台 worker（向量 drainer / 维护 / 蒸馏），各身份 store 共享唤醒事件；
  · Bearer 令牌绑定身份 (`<machine>|<agent>` + `role`)，客户端请求体不能伪造 machine/agent；
  · `/v1/admin/*` 需要 admin 角色令牌（如 `server`）或控制台 `X-Auth-Token`。
"""
from __future__ import annotations

import hmac
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from . import PROTOCOL_VERSION, __version__
from .config import resolve_config
from .context import RequestContext, ScopedStore
from .store import AtbrainsStore
from .tokens import TokenIdentity, TokenStore, header_value, infer_client
from .tools_registry import as_bool, as_int, as_list, clean_rows, context_markdown, context_packet

# 异常消息脱敏:即便某个驱动把连接串塞进了报错里,也不能把口令下发
_SECRET_RE = re.compile(r"(?i)\b(password|passwd|pwd|token|api[_-]?key|secret)\b\s*[=:]\s*\S+")

_DEFAULT_TOKENS = TokenStore()


class BadRequest(ValueError):
    """客户端请求不合法 → 400。"""


class Forbidden(PermissionError):
    """权限不足（非 admin 令牌调用 /v1/admin/*）→ 403。"""


def _safe_error(exc: BaseException) -> str:
    msg = _SECRET_RE.sub(r"\1=***", str(exc))[:400]
    return f"{type(exc).__name__}: {msg}"


def load_tokens() -> dict[str, str]:
    return _DEFAULT_TOKENS.load()


def token_add(name: str, *, role: str = "") -> str:
    return _DEFAULT_TOKENS.add(name, role=role)


def token_rm(name: str) -> bool:
    return _DEFAULT_TOKENS.remove(name)


def _qparam(query: Any, key: str, default: str = "") -> str:
    if not isinstance(query, dict):
        return default
    raw = query.get(key)
    if isinstance(raw, (list, tuple)):
        raw = raw[0] if raw else default
    return str(raw if raw is not None else default)


def _qint(query: Any, body: Any, key: str, default: int) -> int:
    raw = body.get(key) if isinstance(body, dict) and key in body else _qparam(query, key, "")
    if raw in ("", None):
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise BadRequest(f"{key} 必须是整数") from None


class MemoryService:
    """Memory Service 的 HTTP 无关实现（路由 + 鉴权 + 每身份一个 AtbrainsStore）。"""

    _ROUTES: dict[str, tuple[tuple[str, ...], str]] = {
        "/v1/health": (("GET", "POST"), "_health"),
        "/v1/memory/recall": (("POST",), "_recall"),
        "/v1/memory/remember": (("POST",), "_remember"),
        "/v1/memory/forget": (("POST",), "_forget"),
        "/v1/memory/restore": (("POST",), "_restore"),
        "/v1/memory/deactivate": (("POST",), "_deactivate"),
        "/v1/memory/recent": (("POST",), "_recent"),
        "/v1/memory/turn": (("POST",), "_turn"),
        "/v1/memory/turn/detail": (("POST",), "_turn_detail"),
        "/v1/memory/archive": (("POST",), "_archive"),
        "/v1/memory/session/touch": (("POST",), "_session_touch"),
        "/v1/memory/status": (("GET", "POST"), "_status"),
        "/v1/claim/remember": (("POST",), "_claim_remember"),
        "/v1/claim/recall": (("POST",), "_claim_recall"),
        "/v1/claim/retire": (("POST",), "_claim_retire"),
        "/v1/episode/set": (("POST",), "_episode_set"),
        "/v1/episode/get": (("POST",), "_episode_get"),
        "/v1/context": (("POST",), "_context"),
        "/v1/handoff": (("POST",), "_handoff"),
        "/v1/admin/overview": (("GET",), "_admin_overview"),
        "/v1/admin/memories": (("GET",), "_admin_memories"),
        "/v1/admin/memory": (("GET",), "_admin_memory"),
        "/v1/admin/turns": (("GET",), "_admin_turns"),
        "/v1/admin/turn": (("GET",), "_admin_turn"),
        "/v1/admin/sessions": (("GET",), "_admin_sessions"),
        "/v1/admin/recent": (("GET",), "_admin_recent"),
        "/v1/admin/search": (("GET",), "_admin_search"),
        "/v1/admin/remember": (("POST",), "_admin_remember"),
        "/v1/admin/forget": (("POST",), "_admin_forget"),
        "/v1/admin/restore": (("POST",), "_admin_restore"),
        "/v1/admin/drain": (("POST",), "_admin_drain"),
        "/v1/admin/doctor": (("POST",), "_admin_doctor"),
        "/v1/admin/prune": (("POST",), "_admin_prune"),
        "/v1/admin/tokens": (("GET", "POST", "DELETE"), "_admin_tokens"),
    }
    _OPEN = frozenset({"/v1/health"})
    _ADMIN_PREFIX = "/v1/admin/"

    def __init__(
        self,
        config_home: Optional[str] = None,
        *,
        tokens_path: Optional[Path] = None,
        start_workers: bool = False,
        console_token_fn: Optional[Callable[[], str]] = None,
        store_factory: Optional[Callable[[dict[str, Any]], Any]] = None,
    ) -> None:
        self.config_home = config_home
        self.base_cfg = resolve_config(config_home)
        resolved_tokens = tokens_path or (Path(str(self.base_cfg["data_dir"])) / "service-tokens.json")
        self.tokens = TokenStore(tokens_path if tokens_path is not None else (
            None if not config_home and not self.base_cfg.get("data_dir") else resolved_tokens
        ))
        self._console_token_fn = console_token_fn
        self._store_factory = store_factory
        self._engine: Optional[Any] = None
        self._lock = threading.Lock()
        self._vec_wake = threading.Event()
        self._workers_started = False
        self._activity: dict[str, dict[str, Any]] = {}
        self._activity_lock = threading.Lock()
        if start_workers:
            self.start_workers()

    @property
    def engine(self) -> Any:
        if self._engine is None:
            with self._lock:
                if self._engine is None:
                    if self._store_factory is not None:
                        self._engine = self._store_factory(dict(self.base_cfg))
                    else:
                        self._engine = AtbrainsStore(
                            dict(self.base_cfg), start_workers=False, vec_wake=self._vec_wake
                        )
        return self._engine

    # ---------------------------------------------------------- 鉴权

    def authorize_identity(self, headers: Any, *, channel: str = "v1") -> Optional[TokenIdentity]:
        return self.tokens.authorize(headers, channel=channel)

    @staticmethod
    def authorize(headers: Any) -> Optional[str]:
        ident = _DEFAULT_TOKENS.authorize(headers, channel="v1")
        return ident.name if ident else None

    def _authorize_admin(self, headers: Any) -> Optional[TokenIdentity]:
        ident = self.authorize_identity(headers, channel="v1")
        if ident is not None:
            if ident.role != "admin":
                raise Forbidden("需要 admin 权限令牌")
            return ident
        if self._console_token_fn is not None:
            expected = str(self._console_token_fn() or "")
            got = ""
            for k, v in (headers or {}).items():
                if str(k).lower() == "x-auth-token":
                    got = str(v or "")
                    break
            if expected and got and hmac.compare_digest(got.encode("utf-8"), expected.encode("utf-8")):
                return TokenIdentity(name="console", machine="", agent="console", role="admin")
        return None

    # ---------------------------------------------------------- store 与 worker

    def start_workers(self) -> None:
        with self._lock:
            if self._workers_started:
                return
            self._workers_started = True
        eng = self.engine
        if hasattr(eng, "start_workers"):
            eng.start_workers()

    def store_for_identity(self, ident: TokenIdentity) -> ScopedStore:
        ctx = RequestContext(machine=ident.machine, agent=ident.agent, role=ident.role)
        return ScopedStore(self.engine, ctx)

    def store_for(self, machine: str, agent: str = "default", *, role: str = "agent") -> ScopedStore:
        ag = (str(agent or "default").strip().lower() or "default")[:32]
        mach = str(machine or "").strip()[:48]
        return ScopedStore(self.engine, RequestContext(machine=mach, agent=ag, role=role))

    def _admin_store(self) -> ScopedStore:
        return self.store_for("", "admin", role="admin")

    # ---------------------------------------------------------- 入口

    def record_activity(
        self,
        ident: TokenIdentity,
        *,
        channel: str,
        headers: Any = None,
        path: str = "",
        remote_ip: str = "",
    ) -> None:
        if not ident or not ident.name or ident.name == "console":
            return
        ip = header_value(headers, "X-Real-IP").strip()
        if not ip:
            xff = header_value(headers, "X-Forwarded-For").strip()
            ip = xff.split(",")[0].strip() if xff else str(remote_ip or "").strip()
        ua = header_value(headers, "User-Agent").strip()[:120]
        hdr_client = header_value(headers, "X-Atbrains-Client").strip().lower()[:32]
        detected = hdr_client or ident.client
        if not detected and ua:
            low = ua.lower()
            for kw, cname in (
                ("hermes", "hermes"),
                ("astrbot", "astrbot"),
                ("dsh", "dsh"),
                ("claude", "claude-code"),
                ("codex", "codex"),
                ("cursor", "cursor"),
                ("atbrains-client", "python-sdk"),
            ):
                if kw in low:
                    detected = cname
                    break
        now = time.time()
        seen = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._activity_lock:
            prev = self._activity.get(ident.name)
            reqs = int(prev.get("requests", 0)) + 1 if prev else 1
            self._activity[ident.name] = {
                "last_ts": now,
                "last_seen": seen,
                "last_ip": ip,
                "last_channel": str(channel or ""),
                "last_ua": ua,
                "detected_client": detected or (prev.get("detected_client", "") if prev else ""),
                "last_path": str(path or "")[:64],
                "requests": reqs,
            }

    def handle(
        self,
        method: str,
        path: str,
        query: dict,
        body: dict,
        headers: dict,
        *,
        remote_ip: str = "",
    ) -> tuple[int, dict]:
        method = str(method or "GET").upper()
        route = self._ROUTES.get(path)
        if route is None:
            return 404, {"ok": False, "error": "not found"}
        methods, handler = route
        if method not in methods:
            return 405, {"ok": False, "error": f"method not allowed: {method}"}
        try:
            if path in self._OPEN:
                return 200, getattr(self, handler)()
            payload = body if isinstance(body, dict) else {}
            if path.startswith(self._ADMIN_PREFIX):
                ident = self._authorize_admin(headers)
                if ident is None:
                    return 401, {"ok": False, "error": "unauthorized"}
                self.record_activity(ident, channel="admin", headers=headers, path=path, remote_ip=remote_ip)
                return 200, getattr(self, handler)(method, ident, payload, query)
            ident = self.authorize_identity(headers, channel="v1")
            if ident is None:
                return 401, {"error": "unauthorized"}
            self.record_activity(ident, channel="v1", headers=headers, path=path, remote_ip=remote_ip)
            return 200, getattr(self, handler)(ident, payload, query)
        except BadRequest as e:
            return 400, {"ok": False, "error": str(e)}
        except PermissionError as e:
            return 403, {"ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001
            return 500, {"ok": False, "error": _safe_error(e)}

    # ---------------------------------------------------------- 核心端点

    def _health(self) -> dict:
        return {
            "ok": True,
            "version": PROTOCOL_VERSION,
            "server_version": __version__,
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    def _recall(self, ident: TokenIdentity, body: dict, query: dict) -> dict:
        q = str(body.get("query") or "").strip()
        if not q:
            raise BadRequest("query 不能为空")
        session_id = str(body.get("session_id") or "")
        limit = body.get("limit")
        eff_limit: Optional[int] = None
        if limit is not None:
            try:
                eff_limit = max(1, min(20, int(limit)))
            except (TypeError, ValueError):
                raise BadRequest("limit 必须是整数") from None
        kind = str(body.get("kind") or "").strip() or None
        want_agent = str(body.get("agent") or "").strip() or None
        include_turns = as_bool(body.get("include_turns"), True)
        include_claims = as_bool(body.get("include_claims"), False)

        want_turn_items = bool(body.get("include_turn_items"))
        st = self.store_for_identity(ident)
        res = st.prefetch_block(
            q,
            session_id=session_id,
            with_hits=True,
            with_turns=want_turn_items,
            limit=eff_limit,
            kind=kind,
            agent=want_agent,
            include_turns=include_turns or want_turn_items,
        )
        if want_turn_items and len(res) == 4:
            text, count, hits, turn_rows = res
        else:
            text, count, hits = res[0], res[1], res[2]
            turn_rows = []
        out: dict[str, Any] = {"ok": True, "text": text, "count": int(count), "items": hits}
        if include_claims:
            try:
                out["claims"] = st.claim_recall(query=q, limit=min(eff_limit or 5, 10))
            except Exception:  # noqa: BLE001
                out["claims"] = []
        if want_turn_items:
            out["turns"] = clean_rows(turn_rows)
        return out

    def _remember(self, ident: TokenIdentity, body: dict, query: dict) -> dict:
        content = str(body.get("content") or "").strip()
        if not content:
            raise BadRequest("content 不能为空")
        try:
            importance = int(body.get("importance") if body.get("importance") is not None else 5)
        except (TypeError, ValueError):
            raise BadRequest("importance 必须是整数") from None
        saved = self.store_for_identity(ident).remember(
            content,
            kind=str(body.get("kind") or "note"),
            tags=str(body.get("tags") or ""),
            importance=importance,
            source=str(body.get("source") or "service"),
            session_id=str(body.get("session_id") or ""),
            memory_type=str(body.get("memory_type") or ""),
        )
        return {"ok": True, "saved": saved}

    def _forget(self, ident: TokenIdentity, body: dict, query: dict) -> dict:
        try:
            mem_id = int(body.get("id"))
        except (TypeError, ValueError):
            raise BadRequest("id 必须是整数") from None
        return {"ok": True, "result": self.store_for_identity(ident).forget(mem_id)}

    def _restore(self, ident: TokenIdentity, body: dict, query: dict) -> dict:
        try:
            mem_id = int(body.get("id"))
        except (TypeError, ValueError):
            raise BadRequest("id 必须是整数") from None
        return {"ok": True, "result": self.store_for_identity(ident).restore(mem_id)}

    def _deactivate(self, ident: TokenIdentity, body: dict, query: dict) -> dict:
        content = str(body.get("content") or "")
        if not content.strip():
            raise BadRequest("content 不能为空")
        n = self.store_for_identity(ident).deactivate_by_content(content)
        return {"ok": True, "deactivated": int(n)}

    def _recent(self, ident: TokenIdentity, body: dict, query: dict) -> dict:
        try:
            limit = int(body.get("limit") if body.get("limit") is not None else 20)
        except (TypeError, ValueError):
            raise BadRequest("limit 必须是整数") from None
        limit = max(1, min(limit, 100))
        st = self.store_for_identity(ident)
        return {"ok": True, "hot": clean_rows(st.hot_recent(limit)), "turns": clean_rows(st.recent_turns(limit))}

    def _turn(self, ident: TokenIdentity, body: dict, query: dict) -> dict:
        session_id = str(body.get("session_id") or "").strip()
        user = str(body.get("user") or "").strip()
        assistant = str(body.get("assistant") or "").strip()
        if not session_id:
            raise BadRequest("session_id 不能为空")
        if not user:
            raise BadRequest("user 不能为空")
        msgs = body.get("messages")
        if msgs is not None and not isinstance(msgs, list):
            raise BadRequest("messages 必须是数组")
        total_messages: Optional[int] = None
        if msgs:
            msgs = [m for m in msgs if isinstance(m, dict)][-4000:]
            raw_total = body.get("total")
            if raw_total is not None:
                try:
                    total_messages = int(raw_total)
                except (TypeError, ValueError):
                    raise BadRequest("total 必须是整数") from None
                total_messages = max(0, min(total_messages, 100000))
        else:
            msgs = None
        if not assistant and not msgs:
            raise BadRequest("assistant 不能为空")
        st = self.store_for_identity(ident)
        turn_id = st.record_turn(
            session_id,
            user,
            assistant,
            platform=str(body.get("platform") or ""),
            author=str(body.get("author") or ""),
            messages=msgs,
            total_messages=total_messages,
        )
        try:
            archived = st.get_session_cursor(session_id)
        except Exception:  # noqa: BLE001
            archived = 0
        return {"ok": True, "id": int(turn_id), "archived": archived}

    def _turn_detail(self, ident: TokenIdentity, body: dict, query: dict) -> dict:
        raw_tid = body.get("turn_id")
        tid = 0
        if raw_tid not in (None, ""):
            try:
                tid = int(raw_tid)
            except (TypeError, ValueError):
                raise BadRequest("turn_id 必须是整数") from None
        sid = str(body.get("session_id") or "").strip()
        max_chars = max(200, min(20000, as_int(body.get("max_chars"), 1500)))
        st = self.store_for_identity(ident)
        res = st.turn_detail(tid, session_id=sid, max_chars=max_chars)
        if res.get("found"):
            res["episode"] = st.episode_get(as_int(res["turn"]["id"]))
        return {"ok": True, **res}

    def _archive(self, ident: TokenIdentity, body: dict, query: dict) -> dict:
        session_id = str(body.get("session_id") or "").strip()
        payload = str(body.get("payload") or "")
        if not session_id:
            raise BadRequest("session_id 不能为空")
        if not payload:
            raise BadRequest("payload 不能为空")
        row_id = self.store_for_identity(ident).archive(
            session_id,
            str(body.get("kind") or "precompress"),
            payload,
            platform=str(body.get("platform") or ""),
        )
        return {"ok": True, "id": int(row_id)}

    def _session_touch(self, ident: TokenIdentity, body: dict, query: dict) -> dict:
        session_id = str(body.get("session_id") or "").strip()
        if not session_id:
            raise BadRequest("session_id 不能为空")
        self.store_for_identity(ident).touch_session(session_id, str(body.get("platform") or ""))
        return {"ok": True}

    def _status(self, ident: TokenIdentity, body: dict, query: dict) -> dict:
        st = self.store_for_identity(ident)
        light = bool(body.get("light")) or _qparam(query, "light", "").strip().lower() in ("1", "true", "yes")
        out: dict[str, Any] = {
            "ok": True,
            "machine": st.machine,
            "agent": st.agent,
            "role": ident.role,
            "ping": st.ping(),
        }
        if not light:
            out["stats"] = st.stats()
        return out

    # ---------------------------------------------------------- 知识层（claims / episodes）

    def _claim_remember(self, ident: TokenIdentity, body: dict, query: dict) -> dict:
        subject = str(body.get("subject") or "").strip()
        predicate = str(body.get("predicate") or "").strip()
        value = str(body.get("value") or "").strip()
        if not subject or not predicate or not value:
            raise BadRequest("subject / predicate / value 都不能为空")
        claim_machine = "shared" if as_bool(body.get("shared"), False) else ""
        claim = self.store_for_identity(ident).claim_remember(
            subject,
            predicate,
            value,
            kind=str(body.get("kind") or "fact"),
            basis=str(body.get("basis") or "direct_report"),
            valid_from=body.get("valid_from") or None,
            valid_to=body.get("valid_to") or None,
            turn_id=body.get("turn_id"),
            seq=body.get("seq"),
            quote=str(body.get("quote") or ""),
            confidence=as_int(body.get("confidence"), 80),
            importance=as_int(body.get("importance"), 5),
            machine=claim_machine,
        )
        return {"ok": True, "claim": claim}

    def _claim_recall(self, ident: TokenIdentity, body: dict, query: dict) -> dict:
        rows = self.store_for_identity(ident).claim_recall(
            query=str(body.get("query") or ""),
            subject=str(body.get("subject") or ""),
            predicate=str(body.get("predicate") or ""),
            limit=max(1, min(50, as_int(body.get("limit"), 10))),
            history=as_bool(body.get("history"), False),
        )
        return {"ok": True, "claims": rows, "count": len(rows)}

    def _claim_retire(self, ident: TokenIdentity, body: dict, query: dict) -> dict:
        try:
            cid = int(body.get("claim_id") if body.get("claim_id") is not None else body.get("id"))
        except (TypeError, ValueError):
            raise BadRequest("claim_id 必须是整数") from None
        return {
            "ok": True,
            "result": self.store_for_identity(ident).claim_retire(cid, reason=str(body.get("reason") or "")),
        }

    def _episode_set(self, ident: TokenIdentity, body: dict, query: dict) -> dict:
        try:
            tid = int(body.get("turn_id"))
        except (TypeError, ValueError):
            raise BadRequest("turn_id 必须是整数") from None
        ep = self.store_for_identity(ident).episode_set(
            tid,
            goal=body.get("goal"),
            decisions=body.get("decisions"),
            open_items=body.get("open_items"),
            blockers=body.get("blockers"),
            next_step=body.get("next_step"),
            status=body.get("status"),
            session_id=str(body.get("session_id") or ""),
        )
        return {"ok": True, "episode": ep}

    def _episode_get(self, ident: TokenIdentity, body: dict, query: dict) -> dict:
        raw_tid = body.get("turn_id")
        tid = as_int(raw_tid, 0) if raw_tid is not None else 0
        res = self.store_for_identity(ident).episode_get(
            tid, session_id=str(body.get("session_id") or ""))
        return {"ok": True, **res}

    # ---------------------------------------------------------- 会话醒来包 / 交接

    def _context(self, ident: TokenIdentity, body: dict, query: dict) -> dict:
        st = self.store_for_identity(ident)
        packet = context_packet(
            st,
            st.agent,
            focus=str(body.get("focus") or ""),
            limit=as_int(body.get("limit"), 8),
            include_turns=as_bool(body.get("include_turns"), True),
            include_episodes=as_bool(body.get("include_episodes"), True),
            max_chars=as_int(body.get("max_chars"), 400),
        )
        if str(body.get("format") or "").lower() == "markdown":
            packet["markdown"] = context_markdown(packet)
        return {"ok": True, **packet}

    def _handoff(self, ident: TokenIdentity, body: dict, query: dict) -> dict:
        note = str(body.get("note") or "").strip()
        if not note:
            raise BadRequest("note 不能为空")
        st = self.store_for_identity(ident)
        saved = st.remember(
            note,
            kind="note",
            tags="handoff",
            importance=max(1, min(10, as_int(body.get("importance"), 7))),
            source="handoff",
            session_id=str(body.get("session_id") or ""),
        )
        out: dict[str, Any] = {
            "ok": True,
            "saved": saved,
            "hint": "下一次会话开头用 /v1/context 或 atbrains_context 读回这段便签",
        }
        turn_id = body.get("turn_id")
        if turn_id is not None:
            out["episode"] = st.episode_set(
                as_int(turn_id),
                next_step=body.get("next_step") or None,
                open_items=as_list(body.get("open_items")) or None,
                session_id=str(body.get("session_id") or ""),
            )
        return out

    # ---------------------------------------------------------- 管理端点 (/v1/admin/*)

    def _admin_overview(self, method: str, ident: TokenIdentity, body: dict, query: dict) -> dict:
        fresh = _qparam(query, "fresh", "0").strip().lower() in ("1", "true", "yes")
        return {"ok": True, **self._admin_store().overview(fresh=fresh)}

    def _admin_memories(self, method: str, ident: TokenIdentity, body: dict, query: dict) -> dict:
        return {"ok": True, **self._admin_store().list_memories(
            limit=min(_qint(query, body, "limit", 100), 500),
            offset=_qint(query, body, "offset", 0),
            active=_qparam(query, "active", "1"),
            q=_qparam(query, "q", ""),
            kind=_qparam(query, "kind", ""),
            machine=_qparam(query, "machine", ""),
            agent=_qparam(query, "agent", ""),
        )}

    def _admin_memory(self, method: str, ident: TokenIdentity, body: dict, query: dict) -> dict:
        return {"ok": True, "item": self._admin_store().get_memory(_qint(query, body, "id", 0))}

    def _admin_turns(self, method: str, ident: TokenIdentity, body: dict, query: dict) -> dict:
        return {"ok": True, **self._admin_store().list_turns(
            limit=min(_qint(query, body, "limit", 40), 300),
            offset=_qint(query, body, "offset", 0),
            machine=_qparam(query, "machine", ""),
            session_id=_qparam(query, "session_id", ""),
            q=_qparam(query, "q", ""),
        )}

    def _admin_turn(self, method: str, ident: TokenIdentity, body: dict, query: dict) -> dict:
        return {"ok": True, "item": self._admin_store().get_turn(_qint(query, body, "id", 0))}

    def _admin_sessions(self, method: str, ident: TokenIdentity, body: dict, query: dict) -> dict:
        return {"ok": True, **self._admin_store().list_sessions(
            limit=min(_qint(query, body, "limit", 100), 500),
            offset=_qint(query, body, "offset", 0),
            machine=_qparam(query, "machine", ""),
            q=_qparam(query, "q", ""),
        )}

    def _admin_recent(self, method: str, ident: TokenIdentity, body: dict, query: dict) -> dict:
        limit = max(1, min(_qint(query, body, "limit", 20), 100))
        return {"ok": True, **self._admin_store().recent(limit)}

    def _admin_search(self, method: str, ident: TokenIdentity, body: dict, query: dict) -> dict:
        q = _qparam(query, "q", "")
        limit = min(_qint(query, body, "limit", 10), 50)
        if not q.strip():
            return {"ok": True, "query": q, "memories": [], "turns": []}
        st = self._admin_store()
        try:
            mem = st.search_memories_hybrid(q, limit=limit)
        except Exception:  # noqa: BLE001
            mem = st.search_memories(q, limit=limit)
        return {
            "ok": True,
            "query": q,
            "memories": clean_rows(mem),
            "turns": clean_rows(st.search_turns(q, limit=min(limit, 10))),
        }

    def _admin_remember(self, method: str, ident: TokenIdentity, body: dict, query: dict) -> dict:
        content = str(body.get("content") or "").strip()
        if not content:
            raise BadRequest("content 不能为空")
        try:
            importance = int(body.get("importance") if body.get("importance") is not None else 5)
        except (TypeError, ValueError):
            raise BadRequest("importance 必须是整数") from None
        saved = self._admin_store().remember(
            content,
            kind=str(body.get("kind") or "note"),
            tags=str(body.get("tags") or ""),
            importance=importance,
            source=str(body.get("source") or "console"),
        )
        return {"ok": True, "saved": saved}

    def _admin_forget(self, method: str, ident: TokenIdentity, body: dict, query: dict) -> dict:
        try:
            mem_id = int(body.get("id"))
        except (TypeError, ValueError):
            raise BadRequest("id 必须是整数") from None
        return {"ok": True, "result": self._admin_store().forget(mem_id)}

    def _admin_restore(self, method: str, ident: TokenIdentity, body: dict, query: dict) -> dict:
        try:
            mem_id = int(body.get("id"))
        except (TypeError, ValueError):
            raise BadRequest("id 必须是整数") from None
        return {"ok": True, "result": self._admin_store().restore(mem_id)}

    def _admin_drain(self, method: str, ident: TokenIdentity, body: dict, query: dict) -> dict:
        res = self._admin_store().drain_vectors(
            max_items=as_int(body.get("max_items"), 0),
            force=bool(body.get("force", False)),
        )
        return {"ok": True, **res}

    def _admin_doctor(self, method: str, ident: TokenIdentity, body: dict, query: dict) -> dict:
        return self._admin_store().doctor()

    def _admin_prune(self, method: str, ident: TokenIdentity, body: dict, query: dict) -> dict:
        return {"ok": True, "result": self._admin_store().prune(dry_run=bool(body.get("dry_run", True)))}

    def _admin_tokens(self, method: str, ident: TokenIdentity, body: dict, query: dict) -> dict:
        if method == "GET":
            now = time.time()
            with self._activity_lock:
                acts = {k: dict(v) for k, v in self._activity.items()}
            items = []
            for name, ent in sorted(self.tokens.entries().items()):
                info = self.tokens.resolve_entry(name, ent)
                act = acts.get(name) or {}
                last_ts = float(act.get("last_ts") or 0)
                items.append({
                    "name": info["name"],
                    "machine": info["machine"],
                    "agent": info["agent"],
                    "client": info["client"] or str(act.get("detected_client") or ""),
                    "role": info["role"],
                    "masked": info["masked"],
                    "online": bool(last_ts and (now - last_ts) <= 300.0),
                    "requests": int(act.get("requests") or 0),
                    "last_seen": str(act.get("last_seen") or ""),
                    "last_ip": str(act.get("last_ip") or ""),
                    "last_channel": str(act.get("last_channel") or ""),
                    "last_ua": str(act.get("last_ua") or ""),
                    "last_path": str(act.get("last_path") or ""),
                })
            return {"ok": True, "tokens": items}
        if method == "POST":
            machine = str(body.get("machine") or "").strip()
            agent = str(body.get("agent") or "").strip().lower()
            client = str(body.get("client") or "").strip().lower()
            role = str(body.get("role") or "").strip().lower()
            name = str(body.get("name") or "").strip()
            if not name:
                if machine and agent:
                    name = f"{machine}|{agent}"
                elif agent:
                    name = agent
            if not name:
                raise BadRequest("name 或 machine/agent 不能为空")
            eff_client = infer_client(agent, client)
            tok = self.tokens.add(name, role=role, machine=machine, agent=agent, client=eff_client)
            ent = self.tokens.entries().get(name) or {"token": tok, "role": role or "agent"}
            info = self.tokens.resolve_entry(name, ent)
            return {
                "ok": True,
                "name": info["name"],
                "machine": info["machine"],
                "agent": info["agent"],
                "client": info["client"],
                "role": info["role"],
                "token": tok,
            }
        if method == "DELETE":
            name = str(body.get("name") or _qparam(query, "name", "")).strip()
            if not name:
                raise BadRequest("name 不能为空")
            removed = self.tokens.remove(name)
            with self._activity_lock:
                self._activity.pop(name, None)
            return {"ok": True, "removed": removed}
        raise BadRequest(f"unsupported method: {method}")
