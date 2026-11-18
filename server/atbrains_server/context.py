"""请求级身份上下文与无状态代理视图（全进程单引擎实例共享连接/缓存/后台线程）。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class RequestContext:
    """每次请求的调用方身份，从服务端存储实例状态剥离。"""

    machine: str = ""
    agent: str = "default"
    role: str = "admin"  # "agent" | "admin"


def normalize_agent(agent: Any, default: str = "default") -> str:
    val = str(agent or "").strip().lower()
    return (val or default)[:32]


def normalize_machine(machine: Any, fallback: str = "") -> str:
    val = str(machine or "").strip()
    return (val or fallback)[:48]


def owns_record(ctx: Optional[RequestContext], row: Optional[dict[str, Any]], *, allow_shared: bool = False) -> bool:
    """校验非 admin 调用方是否有权读取敏感转录或改删记忆/主张。"""
    if ctx is None or ctx.role == "admin":
        return True
    if not row:
        return False
    row_mach = str(row.get("machine") or "").strip()
    if allow_shared and row_mach == "shared":
        return True
    row_agent = str(row.get("agent") or "").strip().lower()
    ctx_agent = normalize_agent(ctx.agent)
    if row_agent:
        if row_agent != ctx_agent:
            return False
        if ctx.machine and row_mach and row_mach not in ("server", "shared") and row_mach != ctx.machine:
            return False
        return True
    # 历史遗留行 agent 为空时，按 machine 兜底校验
    if ctx.machine and row_mach and row_mach != ctx.machine:
        return False
    return True


_SCOPED_METHODS = frozenset({
    "remember",
    "forget",
    "restore",
    "deactivate_by_content",
    "claim_remember",
    "claim_retire",
    "episode_set",
    "episode_get",
    "open_episodes",
    "record_turn",
    "turn_detail",
    "archive",
    "touch_session",
    "search_turns",
    "recent_turns",
})


class ScopedStore:
    """把单个共享 AtbrainsStore 引擎绑定到单次请求的身份视图（零状态、零额外连接）。"""

    def __init__(self, engine: Any, ctx: RequestContext) -> None:
        self._engine = engine
        self._ctx = ctx

    @property
    def machine(self) -> str:
        return self._ctx.machine or str(getattr(self._engine, "machine", "") or "")

    @property
    def agent(self) -> str:
        return self._ctx.agent or str(getattr(self._engine, "agent", "default") or "default")

    @property
    def role(self) -> str:
        return self._ctx.role

    @property
    def cfg(self) -> Any:
        return self._engine.cfg

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._engine, name)
        if name in _SCOPED_METHODS and callable(attr):
            def _wrapped(*args: Any, **kwargs: Any) -> Any:
                kwargs.setdefault("ctx", self._ctx)
                return attr(*args, **kwargs)

            return _wrapped
        return attr
