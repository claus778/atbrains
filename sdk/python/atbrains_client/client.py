"""atbrains 通用 HTTP 客户端 SDK（纯 Python 标准库，零外部依赖）。"""
from __future__ import annotations

import collections
import http.client
import json
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from .config import ClientConfig, normalize_base_url, resolve_client_config
from .spool import OfflineSpool


class AtbrainsClientError(RuntimeError):
    """服务端 /v1 调用异常。`transient=True` 表示可稍后重试或入离线队列。"""

    def __init__(self, message: str, *, status: Optional[int] = None,
                 body: str = "", transient: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.body = body
        self.transient = transient


def _is_private_host(host: str) -> bool:
    host = str(host or "").strip().strip("[]").lower()
    if host in ("127.0.0.1", "localhost", "::1", "0.0.0.0"):
        return True
    parts = host.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        a, b = int(parts[0]), int(parts[1])
        if a == 10 or (a == 192 and b == 168) or (a == 172 and 16 <= b <= 31):
            return True
    return False


def _is_transient_exc(exc: BaseException) -> bool:
    if isinstance(exc, AtbrainsClientError):
        return exc.transient
    return isinstance(exc, (TimeoutError, ConnectionError, OSError, urllib.error.URLError))


class AtbrainsClient:
    """面向任意 Python Agent 的 atbrains `/v1` 瘦客户端。"""

    def __init__(
        self,
        base_url: str = "",
        token: str = "",
        *,
        timeout_ms: int = 1200,
        write_timeout_ms: int = 4000,
        spool_dir: str = "",
        spool_max_bytes: int = 10 * 1024 * 1024,
        recall_cache_ttl: int = 60,
        recall_cache_max: int = 128,
        expected_protocol_major: int = 2,
    ) -> None:
        self.base_url = normalize_base_url(base_url)
        self.token = str(token or "").strip()
        self.timeout_ms = max(200, int(timeout_ms))
        self.write_timeout_ms = max(500, int(write_timeout_ms))
        self.expected_protocol_major = int(expected_protocol_major)
        self.spool: Optional[OfflineSpool] = (
            OfflineSpool(spool_dir, max_bytes=spool_max_bytes) if str(spool_dir or "").strip() else None
        )
        self._cache_ttl = max(0, int(recall_cache_ttl))
        self._cache_max = max(1, int(recall_cache_max))
        self._cache: collections.OrderedDict[tuple, tuple[float, dict[str, Any]]] = collections.OrderedDict()
        self._cache_lock = threading.Lock()
        self._tls = threading.local()
        self._no_proxy_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._protocol_checked: Optional[bool] = None

    @classmethod
    def from_config(cls, config: Optional[ClientConfig] = None, **overrides: Any) -> "AtbrainsClient":
        cfg = config or resolve_client_config(**overrides)
        return cls(
            base_url=cfg.base_url,
            token=cfg.token,
            timeout_ms=cfg.timeout_ms,
            write_timeout_ms=cfg.write_timeout_ms,
            spool_dir=cfg.spool_dir,
            spool_max_bytes=cfg.spool_max_bytes,
            recall_cache_ttl=cfg.recall_cache_ttl,
            recall_cache_max=cfg.recall_cache_max,
            expected_protocol_major=cfg.expected_protocol_major,
        )

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.token)

    def close(self) -> None:
        """关闭当前线程复用的 HTTP keep-alive 连接。"""
        pool = getattr(self._tls, "pool", None)
        if isinstance(pool, dict):
            for conn, _ in list(pool.values()):
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
            pool.clear()

    # ---------------------------------------------------------- HTTP 传输

    def _use_keepalive(self, host: str) -> bool:
        if _is_private_host(host):
            return True
        try:
            if not urllib.request.getproxies():
                return True
            return bool(urllib.request.proxy_bypass(host))
        except Exception:  # noqa: BLE001
            return False

    def _keepalive_request(self, method: str, url: str, timeout_s: float,
                           headers: dict[str, str], body_bytes: Optional[bytes]) -> tuple[int, str]:
        parts = urllib.parse.urlsplit(url)
        scheme = (parts.scheme or "").lower()
        host = parts.hostname or ""
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        key = (scheme, host, parts.port)
        pool: dict[tuple, tuple[Any, float]] = getattr(self._tls, "pool", None)
        if pool is None:
            pool = {}
            self._tls.pool = pool

        retry_errors = (
            http.client.RemoteDisconnected,
            http.client.BadStatusLine,
            http.client.CannotSendRequest,
            http.client.IncompleteRead,
            BrokenPipeError,
            ConnectionResetError,
            ssl.SSLError,
        )
        conn = None
        from_pool = False
        last_exc: Optional[BaseException] = None
        for attempt in (0, 1):
            if conn is None:
                entry = pool.pop(key, None)
                if entry is not None:
                    cand, ts = entry
                    if cand.sock is not None and time.monotonic() - ts <= 45.0:
                        conn, from_pool = cand, True
                    else:
                        try:
                            cand.close()
                        except Exception:  # noqa: BLE001
                            pass
            if conn is None:
                ccls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
                conn = ccls(host, parts.port, timeout=timeout_s)
                from_pool = False
            elif from_pool:
                conn.timeout = timeout_s
                if conn.sock is not None:
                    try:
                        conn.sock.settimeout(timeout_s)
                    except OSError:
                        pass
            try:
                conn.request(method, path, body=body_bytes, headers=headers)
                resp = conn.getresponse()
                status = int(resp.status)
                text = resp.read().decode("utf-8", "replace")
            except Exception as exc:  # noqa: BLE001
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
                pool.pop(key, None)
                conn, last_exc = None, exc
                if attempt == 0 and (isinstance(exc, retry_errors) or (from_pool and isinstance(exc, OSError))):
                    continue
                raise AtbrainsClientError(f"{type(exc).__name__}: {exc}", transient=True) from exc

            if resp.will_close or len(pool) >= 4:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
            else:
                pool[key] = (conn, time.monotonic())
            return status, text
        raise AtbrainsClientError(f"request failed: {last_exc}", transient=True)

    def request(
        self,
        method: str,
        path: str,
        body: Optional[dict[str, Any]] = None,
        *,
        timeout_ms: Optional[int] = None,
        auth: bool = True,
    ) -> dict[str, Any]:
        if not self.base_url:
            raise AtbrainsClientError("未配置 atbrains base_url", transient=True)
        if auth and not self.token:
            raise AtbrainsClientError("未配置 atbrains Bearer 令牌", transient=False)
        url = f"{self.base_url}{path}"
        timeout_s = max(0.05, float(timeout_ms or self.timeout_ms) / 1000.0)
        headers: dict[str, str] = {"Accept": "application/json", "User-Agent": "atbrains-client/1.0"}
        if auth:
            headers["Authorization"] = f"Bearer {self.token}"
        raw_body: Optional[bytes] = None
        if body is not None:
            raw_body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"

        host = urllib.parse.urlsplit(url).hostname or ""
        if self._use_keepalive(host) and urllib.parse.urlsplit(url).scheme in ("http", "https"):
            status, text = self._keepalive_request(method.upper(), url, timeout_s, headers, raw_body)
        else:
            req = urllib.request.Request(url, data=raw_body, headers=headers, method=method.upper())
            opener = self._no_proxy_opener if _is_private_host(host) else None
            try:
                with (opener.open(req, timeout=timeout_s) if opener else urllib.request.urlopen(req, timeout=timeout_s)) as resp:
                    status = int(resp.status)
                    text = resp.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                status = int(exc.code)
                text = exc.read().decode("utf-8", "replace")
            except Exception as exc:  # noqa: BLE001
                raise AtbrainsClientError(f"{type(exc).__name__}: {exc}", transient=True) from exc

        data: dict[str, Any] = {}
        if text.strip():
            try:
                parsed = json.loads(text)
                data = parsed if isinstance(parsed, dict) else {"data": parsed}
            except json.JSONDecodeError:
                data = {"raw": text[:400]}
        if status >= 400:
            msg = str(data.get("error") or data.get("message") or text[:200] or f"HTTP {status}")
            transient = status in (429, 502, 503, 504) or status >= 500
            raise AtbrainsClientError(f"HTTP {status}: {msg}", status=status, body=text[:400], transient=transient)
        return data

    def _write_or_spool(self, path: str, payload: dict[str, Any], *,
                        timeout_ms: Optional[int] = None,
                        allow_spool: bool = True) -> dict[str, Any]:
        self.invalidate_cache()
        try:
            res = self.request("POST", path, payload, timeout_ms=timeout_ms or self.write_timeout_ms)
            # 连接通畅时顺带消化少量积压队列
            if self.spool is not None and self.spool.size_bytes() > 0:
                try:
                    self.flush_spool(max_items=20)
                except Exception:  # noqa: BLE001
                    pass
            return res
        except AtbrainsClientError as exc:
            if allow_spool and exc.transient and self.spool is not None:
                if self.spool.enqueue(path, payload):
                    return {"ok": True, "queued": True, "spool_pending": self.spool.count()}
            raise

    # ---------------------------------------------------------- 本地缓存与离线队列

    def invalidate_cache(self) -> None:
        with self._cache_lock:
            self._cache.clear()

    def flush_spool(self, max_items: int = 100) -> dict[str, int]:
        if self.spool is None:
            return {"sent": 0, "dropped": 0, "remaining": 0}
        return self.spool.drain(
            lambda p, body: self.request("POST", p, body, timeout_ms=self.write_timeout_ms),
            is_transient=_is_transient_exc,
            max_items=max_items,
        )

    def spool_status(self) -> dict[str, Any]:
        if self.spool is None:
            return {"enabled": False, "pending": 0, "bytes": 0}
        return {
            "enabled": True,
            "dir": str(self.spool.dir),
            "pending": self.spool.count(),
            "bytes": self.spool.size_bytes(),
        }

    # ---------------------------------------------------------- 协议与探活

    def health(self) -> dict[str, Any]:
        return self.request("GET", "/v1/health", None, auth=False)

    def protocol_ok(self) -> bool:
        if self._protocol_checked is not None:
            return self._protocol_checked
        try:
            h = self.health()
            major = int(str(h.get("version") or "").split(".")[0])
            self._protocol_checked = major == self.expected_protocol_major
            return self._protocol_checked
        except Exception:  # noqa: BLE001
            # 探活网络失败不锁死，允许后续请求按自身超时/重试处理
            return True

    def status(self, *, light: bool = False) -> dict[str, Any]:
        return self.request("POST", "/v1/memory/status", {"light": bool(light)})

    # ---------------------------------------------------------- 记忆读写与召回

    def recall(
        self,
        query: str,
        *,
        session_id: str = "",
        limit: Optional[int] = None,
        kind: Optional[str] = None,
        agent: Optional[str] = None,
        include_turns: bool = True,
        include_claims: bool = False,
        include_turn_items: bool = False,
        use_cache: bool = True,
    ) -> dict[str, Any]:
        q = str(query or "").strip()
        if not q:
            return {"ok": True, "text": "", "count": 0, "items": []}
        key = (q, session_id, limit, kind, agent, include_turns, include_claims, include_turn_items)
        if use_cache and self._cache_ttl > 0:
            with self._cache_lock:
                hit = self._cache.get(key)
                if hit is not None:
                    ts, val = hit
                    if time.monotonic() - ts <= self._cache_ttl:
                        self._cache.move_to_end(key)
                        return dict(val)
                    self._cache.pop(key, None)

        payload: dict[str, Any] = {"query": q, "include_turns": bool(include_turns)}
        if session_id:
            payload["session_id"] = session_id
        if limit is not None:
            payload["limit"] = int(limit)
        if kind:
            payload["kind"] = kind
        if agent:
            payload["agent"] = agent
        if include_claims:
            payload["include_claims"] = True
        if include_turn_items:
            payload["include_turn_items"] = True

        res = self.request("POST", "/v1/memory/recall", payload)
        if use_cache and self._cache_ttl > 0:
            with self._cache_lock:
                self._cache[key] = (time.monotonic(), dict(res))
                while len(self._cache) > self._cache_max:
                    self._cache.popitem(last=False)
        return res

    def prefetch(self, query: str, *, session_id: str = "", limit: Optional[int] = None) -> tuple[str, int]:
        res = self.recall(query, session_id=session_id, limit=limit)
        return str(res.get("text") or ""), int(res.get("count") or 0)

    def remember(
        self,
        content: str,
        *,
        kind: str = "note",
        tags: str = "",
        importance: int = 5,
        memory_type: str = "",
        source: str = "",
        session_id: str = "",
        allow_spool: bool = True,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "content": str(content),
            "kind": str(kind or "note"),
            "tags": str(tags or ""),
            "importance": int(importance),
        }
        if memory_type:
            payload["memory_type"] = str(memory_type)
        if source:
            payload["source"] = str(source)
        if session_id:
            payload["session_id"] = str(session_id)
        return self._write_or_spool("/v1/memory/remember", payload, allow_spool=allow_spool)

    def forget(self, mem_id: int, *, allow_spool: bool = True) -> dict[str, Any]:
        return self._write_or_spool("/v1/memory/forget", {"id": int(mem_id)}, allow_spool=allow_spool)

    def restore(self, mem_id: int) -> dict[str, Any]:
        self.invalidate_cache()
        return self.request("POST", "/v1/memory/restore", {"id": int(mem_id)}, timeout_ms=self.write_timeout_ms)

    def deactivate(self, content: str, *, allow_spool: bool = True) -> dict[str, Any]:
        return self._write_or_spool("/v1/memory/deactivate", {"content": str(content)}, allow_spool=allow_spool)

    def recent(self, limit: int = 20) -> dict[str, Any]:
        return self.request("POST", "/v1/memory/recent", {"limit": int(limit)})

    # ---------------------------------------------------------- 对话归档与转录

    def turn(
        self,
        session_id: str,
        user: str,
        assistant: str = "",
        *,
        platform: str = "",
        author: str = "",
        messages: Optional[list[dict[str, Any]]] = None,
        total: Optional[int] = None,
        allow_spool: bool = True,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "session_id": str(session_id),
            "user": str(user),
            "assistant": str(assistant or ""),
            "platform": str(platform or ""),
            "author": str(author or ""),
        }
        if messages is not None:
            payload["messages"] = messages
        if total is not None:
            payload["total"] = int(total)
        return self._write_or_spool("/v1/memory/turn", payload, allow_spool=allow_spool)

    def turn_detail(self, turn_id: int = 0, *, session_id: str = "", max_chars: int = 1500) -> dict[str, Any]:
        payload: dict[str, Any] = {"max_chars": int(max_chars)}
        if turn_id:
            payload["turn_id"] = int(turn_id)
        if session_id:
            payload["session_id"] = str(session_id)
        return self.request("POST", "/v1/memory/turn/detail", payload)

    def archive(self, session_id: str, kind: str, payload: str, *,
                platform: str = "", allow_spool: bool = True) -> dict[str, Any]:
        return self._write_or_spool(
            "/v1/memory/archive",
            {"session_id": str(session_id), "kind": str(kind), "payload": str(payload), "platform": str(platform)},
            allow_spool=allow_spool,
        )

    def touch_session(self, session_id: str, platform: str = "", *, allow_spool: bool = True) -> dict[str, Any]:
        return self._write_or_spool(
            "/v1/memory/session/touch",
            {"session_id": str(session_id), "platform": str(platform)},
            allow_spool=allow_spool,
        )

    # ---------------------------------------------------------- 知识层（主张 / 情节 / 醒来包）

    def claim_remember(
        self,
        subject: str,
        predicate: str,
        value: str,
        *,
        kind: str = "fact",
        basis: str = "direct_report",
        valid_from: Optional[str] = None,
        valid_to: Optional[str] = None,
        turn_id: Optional[int] = None,
        seq: Optional[int] = None,
        quote: str = "",
        confidence: int = 80,
        importance: int = 5,
        shared: bool = False,
        allow_spool: bool = True,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "subject": subject,
            "predicate": predicate,
            "value": value,
            "kind": kind,
            "basis": basis,
            "confidence": int(confidence),
            "importance": int(importance),
        }
        if shared:
            payload["shared"] = True
        if valid_from:
            payload["valid_from"] = valid_from
        if valid_to:
            payload["valid_to"] = valid_to
        if turn_id is not None:
            payload["turn_id"] = int(turn_id)
        if seq is not None:
            payload["seq"] = int(seq)
        if quote:
            payload["quote"] = quote
        return self._write_or_spool("/v1/claim/remember", payload, allow_spool=allow_spool)

    def claim_recall(self, *, query: str = "", subject: str = "", predicate: str = "",
                     limit: int = 10, history: bool = False) -> dict[str, Any]:
        return self.request("POST", "/v1/claim/recall", {
            "query": query,
            "subject": subject,
            "predicate": predicate,
            "limit": int(limit),
            "history": bool(history),
        })

    def claim_retire(self, claim_id: int, reason: str = "") -> dict[str, Any]:
        return self.request("POST", "/v1/claim/retire", {"claim_id": int(claim_id), "reason": reason},
                            timeout_ms=self.write_timeout_ms)

    def episode_set(
        self,
        turn_id: int,
        *,
        goal: Optional[str] = None,
        decisions: Any = None,
        open_items: Any = None,
        blockers: Any = None,
        next_step: Optional[str] = None,
        status: Optional[str] = None,
        session_id: str = "",
        allow_spool: bool = True,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"turn_id": int(turn_id)}
        for k, v in (("goal", goal), ("decisions", decisions), ("open_items", open_items),
                     ("blockers", blockers), ("next_step", next_step), ("status", status)):
            if v is not None:
                payload[k] = v
        if session_id:
            payload["session_id"] = session_id
        return self._write_or_spool("/v1/episode/set", payload, allow_spool=allow_spool)

    def episode_get(self, turn_id: int = 0, *, session_id: str = "") -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if turn_id:
            payload["turn_id"] = int(turn_id)
        if session_id:
            payload["session_id"] = session_id
        return self.request("POST", "/v1/episode/get", payload)

    def context(
        self,
        *,
        focus: str = "",
        limit: int = 8,
        include_turns: bool = True,
        include_episodes: bool = True,
        max_chars: int = 400,
        fmt: str = "json",
    ) -> dict[str, Any]:
        return self.request("POST", "/v1/context", {
            "focus": focus,
            "limit": int(limit),
            "include_turns": bool(include_turns),
            "include_episodes": bool(include_episodes),
            "max_chars": int(max_chars),
            "format": fmt,
        })

    def handoff(
        self,
        note: str,
        *,
        next_step: Optional[str] = None,
        open_items: Optional[list[str]] = None,
        turn_id: Optional[int] = None,
        importance: int = 7,
        session_id: str = "",
        allow_spool: bool = True,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"note": note, "importance": int(importance)}
        if next_step:
            payload["next_step"] = next_step
        if open_items:
            payload["open_items"] = open_items
        if turn_id is not None:
            payload["turn_id"] = int(turn_id)
        if session_id:
            payload["session_id"] = session_id
        return self._write_or_spool("/v1/handoff", payload, allow_spool=allow_spool)
