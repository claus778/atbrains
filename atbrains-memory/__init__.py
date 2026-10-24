"""atbrains — Hermes 记忆提供者：Redis（短期存储）+ MariaDB（长期存储）。

分层：
  * Redis（短期）：最近对话流、会话状态、热点记忆镜像、检索缓存 —— TTL 自动过期。
  * MariaDB（长期）：长期事实/偏好/决策、完整对话归档、会话索引、压缩前存档 —— 永久。

生命周期钩子全部接入：
  prefetch / queue_prefetch  → 每轮对话前召回相关记忆（长期库检索 + Redis 缓存）
  sync_turn                  → 每轮对话写入 Redis 流 + MariaDB 归档
  on_memory_write            → 内置 MEMORY.md/USER.md 写入镜像到双存储
  on_pre_compress            → 压缩前丢上下文的内容先存档进 MariaDB
  on_session_end / switch    → 会话边界维护
  on_delegation              → 子代理任务结果入库

工具：atbrains_remember / atbrains_recall / atbrains_forget / atbrains_recent / atbrains_stats / atbrains_turn_detail
      / atbrains_claim_remember / atbrains_claim_recall / atbrains_episode_set / atbrains_episode_get
CLI：hermes atbrains-memory <status|search|remember|recent|forget|prune|doctor>  (见 cli.py)
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from typing import Any, Optional

from agent.memory_provider import MemoryProvider, RecallStatus, spawn_context_thread

from .core import BrainError, AtbrainsStore, hermes_home_path, resolve_config

logger = logging.getLogger(__name__)

PROVIDER_NAME = "atbrains-memory"
PROVIDER_LABEL = "atbrains"


def _remote_configured(cfg: dict[str, Any]) -> bool:
    """远程模式是否配置完整（开关 + URL）。远程模式不依赖本机 Redis/MariaDB 凭据。"""
    try:
        return bool(int(cfg.get("remote_enabled") or 0)) and bool(
            str(cfg.get("remote_url") or "").strip())
    except (TypeError, ValueError):
        return False


SYSTEM_PROMPT_BLOCK = """## atbrains 记忆库（Redis 短期 + MariaDB 长期，本机自托管）
- 长期事实/偏好/决策：随时用 `atbrains_remember` 永久保存（MariaDB），检索用 `atbrains_recall`；两者跨会话生效。
- 结构化主张（「X 的 Y 是 Z」这类可核对的值）：用 `atbrains_claim_remember(subject, predicate, value)`，同槽位只递增版本、旧版本自动封版；`atbrains_claim_recall` 取当前值或历史。
- 每一轮的目标/决策/未完成项：用 `atbrains_episode_set(turn_id, …)` 挂情节头，`atbrains_episode_get` 读回。
- 短期层（Redis）自动缓存最近对话流与会话状态，到期自动清理，不需要手工维护。
- 每轮对话自动归档（MariaDB brain_turns + Redis brain:recent），工具调用/工具输出/推理另存于 brain_turn_messages（zlib 压缩），压缩前上下文自动存档，不会丢细节。
- 需要最近发生了什么 → `atbrains_recent`；要复盘某一轮的工具过程 → `atbrains_turn_detail(turn_id 或 session_id)`；库的规模/健康 → `atbrains_stats`。"""

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "atbrains_remember",
        "description": "永久保存一条长期记忆（事实/偏好/决策/待办）到 atbrains 库（MariaDB 长期存储，Redis 热点缓存）。",
        "parameters": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "记忆内容，简洁自洽，脱离上下文也能读懂。"},
                "kind": {"type": "string",
                         "description": "类别：fact | preference | decision | note | todo（默认 note）"},
                "tags": {"type": "string", "description": "逗号分隔的标签，便于检索。"},
                "importance": {"type": "integer", "minimum": 1, "maximum": 10,
                               "description": "重要度 1-10，默认 5。"},
            },
            "required": ["content"],
        },
    },
    {
        "name": "atbrains_recall",
        "description": "从 atbrains 长期库检索记忆（语义向量+关键词混合召回，也返回相关历史对话片段）。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索关键词或自然语言问题。"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "description": "返回条数，默认 5。"},
                "kind": {"type": "string", "description": "只检索某一类别（可选）。"},
                "agent": {"type": "string", "description": "只检索某个 agent 写入的记忆（hermes/omp/dsh/…，可选；默认全部共享记忆）。"},
                "include_turns": {"type": "boolean", "description": "是否附带相关历史对话，默认 true。"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "atbrains_forget",
        "description": "废止一条记忆（软删除，按 id）。id 从 atbrains_recall / atbrains_remember 返回中获得。",
        "parameters": {
            "type": "object",
            "properties": {"id": {"type": "integer", "description": "记忆的 id。"}},
            "required": ["id"],
        },
    },
    {
        "name": "atbrains_recent",
        "description": "查看最近的对话流（Redis 短期层）或最近写入的记忆（Redis 热点镜像）。",
        "parameters": {
            "type": "object",
            "properties": {
                "n": {"type": "integer", "minimum": 1, "maximum": 50, "description": "条数，默认 10。"},
                "what": {"type": "string", "description": "turns（最近对话，默认）| memories（最近记忆）"},
            },
        },
    },
    {
        "name": "atbrains_stats",
        "description": "atbrains 库统计：记忆/归档/会话数量、Redis 键数、存储占用。",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "atbrains_turn_detail",
        "description": "取回某一轮对话的完整过程：工具调用参数、工具输出、推理（brain_turn_messages 全量转录层）。"
                       "不传 turn_id 时，用 session_id 取该会话最后一轮；atbrains_recent 可先查最近的轮次 id。",
        "parameters": {
            "type": "object",
            "properties": {
                "turn_id": {"type": "integer", "description": "brain_turns.id（推荐）。"},
                "session_id": {"type": "string", "description": "会话 id（不给 turn_id 时取该会话最后一轮）。"},
                "max_chars": {"type": "integer", "minimum": 200, "maximum": 20000,
                              "description": "每条消息正文的字符上限，默认 1500。"},
            },
        },
    },
    {
        "name": "atbrains_claim_remember",
        "description": "记一条结构化主张（subject/predicate/value）：同一槽位再写只递增版本，旧版本自动封版；"
                       "可挂证据（turn_id + seq + quote）指回 brain_turn_messages。适合「X 的 Y 是 Z」这类可核对的事实/偏好/决策。",
        "parameters": {
            "type": "object",
            "properties": {
                "subject": {"type": "string", "description": "主语，如 服务器 / 用户 / 项目名（≤191 字符）。"},
                "predicate": {"type": "string", "description": "谓词，如 磁盘布局 / 时区 / 首选模型（≤96 字符）。"},
                "value": {"type": "string", "description": "当前值/内容（≤16000 字符）。"},
                "kind": {"type": "string", "description": "fact | preference | constraint | decision | procedure | intention | alias（默认 fact）。"},
                "basis": {"type": "string", "description": "direct_report | observed | derived_summary | inferred_suggestion（默认 direct_report）。"},
                "turn_id": {"type": "integer", "description": "证据所在轮次 brain_turns.id（可选）。"},
                "seq": {"type": "integer", "description": "证据在该轮转录里的 seq（可选）。"},
                "quote": {"type": "string", "description": "证据原文引用（≤2000 字符，可选）。"},
                "valid_from": {"type": "string", "description": "生效时间 YYYY-MM-DD HH:MM:SS（UTC，可选）。"},
                "valid_to": {"type": "string", "description": "失效时间（可选）。"},
                "confidence": {"type": "integer", "minimum": 0, "maximum": 100, "description": "置信度 0-100（默认 80）。"},
                "importance": {"type": "integer", "minimum": 1, "maximum": 10, "description": "重要度 1-10（默认 5）。"},
            },
            "required": ["subject", "predicate", "value"],
        },
    },
    {
        "name": "atbrains_claim_recall",
        "description": "检索结构化主张的当前值（history=true 返回全部历史版本，含证据 turn_id/seq/quote）。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "模糊匹配主语/谓词/值（可选）。"},
                "subject": {"type": "string", "description": "只查某个主语（可选）。"},
                "predicate": {"type": "string", "description": "只查某个谓词（可选）。"},
                "history": {"type": "boolean", "description": "true = 返回全部版本（含已封版/已撤回），默认 false。"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "条数，默认 10。"},
            },
        },
    },
    {
        "name": "atbrains_episode_set",
        "description": "给某一轮挂情节头（目标/决策/未完成项/阻碍/下一步），只更新传入的字段。用于「这轮在做什么、做到哪、下一步」。",
        "parameters": {
            "type": "object",
            "properties": {
                "turn_id": {"type": "integer", "description": "brain_turns.id。"},
                "goal": {"type": "string", "description": "这一轮的目标。"},
                "decisions": {"type": "array", "items": {"type": "string"}, "description": "本轮做出的决策。"},
                "open_items": {"type": "array", "items": {"type": "string"}, "description": "未完成项。"},
                "blockers": {"type": "array", "items": {"type": "string"}, "description": "阻碍。"},
                "next_step": {"type": "string", "description": "下一步。"},
                "status": {"type": "string", "description": "open | done | abandoned（默认 open）。"},
            },
            "required": ["turn_id"],
        },
    },
    {
        "name": "atbrains_episode_get",
        "description": "读某一轮的情节头（不传 turn_id 时取该会话最后一轮）。",
        "parameters": {
            "type": "object",
            "properties": {
                "turn_id": {"type": "integer"},
                "session_id": {"type": "string"},
            },
        },
    },
]


class AtbrainsMemoryProvider(MemoryProvider):
    """Redis（短期）+ MariaDB（长期）双存储记忆提供者。"""

    def __init__(self) -> None:
        self._store: Optional[AtbrainsStore] = None
        self._session_id: str = ""
        self._platform: str = ""
        self._hermes_home: str = ""
        self._primary: bool = True
        self._last_recall: Optional[RecallStatus] = None
        self._init_lock = threading.Lock()
        self._drainer_started = False
        self._drainer_lock = threading.Lock()

    # -------------------------------------------------- 核心生命周期

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    def is_available(self) -> bool:
        """依赖可导入 + 凭据可解析（无网络调用）。"""
        try:
            import pymysql  # noqa: F401
            import redis  # noqa: F401
        except Exception:  # noqa: BLE001
            return False
        try:
            cfg = resolve_config(hermes_home_path())
        except Exception:  # noqa: BLE001
            return False
        # 远程模式只需要服务端 URL/令牌：强制要求本机密码会让纯客户端永远加载不了 provider
        if _remote_configured(cfg):
            return True
        return bool(cfg.get("redis_password") and cfg.get("mariadb_password"))

    def unavailable_reason(self) -> str:
        try:
            import pymysql  # noqa: F401
            import redis  # noqa: F401
        except Exception:  # noqa: BLE001
            return ("atbrains 需要 python 包 redis 和 PyMySQL —— 运行 `hermes pm install` "
                    "让 PM 按插件声明安装（或 pip install redis pymysql）。")
        try:
            cfg = resolve_config(hermes_home_path())
        except Exception as exc:  # noqa: BLE001
            return f"atbrains 配置读取失败: {exc}"
        if _remote_configured(cfg):
            return ""
        missing = []
        if not cfg.get("redis_password"):
            missing.append("ATBRAINS_REDIS_PASSWORD")
        if not cfg.get("mariadb_password"):
            missing.append("ATBRAINS_MARIADB_PASSWORD")
        if missing:
            return ("atbrains 缺少凭据 " + ", ".join(missing) +
                    "（写入 ~/.hermes/.env，或运行 `hermes memory setup` 配置）。")
        return ""

    def initialize(self, session_id: str, **kwargs) -> None:
        with self._init_lock:
            self._session_id = str(session_id or "")
            self._hermes_home = str(kwargs.get("hermes_home") or hermes_home_path())
            self._platform = str(kwargs.get("platform") or "")
            self._primary = str(kwargs.get("agent_context") or "primary") in ("", "primary")
            try:
                cfg = resolve_config(self._hermes_home)
                store = AtbrainsStore(cfg)
                try:
                    store.ensure_schema()
                except Exception as exc:  # noqa: BLE001
                    # 数据库暂时不可达不阻塞启动；后续操作会重试建表。
                    logger.warning("atbrains: 建表延迟（数据库暂不可达）: %s", exc)
                self._store = store
            except Exception as exc:  # noqa: BLE001
                logger.warning("atbrains: 初始化失败 (%s)", exc)
                self._store = None
            try:
                self._start_vector_drainer()
            except Exception as exc:  # noqa: BLE001
                logger.debug("atbrains: 启动向量 drainer 失败: %s", exc)

    def _start_vector_drainer(self) -> None:
        """每进程最多一个后台 drainer：被写入唤醒，另有周期兜底扫描。"""
        store = self._store
        if store is None or not int(store.cfg.get("vector_enabled") or 0):
            return
        with self._drainer_lock:
            if self._drainer_started:
                return
            self._drainer_started = True

        def _loop() -> None:
            interval = max(15, int(store.cfg.get("vector_drain_interval") or 60))
            try:
                store.drain_vectors()   # 启动即扫一遍：清掉本进程启动前的欠账
            except Exception as exc:  # noqa: BLE001
                logger.debug("atbrains: drainer 首扫异常: %s", exc)
            while True:
                try:
                    store._vec_wake.wait(timeout=interval)
                    store._vec_wake.clear()
                    store.drain_vectors()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("atbrains: 向量 drainer 轮次异常: %s", exc)
                time.sleep(0.5)

        threading.Thread(target=_loop, name="brain-vector-drain", daemon=True).start()

    # -------------------------------------------------- 召回

    def system_prompt_block(self) -> str:
        return SYSTEM_PROMPT_BLOCK

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        store = self._store
        if store is None or not query:
            self._last_recall = None
            return ""
        try:
            text, count = store.prefetch_block(query, session_id=session_id or self._session_id)
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
        store = self._store
        if store is None or not query:
            return

        def _warm() -> None:
            try:
                store.prefetch_block(query, session_id=session_id or self._session_id)
            except Exception as exc:  # noqa: BLE001
                logger.debug("atbrains: 后台召回预热失败: %s", exc)

        try:
            spawn_context_thread(_warm, name="brain-prefetch-warm").start()
        except Exception:  # noqa: BLE001
            # 帮助器不可用时退化为普通线程（best-effort 预热）。
            threading.Thread(target=_warm, name="brain-prefetch-warm", daemon=True).start()

    def recall_status(self) -> Optional[RecallStatus]:
        return self._last_recall

    # -------------------------------------------------- 写入

    def sync_turn(self, user_content: str, assistant_content: str, *,
                  session_id: str = "", messages: Optional[list[dict[str, Any]]] = None,
                  turn_author: Optional[dict[str, Any]] = None) -> None:
        store = self._store
        if store is None or not self._primary:
            return
        author = ""
        if isinstance(turn_author, dict):
            author = str(turn_author.get("name") or turn_author.get("id") or "")[:64]
        try:
            store.record_turn(session_id or self._session_id, user_content or "",
                              assistant_content or "", platform=self._platform, author=author,
                              messages=messages)
        except Exception as exc:  # noqa: BLE001
            logger.warning("atbrains: sync_turn 写入失败: %s", exc)

    def on_memory_write(self, action: str, target: str, content: str,
                        metadata: Optional[dict[str, Any]] = None) -> None:
        store = self._store
        if store is None or not self._primary:
            return
        prev = ""
        if isinstance(metadata, dict):
            prev = str(metadata.get("previous_content") or "")
        try:
            if action == "add":
                if content:
                    store.remember(content, kind="builtin-memory", tags=str(target or ""),
                                   importance=6, source="memory_write",
                                   session_id=self._session_id)
            elif action == "replace":
                # 先写新内容再停用旧内容：反过来若写入失败，旧记忆已停用、新记忆没落库 → 净丢失。
                # 内容相同则 remember 走 dedupe 刷新，不能再按内容停用（会把刚刷新的行停掉）。
                if content:
                    store.remember(content, kind="builtin-memory", tags=str(target or ""),
                                   importance=6, source="memory_write",
                                   session_id=self._session_id)
                if prev and prev != content:
                    store.deactivate_by_content(prev)
            elif action == "remove":
                target_content = prev or content
                if target_content:
                    store.deactivate_by_content(target_content)
        except Exception as exc:  # noqa: BLE001
            logger.warning("atbrains: on_memory_write 镜像失败: %s", exc)

    def on_pre_compress(self, messages: list[dict[str, Any]]) -> str:
        store = self._store
        if store is None or not self._primary:
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
                        str(p.get("text") or "") for p in text
                        if isinstance(p, dict) and p.get("type") == "text")
                text = str(text or "")
                if not text.strip():
                    continue
                payload.append({"role": role, "content": text[:2000]})
            if payload:
                store.archive(self._session_id, "precompress",
                              json.dumps(payload, ensure_ascii=False),
                              platform=self._platform)
        except Exception as exc:  # noqa: BLE001
            logger.warning("atbrains: on_pre_compress 存档失败: %s", exc)
        return ""

    def on_session_end(self, messages: list[dict[str, Any]]) -> None:
        store = self._store
        if store is None or not self._primary:
            return
        try:
            store.touch_session(self._session_id, self._platform)
        except Exception:  # noqa: BLE001
            pass

    def on_session_switch(self, new_session_id: str, *, parent_session_id: str = "",
                          reset: bool = False, rewound: bool = False, **kwargs) -> None:
        self._session_id = str(new_session_id or self._session_id)

    def on_delegation(self, task: str, result: str, *, child_session_id: str = "",
                      **kwargs) -> None:
        store = self._store
        if store is None or not self._primary:
            return
        try:
            content = f"子代理任务: {task}\n结果: {result}"
            store.remember(content[:4000], kind="delegation", importance=4,
                           source="delegation", session_id=self._session_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("atbrains: on_delegation 记录失败: %s", exc)

    # -------------------------------------------------- 工具

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return TOOL_SCHEMAS

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs) -> str:
        try:
            if tool_name == "atbrains_remember":
                return self._tool_remember(args or {})
            if tool_name == "atbrains_recall":
                return self._tool_recall(args or {})
            if tool_name == "atbrains_forget":
                return self._tool_forget(args or {})
            if tool_name == "atbrains_recent":
                return self._tool_recent(args or {})
            if tool_name == "atbrains_stats":
                return self._tool_stats()
            if tool_name == "atbrains_turn_detail":
                return self._tool_turn_detail(args or {})
            if tool_name == "atbrains_claim_remember":
                return self._tool_claim_remember(args or {})
            if tool_name == "atbrains_claim_recall":
                return self._tool_claim_recall(args or {})
            if tool_name == "atbrains_episode_set":
                return self._tool_episode_set(args or {})
            if tool_name == "atbrains_episode_get":
                return self._tool_episode_get(args or {})
        except BrainError as exc:
            return json.dumps({"ok": False, "error": str(exc)[:300]}, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"ok": False, "error": str(exc)[:300]}, ensure_ascii=False)
        return json.dumps({"ok": False, "error": f"未知工具 {tool_name}"}, ensure_ascii=False)

    def _require_store(self) -> AtbrainsStore:
        if self._store is None:
            raise BrainError("atbrains 存储未初始化（检查连接与凭据）")
        return self._store

    def _tool_remember(self, args: dict[str, Any]) -> str:
        store = self._require_store()
        result = store.remember(
            str(args.get("content") or ""),
            kind=str(args.get("kind") or "note"),
            tags=str(args.get("tags") or ""),
            importance=args.get("importance") or 5,
            source="tool",
            session_id=self._session_id,
        )
        return json.dumps({"ok": True, "saved": result, "storage": "MariaDB(长期) + Redis(热点)"},
                          ensure_ascii=False)

    def _tool_recall(self, args: dict[str, Any]) -> str:
        store = self._require_store()
        query = str(args.get("query") or "")
        limit = max(1, min(20, int(args.get("limit") or 5)))
        kind = args.get("kind") or None
        agent = args.get("agent") or None
        include_turns = args.get("include_turns", True)
        mems = store.search_memories_hybrid(query, limit=limit,
                                            kind=str(kind) if kind else None,
                                            agent=str(agent) if agent else None)
        turns = store.search_turns(query, limit=min(limit, 5)) if include_turns else []
        try:
            claims = store.claim_recall(query=query, limit=min(limit, 5)) if query else []
        except Exception:  # noqa: BLE001
            claims = []
        for m in mems:
            for k, v in m.items():
                if isinstance(v, datetime):
                    m[k] = v.isoformat(sep=" ", timespec="seconds")
        for t in turns:
            for k, v in t.items():
                if isinstance(v, datetime):
                    t[k] = v.isoformat(sep=" ", timespec="seconds")
        return json.dumps({"ok": True, "memories": mems, "turns": turns, "claims": claims},
                          ensure_ascii=False)

    def _tool_forget(self, args: dict[str, Any]) -> str:
        store = self._require_store()
        mem_id = args.get("id")
        if mem_id is None:
            raise BrainError("需要 id")
        return json.dumps({"ok": True, "result": store.forget(int(mem_id))}, ensure_ascii=False)

    def _tool_recent(self, args: dict[str, Any]) -> str:
        store = self._require_store()
        n = max(1, min(50, int(args.get("n") or 10)))
        what = str(args.get("what") or "turns")
        if what == "memories":
            return json.dumps({"ok": True, "recent_memories": store.hot_recent(n)},
                              ensure_ascii=False)
        turns = store.recent_turns(n)
        for t in turns:
            t["user"] = str(t.get("user") or "")[:200]
            t["assistant"] = str(t.get("assistant") or "")[:200]
        return json.dumps({"ok": True, "recent_turns": turns}, ensure_ascii=False)

    def _tool_stats(self) -> str:
        store = self._require_store()
        return json.dumps({"ok": True, "stats": store.stats()}, ensure_ascii=False)

    def _tool_turn_detail(self, args: dict[str, Any]) -> str:
        store = self._require_store()
        turn_id = args.get("turn_id")
        session_id = str(args.get("session_id") or "")
        max_chars = args.get("max_chars") or 1500
        result = store.turn_detail(int(turn_id) if turn_id is not None else 0,
                                   session_id=session_id,
                                   max_chars=int(max_chars))
        if result.get("found"):
            result["episode"] = store.episode_get(int(result["turn"]["id"]))
        return json.dumps({"ok": True, **result}, ensure_ascii=False)

    def _tool_claim_remember(self, args: dict[str, Any]) -> str:
        store = self._require_store()
        result = store.claim_remember(
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
            confidence=args.get("confidence") if args.get("confidence") is not None else 80,
            importance=args.get("importance") if args.get("importance") is not None else 5,
        )
        return json.dumps({"ok": True, "claim": result}, ensure_ascii=False)

    def _tool_claim_recall(self, args: dict[str, Any]) -> str:
        store = self._require_store()
        rows = store.claim_recall(
            query=str(args.get("query") or ""),
            subject=str(args.get("subject") or ""),
            predicate=str(args.get("predicate") or ""),
            limit=int(args.get("limit") or 10),
            history=bool(args.get("history")),
        )
        return json.dumps({"ok": True, "claims": rows, "count": len(rows)}, ensure_ascii=False)

    def _tool_episode_set(self, args: dict[str, Any]) -> str:
        store = self._require_store()
        turn_id = args.get("turn_id")
        if turn_id is None:
            raise BrainError("需要 turn_id（atbrains_recent 可查最近轮次）")
        result = store.episode_set(
            int(turn_id),
            goal=args.get("goal"),
            decisions=args.get("decisions"),
            open_items=args.get("open_items"),
            blockers=args.get("blockers"),
            next_step=args.get("next_step"),
            status=args.get("status"),
            session_id=self._session_id,
        )
        return json.dumps({"ok": True, "episode": result}, ensure_ascii=False)

    def _tool_episode_get(self, args: dict[str, Any]) -> str:
        store = self._require_store()
        turn_id = args.get("turn_id")
        result = store.episode_get(int(turn_id) if turn_id is not None else 0,
                                   session_id=str(args.get("session_id") or ""))
        return json.dumps({"ok": True, **result}, ensure_ascii=False)

    # -------------------------------------------------- 关停

    def shutdown(self) -> None:
        store = self._store
        if store is None:
            return
        try:
            store.close()
        except Exception:  # noqa: BLE001
            pass

    # -------------------------------------------------- 配置

    def get_config_schema(self) -> list[dict[str, Any]]:
        return [
            {"key": "redis_password", "description": "Redis 密码（atbrains 短期存储）",
             "secret": True, "required": True, "env_var": "ATBRAINS_REDIS_PASSWORD"},
            {"key": "mariadb_password", "description": "MariaDB 密码（atbrains 长期存储）",
             "secret": True, "required": True, "env_var": "ATBRAINS_MARIADB_PASSWORD"},
            {"key": "redis_host", "description": "Redis 主机", "default": "127.0.0.1"},
            {"key": "redis_port", "description": "Redis 端口", "type": "integer", "default": 9079},
            {"key": "mariadb_host", "description": "MariaDB 主机", "default": "127.0.0.1"},
            {"key": "mariadb_port", "description": "MariaDB 端口", "type": "integer", "default": 57306},
            {"key": "mariadb_user", "description": "MariaDB 用户", "default": "agent"},
            # 与 DEFAULT_CONFIG / README / 实际库名保持一致（旧默认值会把 setup 写到错库）
            {"key": "mariadb_db", "description": "MariaDB 数据库", "default": "agent_memory"},
        ]

    def save_config(self, values: dict[str, Any], hermes_home: str) -> None:
        home = hermes_home_path(hermes_home)
        path = home / "atbrains.json"
        existing: dict[str, Any] = {}
        try:
            legacy = home / "brain.json"
            src = path if path.exists() else (legacy if legacy.exists() else path)
            data = json.loads(src.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict):
                existing = data
        except Exception:  # noqa: BLE001
            pass
        for k, v in (values or {}).items():
            if k in DEFAULT_NON_SECRET_KEYS and v is not None:
                existing[k] = v
        path.write_text(json.dumps(existing, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")


DEFAULT_NON_SECRET_KEYS = {
    "redis_host", "redis_port", "redis_db", "mariadb_host", "mariadb_port",
    "mariadb_user", "mariadb_db",
}


def register(ctx) -> None:
    """记忆插件发现系统入口。"""
    ctx.register_memory_provider(AtbrainsMemoryProvider())
    try:
        from pathlib import Path

        skill_path = Path(__file__).resolve().parent / "skills" / "atbrains-memory" / "SKILL.md"
        if skill_path.is_file():
            ctx.register_skill(
                "memory", skill_path,
                "How to use the atbrains memory store: Redis short-term + MariaDB long-term, and the brain_* tools.",
            )
    except Exception:  # noqa: BLE001
        logger.debug("atbrains: 技能注册失败", exc_info=True)
