"""atbrains Memory Service(/v1)—— 统一记忆服务端。

各机器上的 brain 客户端以后只走 HTTP:检索/融合/向量调度/重试全部收拢到服务端,
客户端不再本地直连 MariaDB/Redis/Qdrant。

设计要点:
  · 复用兄弟插件 brain 的 core.BrainStore(与 probe.py 同一套凭据解析与读写逻辑),
    atbrains 不重造存储层;
  · 每个客户端令牌绑定一个 machine 名,**由服务端决定**:请求体/查询串里的 machine
    一律忽略,客户端无法伪造来源机(否则可冒名写入别人的记忆);
  · 令牌表 service-tokens.json(插件目录,权限 600,首次运行自动生成 {"server": <随机令牌>});
    令牌值绝不出现在日志/错误消息/响应体里;
  · http.server 是多线程的:BrainStore 的 MySQL 连接是 thread-local、Redis 客户端自带锁,
    本模块只需给 machine→store 字典与令牌缓存加锁;
  · /v1/* 与网页控制台 /api/* 共用一个端口,但令牌体系彼此独立(Bearer vs X-Auth-Token)。
"""
from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

try:  # 以包形式导入(cli.py / `import atbrains.service`)
    from . import probe
except ImportError:  # console/server.py 把插件根目录塞进 sys.path,以顶层模块方式导入
    import probe  # type: ignore

HERE = Path(__file__).resolve().parent
TOKENS_PATH = HERE / "service-tokens.json"

# 令牌表缓存:(文件 mtime_ns, {machine: 令牌});mtime 变了就重读(CLI 在别的进程里改文件)
_tokens_lock = threading.Lock()
_tokens_cache: Optional[tuple[Optional[int], dict[str, str]]] = None

# 异常消息脱敏:即便某个驱动把连接串塞进了报错里,也不能把口令下发
_SECRET_RE = re.compile(r"(?i)\b(password|passwd|pwd|token|api[_-]?key|secret)\b\s*[=:]\s*\S+")


class BadRequest(ValueError):
    """客户端请求不合法 → 400(与服务端内部故障区分开)。"""


def _safe_error(exc: BaseException) -> str:
    """异常 → 可下发的错误消息:只留类型与脱敏后的消息,绝不 dump cfg。"""
    msg = _SECRET_RE.sub(r"\1=***", str(exc))[:400]
    return f"{type(exc).__name__}: {msg}"


def _header(headers: Any, name: str) -> str:
    """请求头大小写不敏感取值(客户端可能写 Authorization / authorization)。"""
    want = name.lower()
    for k, v in (headers or {}).items():
        if str(k).lower() == want:
            return str(v or "")
    return ""


def _extract_token(headers: Any) -> str:
    """从 Authorization: Bearer <令牌> 或 X-Memory-Token: <令牌> 里取令牌。"""
    auth = _header(headers, "Authorization")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    return _header(headers, "X-Memory-Token").strip()


def mask(token: str) -> str:
    """令牌掩码(abcd…wxyz)—— 只给 CLI 看,服务端响应里从不出现令牌。"""
    t = str(token or "")
    return f"{t[:4]}…{t[-4:]}" if len(t) > 12 else "…"


# ───────────────────────────── 令牌表 ─────────────────────────────

def _read_tokens() -> dict[str, str]:
    """读令牌表;文件不存在则生成 {"server": <随机令牌>}(权限 600)。"""
    try:
        raw = json.loads(TOKENS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {"server": secrets.token_urlsafe(32)}
        _write_tokens(data)
        return data
    except Exception:  # noqa: BLE001 —— 坏文件当空表处理,不阻断服务
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if isinstance(v, str)}


def _write_tokens(data: dict[str, str]) -> None:
    """写令牌表:临时文件 + 原子替换,权限 600。"""
    blob = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    tmp = TOKENS_PATH.with_name(TOKENS_PATH.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(blob)
    os.replace(tmp, TOKENS_PATH)
    os.chmod(TOKENS_PATH, 0o600)


def load_tokens() -> dict[str, str]:
    """令牌表 {machine: 令牌}(首次调用会自动生成 server 令牌)。"""
    global _tokens_cache
    with _tokens_lock:
        try:
            mtime: Optional[int] = TOKENS_PATH.stat().st_mtime_ns
        except OSError:
            mtime = None
        if _tokens_cache is not None and _tokens_cache[0] == mtime:
            return dict(_tokens_cache[1])
        data = _read_tokens()   # 文件缺失时在这里生成
        try:
            mtime = TOKENS_PATH.stat().st_mtime_ns
        except OSError:
            pass
        _tokens_cache = (mtime, data)
        return dict(data)


def token_add(machine: str) -> str:
    """给某 machine 生成(或轮换)令牌,返回明文 —— 只在这一处返回,调用方负责只显示一次。"""
    name = str(machine or "").strip()
    if not name:
        raise BadRequest("machine 不能为空")
    with _tokens_lock:
        data = _read_tokens()
        data[name] = secrets.token_urlsafe(32)
        _write_tokens(data)
        _tokens_cache = (TOKENS_PATH.stat().st_mtime_ns, dict(data))
    return data[name]


def token_rm(machine: str) -> bool:
    """删除某 machine 的令牌;不存在返回 False。"""
    name = str(machine or "").strip()
    with _tokens_lock:
        data = _read_tokens()
        if name not in data:
            return False
        del data[name]
        _write_tokens(data)
        _tokens_cache = (TOKENS_PATH.stat().st_mtime_ns, dict(data))
        return True


# ───────────────────────────── 服务 ─────────────────────────────

def _new_store(hermes_home: str, machine: str, agent: str = "hermes") -> Any:
    """构造 brain 的 BrainStore,并覆盖来源身份。

    agent = 写入方身份（Hermes 客户端一律 hermes；MCP 通道用令牌名 omp/dsh/…）。
    machine = 来源主机；传空串表示「沿用配置里的主机名」——MCP 通道没有客户端
    主机信息，写入点就是跑本服务的这台机器，不能拿 agent 名冒充主机。

    brain 插件位置与 sys.path 处理沿用 probe.py 的方式(_find_brain_plugin);
    cfg 含 MariaDB/Redis 口令,只在进程内传递,绝不进日志/响应。
    """
    brain_dir = probe._find_brain_plugin()
    if str(brain_dir) not in sys.path:
        sys.path.insert(0, str(brain_dir))
    from core import BrainStore, resolve_config  # type: ignore

    cfg = resolve_config(hermes_home)
    if machine:
        cfg["machine"] = machine            # 服务端决定来源机(客户端伪造无效)
    cfg["agent"] = agent
    return BrainStore(cfg)


class MemoryService:
    """Memory Service 的 HTTP 无关实现(路由 + 鉴权 + 每 machine 一个 BrainStore)。

    console/server.py 只做:收原始 body/headers → handle() → 回写 JSON。
    """

    # 路径 → (允许的方法, 处理方法名);除注明外全部 POST
    _ROUTES: dict[str, tuple[tuple[str, ...], str]] = {
        "/v1/health": (("GET", "POST"), "_health"),
        "/v1/memory/recall": (("POST",), "_recall"),
        "/v1/memory/remember": (("POST",), "_remember"),
        "/v1/memory/forget": (("POST",), "_forget"),
        "/v1/memory/recent": (("POST",), "_recent"),
        "/v1/memory/turn": (("POST",), "_turn"),
        "/v1/memory/status": (("GET", "POST"), "_status"),
    }
    # 不需要令牌的探活端点(健康检查不该要求凭据)
    _OPEN = frozenset({"/v1/health"})

    def __init__(self, hermes_home: Optional[str] = None) -> None:
        self.hermes_home = hermes_home or probe.hermes_home()
        self._stores: dict[str, Any] = {}
        self._lock = threading.Lock()

    # ---------------------------------------------------------- 鉴权

    @staticmethod
    def authorize(headers: Any) -> Optional[str]:
        """请求头 → machine 名;未带令牌或匹配不上返回 None。

        逐个常量时间比较(hmac.compare_digest),不区分"没带令牌"与"令牌错"。
        """
        got = _extract_token(headers)
        if not got:
            return None
        enc = got.encode("utf-8", "replace")
        for machine, known in load_tokens().items():
            if known and hmac.compare_digest(enc, known.encode("utf-8", "replace")):
                return machine
        return None

    # ---------------------------------------------------------- store

    def store_for(self, machine: str, agent: str = "hermes") -> Any:
        """按 (agent, machine) 取 BrainStore(构造很快,锁内建,避免重复实例)。

        Hermes 客户端:/v1 传入的是令牌名 = 主机名,agent 固定 hermes。
        MCP 客户端:service 层传 machine="",agent = 令牌名，主机沿用服务端配置。
        """
        key = f"{agent}\x00{machine}"
        with self._lock:
            st = self._stores.get(key)
            if st is None:
                st = _new_store(self.hermes_home, machine, agent)
                self._stores[key] = st
            return st

    # ---------------------------------------------------------- 入口

    def handle(self, method: str, path: str, query: dict, body: dict, headers: dict) -> tuple[int, dict]:
        """唯一入口:返回 (HTTP 状态码, JSON 可序列化 dict)。

        query 预留给 GET 形态的过滤参数(当前 /v1 的入参都在 body 里)。
        """
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
            machine = MemoryService.authorize(headers)
            if machine is None:
                return 401, {"error": "unauthorized"}
            payload = body if isinstance(body, dict) else {}
            return 200, getattr(self, handler)(machine, payload, query)
        except BadRequest as e:
            return 400, {"ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001 —— 内部故障统一 500,消息已脱敏
            return 500, {"ok": False, "error": _safe_error(e)}

    # ---------------------------------------------------------- 各端点

    def _health(self) -> dict:
        """探活:不需要令牌,也不给出任何机器/计数信息。"""
        return {"ok": True, "version": probe.plugin_version(),
                "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}

    def _recall(self, machine: str, body: dict, query: dict) -> dict:
        """检索:注入文本走 brain 的 prefetch_block(与服务端一致的融合与缓存)。"""
        q = str(body.get("query") or "").strip()
        if not q:
            raise BadRequest("query 不能为空")
        session_id = str(body.get("session_id") or "")
        st = self.store_for(machine)
        text, count = st.prefetch_block(q, session_id=session_id)
        return {"ok": True, "text": text, "count": int(count), "items": self._hits(st, q)}

    @staticmethod
    def _hits(st: Any, q: str) -> list[dict]:
        """结构化命中(给客户端做展示/审计;权威内容仍是注入文本 text)。

        brain 的 RRF 融合不对外给分数,只给命中途径 via(kw/vec/both)与向量余弦 vscore,
        所以这里如实只透出这两项,不编造一个排序分。
        """
        limit = max(1, min(int(st.cfg.get("prefetch_memories") or 5), 20))
        out = []
        for m in st.search_memories_hybrid(q, limit=limit):
            vscore = m.get("vscore")
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
                "preview": str(m.get("content") or "")[:240],
            })
        return out

    def _remember(self, machine: str, body: dict, query: dict) -> dict:
        content = str(body.get("content") or "").strip()
        if not content:
            raise BadRequest("content 不能为空")
        try:
            importance = int(body.get("importance") if body.get("importance") is not None else 5)
        except (TypeError, ValueError):
            raise BadRequest("importance 必须是整数") from None
        saved = self.store_for(machine).remember(
            content,
            kind=str(body.get("kind") or "note"),
            tags=str(body.get("tags") or ""),
            importance=importance,
            memory_type=str(body.get("memory_type") or ""),
        )
        return {"ok": True, "saved": saved}

    def _forget(self, machine: str, body: dict, query: dict) -> dict:
        try:
            mem_id = int(body.get("id"))
        except (TypeError, ValueError):
            raise BadRequest("id 必须是整数") from None
        return {"ok": True, "result": self.store_for(machine).forget(mem_id)}

    def _recent(self, machine: str, body: dict, query: dict) -> dict:
        try:
            limit = int(body.get("limit") if body.get("limit") is not None else 20)
        except (TypeError, ValueError):
            raise BadRequest("limit 必须是整数") from None
        limit = max(1, min(limit, 100))
        st = self.store_for(machine)
        return {"ok": True, "hot": st.hot_recent(limit), "turns": st.recent_turns(limit)}

    def _turn(self, machine: str, body: dict, query: dict) -> dict:
        session_id = str(body.get("session_id") or "").strip()
        user = str(body.get("user") or "").strip()
        assistant = str(body.get("assistant") or "").strip()
        for name, val in (("session_id", session_id), ("user", user), ("assistant", assistant)):
            if not val:
                raise BadRequest(f"{name} 不能为空")
        turn_id = self.store_for(machine).record_turn(
            session_id, user, assistant, platform=str(body.get("platform") or ""))
        return {"ok": True, "id": int(turn_id)}

    def _status(self, machine: str, body: dict, query: dict) -> dict:
        st = self.store_for(machine)
        return {"ok": True, "machine": machine, "agent": st.agent, "ping": st.ping(), "stats": st.stats()}
