"""atbrains — Hermes 记忆提供者（纯 HTTP 薄适配器，走 atbrains-server /v1 API）。

本适配器不直接连接 MariaDB / Redis / Qdrant，不持有数据库密码，不执行表结构迁移或后台蒸馏/向量任务：
  * 全部读写与召回统一走 `AtbrainsClient` (`/v1/*`)；
  * 服务端暂不可达时，写操作自动进入本地有界 JSONL 离线队列，恢复后按序重放；
  * 工具定义与协议由 `atbrains_client.schemas` 统一提供。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any, Optional


def _ensure_sdk() -> None:
    """按优先级定位纯标准库 `atbrains_client`（已安装包 > 插件内嵌 `_sdk` > 仓库 `sdk/python`）。"""
    try:
        import atbrains_client  # noqa: F401
        return
    except ImportError:
        pass
    here = Path(__file__).resolve().parent
    for cand in (here / "_sdk", here.parents[1] / "sdk" / "python"):
        if (cand / "atbrains_client" / "__init__.py").is_file() and str(cand) not in sys.path:
            sys.path.insert(0, str(cand))
            return


_ensure_sdk()

from atbrains_client import (  # noqa: E402
    AtbrainsClient,
    AtbrainsClientError,
    ClientConfig,
    openai_tool_schemas,
    resolve_client_config,
)

try:  # Hermes 运行时导入真模块；离线测试/独立检验时提供最小占位
    from agent.memory_provider import MemoryProvider, RecallStatus  # type: ignore
except ImportError:
    class MemoryProvider:  # type: ignore[no-redef]
        pass

    class RecallStatus:  # type: ignore[no-redef]
        def __init__(self, label: str, count: int) -> None:
            self.label = label
            self.count = count

try:
    from agent.memory_provider import spawn_context_thread  # type: ignore
except ImportError:
    def spawn_context_thread(target, *, name, daemon=True, args=(), kwargs=None):  # type: ignore[no-redef]
        return threading.Thread(target=target, args=args, kwargs=kwargs or {}, name=name, daemon=daemon)

logger = logging.getLogger(__name__)

PROVIDER_NAME = "atbrains-memory"
PROVIDER_LABEL = "atbrains"

SYSTEM_PROMPT_BLOCK = """## atbrains 共享记忆库（通用 HTTP 服务，跨 Agent 共享）
- 长期事实/偏好/决策：随时用 `atbrains_remember` 保存，检索用 `atbrains_recall`；跨会话、跨 Agent 生效。
- 结构化主张（「X 的 Y 是 Z」这类可核对的值）：用 `atbrains_claim_remember(subject, predicate, value)`，同槽位自动递增版本；`atbrains_claim_recall` 取当前值或历史。
- 会话开场/收尾：用 `atbrains_context` 取回醒来包；任务交接用 `atbrains_handoff`；单轮目标与未完成项用 `atbrains_episode_set` / `atbrains_episode_get`。
- 需要最近动态 → `atbrains_recent`；复盘某一轮工具过程 → `atbrains_turn_detail`；库状态 → `atbrains_stats`。"""

TOOL_SCHEMAS: list[dict[str, Any]] = openai_tool_schemas()


def _hermes_home(hermes_home: Optional[str] = None) -> Path:
    if hermes_home:
        return Path(hermes_home).expanduser()
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".hermes"


def _resolve_client_cfg(hermes_home: Optional[str] = None) -> ClientConfig:
    home = _hermes_home(hermes_home)
    cfg = resolve_client_config(str(home), client_name="hermes")
    if not cfg.spool_dir:
        cfg.spool_dir = str(home / "atbrains-spool")
    return cfg


class AtbrainsMemoryProvider(MemoryProvider):
    """基于 `/v1` HTTP SDK 的薄 Hermes 记忆提供者。"""

    def __init__(self, *, client: Optional[AtbrainsClient] = None) -> None:
        self._client: Optional[AtbrainsClient] = client
        self._cfg: Optional[ClientConfig] = None
        self._session_id: str = ""
        self._platform: str = ""
        self._hermes_home: str = ""
        self._primary: bool = True
        self._last_recall: Optional[RecallStatus] = None
        self._init_lock = threading.Lock()

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    def is_available(self) -> bool:
        try:
            cfg = _resolve_client_cfg()
            return bool(cfg.base_url and cfg.token)
        except Exception:  # noqa: BLE001
            return False

    def unavailable_reason(self) -> str:
        try:
            cfg = _resolve_client_cfg()
        except Exception as exc:  # noqa: BLE001
            return f"atbrains 配置读取失败: {exc}"
        missing = []
        if not cfg.base_url:
            missing.append("service_url / ATBRAINS_URL")
        if not cfg.token:
            missing.append("ATBRAINS_TOKEN / ATBRAINS_REMOTE_TOKEN")
        if missing:
            return (
                "atbrains 客户端缺少配置 " + ", ".join(missing)
                + "（可运行 `hermes atbrains-memory tokset <token> --url <url>`，或手动写入 ~/.hermes/atbrains.json 与 ~/.hermes/.env）。"
            )
        return ""

    def initialize(self, session_id: str, **kwargs) -> None:
        with self._init_lock:
            self._session_id = str(session_id or "")
            self._hermes_home = str(kwargs.get("hermes_home") or _hermes_home())
            self._platform = str(kwargs.get("platform") or "")
            self._primary = str(kwargs.get("agent_context") or "primary") in ("", "primary")
            if self._client is not None:
                return
            try:
                cfg = _resolve_client_cfg(self._hermes_home)
                self._cfg = cfg
                if cfg.base_url and cfg.token:
                    self._client = AtbrainsClient.from_config(cfg)
                    self._checkin_async(self._client)
                else:
                    self._client = None
            except Exception as exc:  # noqa: BLE001
                logger.warning("atbrains: 客户端初始化失败 (%s)", exc)
                self._client = None

    @staticmethod
    def _checkin_async(cli: AtbrainsClient) -> None:
        """启动时异步轻探活一次，向服务端校验令牌并立即上报在线状态。"""
        def _probe() -> None:
            try:
                cli.status(light=True)
            except Exception as exc:  # noqa: BLE001
                logger.debug("atbrains: 启动在线上报跳过: %s", exc)

        try:
            threading.Thread(target=_probe, name="atbrains-checkin", daemon=True).start()
        except Exception:  # noqa: BLE001
            pass

    def _require_client(self) -> AtbrainsClient:
        if self._client is None:
            raise AtbrainsClientError("atbrains 客户端未初始化（检查 service_url 与令牌）")
        return self._client

    # -------------------------------------------------- 召回

    def system_prompt_block(self) -> str:
        return SYSTEM_PROMPT_BLOCK

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        cli = self._client
        if cli is None or not query or not cli.protocol_ok():
            self._last_recall = None
            return ""
        try:
            text, count = cli.prefetch(query, session_id=session_id or self._session_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("atbrains: prefetch 失败: %s", exc)
            self._last_recall = None
            return ""
        if text:
            self._last_recall = RecallStatus(PROVIDER_LABEL, int(count) if count else 0)
            return text
        self._last_recall = None
        return ""

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        cli = self._client
        if cli is None or not query or not cli.protocol_ok():
            return

        def _warm() -> None:
            try:
                cli.prefetch(query, session_id=session_id or self._session_id)
            except Exception as exc:  # noqa: BLE001
                logger.debug("atbrains: 后台召回预热失败: %s", exc)

        try:
            spawn_context_thread(_warm, name="atbrains-prefetch-warm").start()
        except Exception:  # noqa: BLE001
            threading.Thread(target=_warm, name="atbrains-prefetch-warm", daemon=True).start()

    def recall_status(self) -> Optional[RecallStatus]:
        return self._last_recall

    # -------------------------------------------------- 写入（失败自动入本地离线队列）

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[list[dict[str, Any]]] = None,
        turn_author: Optional[dict[str, Any]] = None,
    ) -> None:
        cli = self._client
        if cli is None or not self._primary or not cli.protocol_ok():
            return
        author = ""
        if isinstance(turn_author, dict):
            author = str(turn_author.get("name") or turn_author.get("id") or "")[:64]
        try:
            cli.turn(
                session_id or self._session_id,
                user_content or "",
                assistant_content or "",
                platform=self._platform,
                author=author,
                messages=messages,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("atbrains: sync_turn 写入失败: %s", exc)

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        cli = self._client
        if cli is None or not self._primary or not cli.protocol_ok():
            return
        prev = str(metadata.get("previous_content") or "") if isinstance(metadata, dict) else ""
        try:
            if action == "add":
                if content:
                    cli.remember(
                        content, kind="builtin-memory", tags=str(target or ""),
                        importance=6, source="memory_write", session_id=self._session_id,
                    )
            elif action == "replace":
                if content:
                    cli.remember(
                        content, kind="builtin-memory", tags=str(target or ""),
                        importance=6, source="memory_write", session_id=self._session_id,
                    )
                if prev and prev != content:
                    cli.deactivate(prev)
            elif action == "remove":
                target_content = prev or content
                if target_content:
                    cli.deactivate(target_content)
        except Exception as exc:  # noqa: BLE001
            logger.warning("atbrains: on_memory_write 镜像失败: %s", exc)

    def on_pre_compress(self, messages: list[dict[str, Any]]) -> str:
        cli = self._client
        if cli is None or not self._primary or not cli.protocol_ok():
            return ""
        try:
            payload: list[dict[str, Any]] = []
            for m in messages or []:
                if not isinstance(m, dict):
                    continue
                role = str(m.get("role") or "")
                if role not in ("user", "assistant"):
                    continue
                text = m.get("content")
                if isinstance(text, list):
                    text = " ".join(
                        str(p.get("text") or "")
                        for p in text
                        if isinstance(p, dict) and p.get("type") == "text"
                    )
                s = str(text or "")
                if not s.strip():
                    continue
                payload.append({"role": role, "content": s[:2000]})
            if payload:
                cli.archive(
                    self._session_id,
                    "precompress",
                    json.dumps(payload, ensure_ascii=False),
                    platform=self._platform,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("atbrains: on_pre_compress 存档失败: %s", exc)
        return ""

    def on_session_end(self, messages: list[dict[str, Any]]) -> None:
        cli = self._client
        if cli is None or not self._primary or not self._session_id:
            return
        try:
            cli.touch_session(self._session_id, self._platform)
        except Exception:  # noqa: BLE001
            pass

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        rewound: bool = False,
        **kwargs,
    ) -> None:
        self._session_id = str(new_session_id or self._session_id)

    def on_delegation(self, task: str, result: str, *, child_session_id: str = "", **kwargs) -> None:
        cli = self._client
        if cli is None or not self._primary:
            return
        try:
            content = f"子代理任务: {task}\n结果: {result}"
            cli.remember(
                content[:4000], kind="delegation", importance=4,
                source="delegation", session_id=self._session_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("atbrains: on_delegation 记录失败: %s", exc)

    # -------------------------------------------------- 工具

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return TOOL_SCHEMAS

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs) -> str:
        try:
            res = self._dispatch_tool(tool_name, args or {})
            return json.dumps(res, ensure_ascii=False, default=str)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"ok": False, "error": str(exc)[:300]}, ensure_ascii=False)

    def _dispatch_tool(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        cli = self._require_client()
        if tool_name == "atbrains_remember":
            res = cli.remember(
                str(args.get("content") or ""),
                kind=str(args.get("kind") or "note"),
                tags=str(args.get("tags") or ""),
                importance=int(args.get("importance") or 5),
                source="tool",
                session_id=self._session_id,
            )
            return {"ok": True, **res, "storage": "atbrains-server (/v1)"}

        if tool_name == "atbrains_recall":
            query = str(args.get("query") or "")
            limit = max(1, min(20, int(args.get("limit") or 5)))
            res = cli.recall(
                query,
                session_id=self._session_id,
                limit=limit,
                kind=str(args["kind"]) if args.get("kind") else None,
                agent=str(args["agent"]) if args.get("agent") else None,
                include_turns=bool(args.get("include_turns", True)),
                include_claims=True,
                include_turn_items=bool(args.get("include_turns", True)),
            )
            return {
                "ok": True,
                "memories": res.get("items") or [],
                "turns": res.get("turns") or [],
                "claims": res.get("claims") or [],
                "text": res.get("text") or "",
            }

        if tool_name == "atbrains_forget":
            if args.get("id") is None:
                raise ValueError("需要 id")
            return cli.forget(int(args["id"]))

        if tool_name == "atbrains_recent":
            n = max(1, min(50, int(args.get("n") or 10)))
            what = str(args.get("what") or "turns")
            data = cli.recent(limit=n)
            if what == "memories":
                return {"ok": True, "recent_memories": data.get("hot") or []}
            turns = data.get("turns") or []
            for t in turns:
                t["user"] = str(t.get("user") or "")[:200]
                t["assistant"] = str(t.get("assistant") or "")[:200]
            return {"ok": True, "recent_turns": turns}

        if tool_name == "atbrains_stats":
            return cli.status(light=False)

        if tool_name == "atbrains_turn_detail":
            turn_id = args.get("turn_id")
            return cli.turn_detail(
                int(turn_id) if turn_id is not None else 0,
                session_id=str(args.get("session_id") or ""),
                max_chars=int(args.get("max_chars") or 1500),
            )

        if tool_name == "atbrains_claim_remember":
            return cli.claim_remember(
                str(args.get("subject") or ""),
                str(args.get("predicate") or ""),
                str(args.get("value") or ""),
                kind=str(args.get("kind") or "fact"),
                basis=str(args.get("basis") or "direct_report"),
                valid_from=args.get("valid_from") or None,
                valid_to=args.get("valid_to") or None,
                turn_id=args.get("turn_id"),
                seq=args.get("seq"),
                quote=str(args.get("quote") or ""),
                confidence=int(args["confidence"]) if args.get("confidence") is not None else 80,
                importance=int(args["importance"]) if args.get("importance") is not None else 5,
            )

        if tool_name == "atbrains_claim_recall":
            return cli.claim_recall(
                query=str(args.get("query") or ""),
                subject=str(args.get("subject") or ""),
                predicate=str(args.get("predicate") or ""),
                limit=int(args.get("limit") or 10),
                history=bool(args.get("history")),
            )

        if tool_name == "atbrains_episode_set":
            if args.get("turn_id") is None:
                raise ValueError("需要 turn_id（atbrains_recent 可查最近轮次）")
            return cli.episode_set(
                int(args["turn_id"]),
                goal=args.get("goal"),
                decisions=args.get("decisions"),
                open_items=args.get("open_items"),
                blockers=args.get("blockers"),
                next_step=args.get("next_step"),
                status=args.get("status"),
                session_id=self._session_id,
            )

        if tool_name == "atbrains_episode_get":
            turn_id = args.get("turn_id")
            return cli.episode_get(
                int(turn_id) if turn_id is not None else 0,
                session_id=str(args.get("session_id") or ""),
            )

        if tool_name == "atbrains_context":
            return cli.context(
                focus=str(args.get("focus") or ""),
                limit=int(args.get("limit") or 8),
                include_turns=bool(args.get("include_turns", True)),
                include_episodes=bool(args.get("include_episodes", True)),
                max_chars=int(args.get("max_chars") or 400),
            )

        if tool_name == "atbrains_handoff":
            return cli.handoff(
                str(args.get("note") or ""),
                next_step=args.get("next_step"),
                open_items=args.get("open_items"),
                turn_id=args.get("turn_id"),
                importance=int(args.get("importance") or 7),
                session_id=self._session_id,
            )

        return {"ok": False, "error": f"未知工具 {tool_name}"}

    # -------------------------------------------------- 关停与配置

    def shutdown(self) -> None:
        cli = self._client
        if cli is not None:
            try:
                cli.flush_spool(max_items=50)
            except Exception:  # noqa: BLE001
                pass

    def get_config_schema(self) -> list[dict[str, Any]]:
        return [
            {"key": "service_url", "description": "atbrains-server 地址（如 http://127.0.0.1:8787）",
             "required": True, "default": "http://127.0.0.1:8787"},
            {"key": "ATBRAINS_TOKEN", "description": "atbrains-server Bearer 令牌",
             "secret": True, "required": True, "env_var": "ATBRAINS_TOKEN"},
            {"key": "timeout_ms", "description": "召回请求超时（毫秒）", "type": "integer", "default": 1200},
            {"key": "write_timeout_ms", "description": "归档写请求超时（毫秒）", "type": "integer", "default": 4000},
        ]

    def save_config(self, values: dict[str, Any], hermes_home: str) -> None:
        home = _hermes_home(hermes_home)
        path = home / "atbrains.json"
        existing: dict[str, Any] = {}
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict):
                existing = data
        except Exception:  # noqa: BLE001
            pass
        for k in ("service_url", "remote_url", "timeout_ms", "write_timeout_ms"):
            if k in (values or {}) and values[k] is not None:
                existing[k] = values[k]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(existing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def register(ctx) -> None:
    """记忆插件发现系统入口。"""
    ctx.register_memory_provider(AtbrainsMemoryProvider())
    try:
        skill_path = Path(__file__).resolve().parent / "skills" / "atbrains-memory" / "SKILL.md"
        if skill_path.is_file():
            ctx.register_skill(
                "memory",
                skill_path,
                "How to use the shared atbrains memory service over /v1 HTTP.",
            )
    except Exception:  # noqa: BLE001
        logger.debug("atbrains: 技能注册失败", exc_info=True)
