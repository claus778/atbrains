#!/usr/bin/env python3
"""atbrains MCP 端点 —— 把共享长期记忆工具暴露给任意支持 MCP 的 agent。

背景
----
记忆引擎本体是 atbrains 自带的 Python 库（Hermes 通过 memory provider 加载它）。其它
agent（编辑器、DeepSeek Harness、Codex 等）不能直接 import 它。本模块把同一套 AtbrainsStore 能力包成
**MCP server**（JSON-RPC 2.0，与传输无关）：

    HTTP  : POST /mcp      Authorization: Bearer <令牌>   （console/server.py 交给本模块）
    stdio : mcp_stdio.py   --agent <身份>                 （客户端拉起子进程）

传输层只负责收消息；`AtbrainsMCP.handle()` 处理 initialize / tools / resources / prompts。
工具集合参考 mnemos-mcp 的形状：会话开头 atbrains_context，收尾 atbrains_handoff。

令牌决定 agent 身份（令牌表 machine 名）。所有 agent 共用同一个 MariaDB
长期库（默认 agent_memory），写入以 `agent` 列标注来源 agent —— 与
scope-recall 的 "shared store, each memory says which agent it came in through"
同一模型。Redis 只做热点缓存，不承载跨 agent 的权威数据。

设计约束
--------
* 只用标准库 + atbrains-memory 插件自带的 core.py/vendor（不新增依赖）。
* 不 import 插件 `__init__.py`（它依赖宿主框架的 agent.memory_provider）；
  工具 schema 在本文件维护一份，与插件工具同名同参。
* 无状态：不强制 MCP session；客户端带不带 Mcp-Session-Id 都接受。
"""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime
from typing import Any, Optional

# 协议版本：客户端报什么就回什么（在支持集合内），否则回最新的。
MCP_PROTOCOL_DEFAULT = "2025-06-18"
MCP_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "atbrains", "version": "2.1.0",
               "title": "atbrains 共享长期记忆"}

# --------------------------------------------------------------------------- 工具

TOOLS: list[dict[str, Any]] = [
    {
        "name": "atbrains_remember",
        "description": "永久保存一条长期记忆（事实/偏好/决策/待办）到 atbrains 共享库。"
                       "用户明确陈述稳定的偏好、身份、项目约定、决定时调用；不要保存一次性闲聊、"
                       "临时命令输出、密钥/密码。写入会标注来源 agent。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string",
                            "description": "记忆内容，简洁自洽，脱离上下文也能读懂。"},
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
        "description": "从 atbrains 共享长期库检索记忆（语义向量 + 关键词混合召回），"
                       "同时返回相关历史对话片段和结构化主张。"
                       "开始任务前、回答涉及用户历史/偏好/项目时先调用它。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索关键词或自然语言问题。"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20,
                          "description": "返回条数，默认 5。"},
                "kind": {"type": "string", "description": "只检索某一类别（可选）。"},
                "agent": {"type": "string",
                          "description": "只检索某个 agent 写入的记忆（传 agent 名，可选；默认跨 agent 全部共享记忆）。"},
                "include_turns": {"type": "boolean",
                                  "description": "是否附带相关历史对话，默认 true。"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "atbrains_forget",
        "description": "废止一条记忆（软删除，按 id）。id 从 atbrains_recall / atbrains_remember 返回中获得。",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "integer", "description": "记忆的 id。"}},
            "required": ["id"],
        },
    },
    {
        "name": "atbrains_recent",
        "description": "查看最近写入的记忆（Redis 热点镜像）或最近对话流。"
                       "想知道「刚刚/最近发生了什么」时调用。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "n": {"type": "integer", "minimum": 1, "maximum": 50,
                      "description": "条数，默认 10。"},
                "what": {"type": "string",
                         "description": "memories（最近记忆，默认）| turns（最近对话）"},
            },
        },
    },
    {
        "name": "atbrains_stats",
        "description": "atbrains 库统计：本 agent 身份、记忆/归档/会话数量、Redis 键数、存储占用。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "atbrains_turn_detail",
        "description": "取回某一轮对话的完整过程：工具调用参数、工具输出、推理（brain_turn_messages 转录层）。"
                       "不传 turn_id 时用 session_id 取该会话最后一轮。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "turn_id": {"type": "integer", "description": "brain_turns.id（推荐）。"},
                "session_id": {"type": "string",
                               "description": "会话 id（不给 turn_id 时取该会话最后一轮）。"},
                "max_chars": {"type": "integer", "minimum": 200, "maximum": 20000,
                              "description": "每条消息正文的字符上限，默认 1500。"},
            },
        },
    },
    {
        "name": "atbrains_claim_remember",
        "description": "记一条结构化主张（subject/predicate/value）：同一槽位再写只递增版本、旧版本自动封版。"
                       "适合「X 的 Y 是 Z」这类可核对的事实/偏好/决策。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "subject": {"type": "string", "description": "主语，如 服务器 / 用户 / 项目名（≤191 字符）。"},
                "predicate": {"type": "string", "description": "谓词，如 磁盘布局 / 时区 / 首选模型（≤96 字符）。"},
                "value": {"type": "string", "description": "当前值/内容（≤16000 字符）。"},
                "kind": {"type": "string",
                         "description": "fact | preference | constraint | decision | procedure | intention | alias（默认 fact）。"},
                "basis": {"type": "string",
                          "description": "direct_report | observed | derived_summary | inferred_suggestion（默认 direct_report）。"},
                "turn_id": {"type": "integer", "description": "证据所在轮次 brain_turns.id（可选）。"},
                "seq": {"type": "integer", "description": "证据在该轮转录里的 seq（可选）。"},
                "quote": {"type": "string", "description": "证据原文引用（≤2000 字符，可选）。"},
                "valid_from": {"type": "string", "description": "生效时间 YYYY-MM-DD HH:MM:SS（UTC，可选）。"},
                "valid_to": {"type": "string", "description": "失效时间（可选）。"},
                "confidence": {"type": "integer", "minimum": 0, "maximum": 100,
                               "description": "置信度 0-100（默认 80）。"},
                "importance": {"type": "integer", "minimum": 1, "maximum": 10,
                               "description": "重要度 1-10（默认 5）。"},
            },
            "required": ["subject", "predicate", "value"],
        },
    },
    {
        "name": "atbrains_claim_recall",
        "description": "检索结构化主张的当前值（history=true 返回全部历史版本，含证据 turn_id/seq/quote）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "模糊匹配主语/谓词/值（可选）。"},
                "subject": {"type": "string", "description": "只查某个主语（可选）。"},
                "predicate": {"type": "string", "description": "只查某个谓词（可选）。"},
                "history": {"type": "boolean",
                            "description": "true = 返回全部版本（含已封版/已撤回），默认 false。"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50,
                          "description": "条数，默认 10。"},
            },
        },
    },
    {
        "name": "atbrains_episode_set",
        "description": "给某一轮挂情节头（目标/决策/未完成项/阻碍/下一步），只更新传入的字段。"
                       "用于「这轮在做什么、做到哪、下一步」。需要 brain_turns.id（先用 atbrains_recent 查）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "turn_id": {"type": "integer", "description": "brain_turns.id。"},
                "goal": {"type": "string", "description": "这一轮的目标。"},
                "decisions": {"type": "array", "items": {"type": "string"},
                              "description": "本轮做出的决策。"},
                "open_items": {"type": "array", "items": {"type": "string"},
                               "description": "未完成项。"},
                "blockers": {"type": "array", "items": {"type": "string"},
                             "description": "阻碍。"},
                "next_step": {"type": "string", "description": "下一步。"},
                "status": {"type": "string", "description": "open | done | abandoned（默认 open）"},
            },
            "required": ["turn_id"],
        },
    },
    {
        "name": "atbrains_episode_get",
        "description": "读某一轮的情节头（不传 turn_id 时取该会话最后一轮）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "turn_id": {"type": "integer"},
                "session_id": {"type": "string"},
            },
        },
    },
    {
        "name": "atbrains_context",
        "description": "醒来包：一次取回「我是谁 + 我记着什么 + 上次做到哪」。"
                       "会话开始时先调用它（比 recall 更全：重点记忆 + 结构化主张 + 未完成情节 + 最近对话）。"
                       "带 focus 时按当前任务做语义检索，否则返回高重要度记忆。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "focus": {"type": "string",
                          "description": "当前任务/问题关键词（可选）；给了就按它召回，不给返回高重要度记忆。"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20,
                          "description": "记忆条数，默认 8。"},
                "include_turns": {"type": "boolean",
                                  "description": "是否附最近对话一行摘要，默认 true。"},
                "include_episodes": {"type": "boolean",
                                     "description": "是否附未完成情节（open），默认 true。"},
                "max_chars": {"type": "integer", "minimum": 80, "maximum": 2000,
                              "description": "每条记忆正文截断长度，默认 400。"},
            },
        },
    },
    {
        "name": "atbrains_handoff",
        "description": "给下一次会话留一段话（handoff 便签）：写一条 kind=note、tags=handoff 的记忆，"
                       "可选同时把 next_step / open_items 挂到某一轮的情节头上。"
                       "会话收尾、任务只做了一半、要交接给别人时调用。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "note": {"type": "string",
                         "description": "留给下一次的话：做到哪、坑在哪、下一步做什么。"},
                "next_step": {"type": "string", "description": "下一步（写进情节头，可选）。"},
                "open_items": {"type": "array", "items": {"type": "string"},
                               "description": "未完成项（写进情节头，可选）。"},
                "turn_id": {"type": "integer",
                            "description": "要挂情节头的 brain_turns.id（可选；不给就只写便签）。"},
                "importance": {"type": "integer", "minimum": 1, "maximum": 10,
                               "description": "重要度，默认 7（handoff 比普通笔记重要）。"},
            },
            "required": ["note"],
        },
    },
]

# 工具声明（title/只读/破坏性/幂等）—— MCP 2025-03-26 起的 annotations 字段，
# 客户端用它决定要不要二次确认、能不能并发。缺省按「写操作」保守处理。
_ANNOTATION_FLAGS: dict[str, tuple[str, bool, bool, bool]] = {
    "atbrains_remember": ("保存记忆", False, False, False),
    "atbrains_recall": ("检索记忆", True, False, True),
    "atbrains_forget": ("停用记忆", False, True, True),
    "atbrains_recent": ("最近动态", True, False, True),
    "atbrains_stats": ("库统计", True, False, True),
    "atbrains_turn_detail": ("单轮详情", True, False, True),
    "atbrains_claim_remember": ("记录主张", False, False, False),
    "atbrains_claim_recall": ("检索主张", True, False, True),
    "atbrains_episode_set": ("写情节头", False, False, True),
    "atbrains_episode_get": ("读情节头", True, False, True),
    "atbrains_context": ("醒来包", True, False, True),
    "atbrains_handoff": ("交接便签", False, False, False),
}
for _tool in TOOLS:
    _title, _ro, _destructive, _idem = _ANNOTATION_FLAGS.get(
        _tool["name"], (_tool["name"], False, False, False))
    _tool["annotations"] = {"title": _title, "readOnlyHint": _ro,
                            "destructiveHint": _destructive, "idempotentHint": _idem,
                            "openWorldHint": False}

TOOL_ALIASES = {t["name"].replace("atbrains_", "brain_"): t["name"] for t in TOOLS}

INSTRUCTIONS = """\
你接入了 atbrains 共享长期记忆（MariaDB + Redis，自托管）。多个 agent（本机 CLI、编辑器、其它 harness）
共用同一个长期库；每条记忆都会标注来源 agent（本会话来源 = {agent}）。

使用规则（按需调用，不要每轮都调）：
0. 会话开头先调一次 atbrains_context（可带 focus=用户第一句话的关键词）：它一次给出重点记忆、
   结构化主张、未完成情节和最近对话，比反复 recall 省事。
1. 开始一个任务、或回答涉及用户历史/偏好/项目约定之前，先调用 atbrains_recall，
   query 用当前问题的关键词（不是整段话）。查到的事实可以直接使用，不必再问用户。
2. 用户明确陈述稳定的偏好、身份、约定、决定、待办时，调用 atbrains_remember 保存；
   内容写成脱离上下文也能读懂的独立句子。不要保存闲聊、临时输出、密钥/密码。
3. 「X 的 Y 是 Z」这类可核对的值，用 atbrains_claim_remember（同槽位自动版本化）；
   查历史版本用 atbrains_claim_recall(history=true)。
4. 需要知道最近发生了什么 → atbrains_recent；复盘某一轮工具过程 → atbrains_turn_detail。
5. 任务做到一半要结束、或要交接给别人时，调 atbrains_handoff 留一段话给下一次会话。
6. 不要编造记忆里没有的内容；检索为空就如实说没有记录。
"""


# --------------------------------------------------------------------------- 资源 / 提示词

# 资源 = 只读快照，客户端（Claude Desktop / Cursor / Codex）可以在会话开始时
# 直接把 @atbrains://context 塞进上下文，不用先调工具。
RESOURCES: list[dict[str, Any]] = [
    {"uri": "atbrains://context",
     "name": "醒来包",
     "title": "atbrains 醒来包（重点记忆 + 主张 + 未完成情节 + 最近对话）",
     "description": "会话开始时读取：我是谁、我记着什么、上次做到哪。markdown 文本。",
     "mimeType": "text/markdown"},
    {"uri": "atbrains://stats",
     "name": "库统计",
     "title": "atbrains 库统计",
     "description": "记忆/对话/主张/图谱计数、向量覆盖与存储占用。JSON。",
     "mimeType": "application/json"},
    {"uri": "atbrains://recent/memories",
     "name": "最近记忆",
     "title": "最近写入的记忆（Redis 热点镜像）",
     "description": "最近写入的 20 条记忆。JSON。",
     "mimeType": "application/json"},
    {"uri": "atbrains://recent/turns",
     "name": "最近对话",
     "title": "最近对话归档（一行摘要，20 条）",
     "description": "最近 20 轮对话的用户/助手摘要。JSON。",
     "mimeType": "application/json"},
]

PROMPTS: list[dict[str, Any]] = [
    {"name": "wake-up",
     "title": "带记忆开始",
     "description": "把 atbrains 醒来包作为会话的第一条上下文注入。",
     "arguments": [{"name": "focus", "description": "当前任务/问题关键词（可选）", "required": False}]},
    {"name": "handoff",
     "title": "交接给下一次会话",
     "description": "让模型把当前进度写成一段 handoff 便签并存进 atbrains。",
     "arguments": [{"name": "next_step", "description": "下一步（可选）", "required": False}]},
]


# --------------------------------------------------------------------------- 工具层

_CONTEXT_MEMORY_CHARS = 400


def _clip(text: Any, limit: int) -> str:
    s = str(text if text is not None else "")
    if limit <= 0 or len(s) <= limit:
        return s
    return s[: max(limit - 1, 0)] + "…"


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if str(v).strip()]
    return [str(value)] if str(value).strip() else []


def _open_episodes(store: Any, limit: int = 5) -> list[dict[str, Any]]:
    """未完成情节（status=open），最近的在前。表在 core 里，这里只读。"""
    try:
        rows = store._exec(
            "SELECT turn_id, session_id, goal, decisions, open_items, blockers, "
            "next_step, status, updated_at FROM brain_episodes "
            "WHERE status = 'open' ORDER BY updated_at DESC, turn_id DESC LIMIT %s",
            (int(max(1, limit)),), fetch=True)
    except Exception:  # noqa: BLE001 —— 情节头是附加信息，读不到不影响主包
        return []
    return _clean(list(rows))


def _top_memories(store: Any, limit: int) -> list[dict[str, Any]]:
    """没有 focus 时的醒来包内容：高重要度 + 最近更新。"""
    try:
        rows = store._exec(
            "SELECT id, kind, content, tags, importance, agent, updated_at "
            "FROM brain_memories WHERE active = 1 AND superseded_by IS NULL "
            "ORDER BY importance DESC, updated_at DESC, id DESC LIMIT %s",
            (int(max(1, limit)),), fetch=True)
    except Exception:  # noqa: BLE001
        return []
    return _clean(list(rows))


def _context_packet(store: Any, machine: str, *, focus: str = "", limit: int = 8,
                    include_turns: bool = True, include_episodes: bool = True,
                    max_chars: int = _CONTEXT_MEMORY_CHARS) -> dict[str, Any]:
    """醒来包：一次调用取回「我是谁 + 记着什么 + 上次做到哪」。

    记忆正文截断到 max_chars：醒来包是索引，不是全文库 —— 需要原文再用
    atbrains_turn_detail / atbrains_recall 取。
    """
    store.ensure_schema()
    limit = max(1, min(20, int(limit)))
    max_chars = max(80, min(2000, int(max_chars)))
    if focus:
        memories = _recall_memories(store, focus, limit, None)
    else:
        memories = _top_memories(store, limit)
    for m in memories:
        m["content"] = _clip(m.get("content"), max_chars)
        m["truncated"] = len(str(m.get("content") or "")) >= max_chars
    try:
        claims = store.claim_recall(limit=5)
    except Exception:  # noqa: BLE001
        claims = []
    packet: dict[str, Any] = {
        "agent": machine,
        "focus": focus,
        "memories": memories,
        "claims": claims,
        "open_episodes": _open_episodes(store, 5) if include_episodes else [],
        "recent_turns": [],
        "counts": {},
    }
    if include_turns:
        for t in store.recent_turns(5):
            packet["recent_turns"].append({
                "id": t.get("id"),
                "ts": _iso(t.get("ts")),
                "machine": t.get("machine") or "",
                "user": _clip(t.get("user_content") or t.get("user"), 120),
                "assistant": _clip(t.get("assistant_content") or t.get("assistant"), 160),
            })
    try:
        mysql = dict(store.stats().get("mysql") or {})
        packet["counts"] = {k: mysql.get(k) for k in
                            ("memories_active", "turns", "claims_active", "episodes", "sessions")
                            if k in mysql}
    except Exception:  # noqa: BLE001
        pass
    return packet


def _context_markdown(packet: dict[str, Any]) -> str:
    """醒来包的 markdown 形式：资源和 wake-up 提示词都用它。"""
    lines = [f"# atbrains 醒来包（agent={packet.get('agent') or '-'}）", ""]
    counts = packet.get("counts") or {}
    if counts:
        lines.append("库：" + " · ".join(f"{k}={v}" for k, v in counts.items()))
        lines.append("")
    if packet.get("focus"):
        lines += [f"焦点：{packet['focus']}", ""]
    mems = packet.get("memories") or []
    lines.append(f"## 记忆（{len(mems)}）")
    for m in mems:
        lines.append(f"- [{m.get('kind')}/{m.get('importance')}] {m.get('content')}"
                     f"  (id={m.get('id')}{'，截断' if m.get('truncated') else ''})")
    if not mems:
        lines.append("- （无）")
    claims = packet.get("claims") or []
    if claims:
        lines += ["", f"## 结构化主张（{len(claims)}）"]
        for c in claims:
            lines.append(f"- {c.get('subject')} · {c.get('predicate')} = "
                         f"{_clip(c.get('value'), 200)}")
    eps = packet.get("open_episodes") or []
    if eps:
        lines += ["", f"## 未完成情节（{len(eps)}）"]
        for e in eps:
            parts = [f"turn#{e.get('turn_id')}"]
            if e.get("goal"):
                parts.append(f"目标={_clip(e.get('goal'), 120)}")
            if e.get("next_step"):
                parts.append(f"下一步={_clip(e.get('next_step'), 120)}")
            lines.append("- " + " · ".join(parts))
    turns = packet.get("recent_turns") or []
    if turns:
        lines += ["", f"## 最近对话（{len(turns)}）"]
        for t in turns:
            lines.append(f"- [{t.get('ts')}] 用户：{t.get('user')} ／ 助手：{t.get('assistant')}")
    lines += ["", "_用 atbrains_recall / atbrains_turn_detail 取原文；"
                  "用 atbrains_remember / atbrains_handoff 写回。_"]
    return "\n".join(lines)

def _iso(v: Any) -> Any:
    if isinstance(v, (datetime, date)):
        return v.isoformat(sep=" ", timespec="seconds") if isinstance(v, datetime) else v.isoformat()
    return v


def _clean(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for row in rows:
        for k, v in list(row.items()):
            if isinstance(v, (datetime, date)):
                row[k] = _iso(v)
    return rows


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _exact_memories(store: Any, query: str, limit: int,
                    kind: Optional[str], agent: Optional[str] = None) -> list[dict[str, Any]]:
    """整句/去空白后精确包含的直查。

    keyword 检索是 OR 语义 + importance 排序：查询里出现 "omp" 这类高频词时，
    刚写入的低重要度记忆会排在几十条高重要度旧条目之后，LIMIT 直接截掉。
    精确子串是强信号，单独直查一次，不参与 importance 竞争。
    """
    q = " ".join(str(query or "").split())
    if len(q) < 4:
        return []
    cols = ("id, kind, memory_type, content, tags, importance, source, session_id, "
            "machine, agent, created_at, updated_at")
    sql = (f"SELECT {cols} FROM brain_memories WHERE active = 1 AND superseded_by IS NULL "
           "AND (content LIKE %s OR REPLACE(REPLACE(content, ' ', ''), '\\n', '') LIKE %s)")
    params: list[Any] = [f"%{_like_escape(q)}%",
                         f"%{_like_escape(''.join(q.split()))}%"]
    if kind:
        sql += " AND kind = %s"
        params.append(kind)
    if agent:
        sql += " AND agent = %s"
        params.append(str(agent).strip().lower()[:32])
    sql += " ORDER BY importance DESC, updated_at DESC, id DESC LIMIT %s"
    params.append(int(max(limit, 1)))
    try:
        return list(store._exec(sql, tuple(params), fetch=True))
    except Exception:  # noqa: BLE001 —— 直查失败不影响主召回
        return []


def _recall_memories(store: Any, query: str, limit: int,
                     kind: Optional[str], agent: Optional[str] = None) -> list[dict[str, Any]]:
    """hybrid 召回 + 整句精确命中提升。

    为什么需要提升：RRF(k=60) 把「关键词第 1 名」和「向量第 1 名」压成同分，
    同分再按 importance 排序 —— 刚写入、importance 不高的记忆会被旧的高重要度
    条目挤下去，表现为「刚保存就搜不到」。这里把内容包含整条查询的记忆提到最前，
    只影响 MCP 通道，不改本机侧的召回排序。
    """
    pool_limit = max(limit * 2, 8)
    pool = store.search_memories_hybrid(query, limit=pool_limit, kind=kind, agent=agent)
    exact = _exact_memories(store, query, limit, kind, agent)
    if not exact:
        return pool[:limit]
    seen = {int(m["id"]) for m in exact if m.get("id") is not None}
    merged = exact + [m for m in pool if int(m.get("id") or 0) not in seen]
    return merged[:limit]


class AtbrainsMCP:
    """MCP 的传输无关实现：console/server.py 把 HTTP 请求交给它。"""

    def __init__(self, service: Any) -> None:
        self.service = service

    # ---------------------------------------------------------------- 入口

    def handle(self, machine: str, payload: Any) -> tuple[int, Any, dict[str, str]]:
        """处理一条(或一批) JSON-RPC 消息。

        返回 (HTTP 状态码, 响应对象/None, 额外响应头)。None 表示只回 202 空体
        （notification 不需要响应）。
        """
        headers = {"Mcp-Session-Id": uuid.uuid4().hex}
        if isinstance(payload, list):
            out = []
            for item in payload:
                resp = self._one(machine, item)
                if resp is not None:
                    out.append(resp)
            if not out:
                return 202, None, headers
            return 200, out, headers
        resp = self._one(machine, payload)
        if resp is None:
            return 202, None, headers
        return 200, resp, headers

    def _one(self, machine: str, msg: Any) -> Optional[dict[str, Any]]:
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return _error(None, -32600, "invalid request: expected JSON-RPC 2.0 object")
        method = str(msg.get("method") or "")
        msg_id = msg.get("id", None)
        is_notification = "id" not in msg
        raw_params = msg.get("params")
        params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}

        if method == "initialize":
            asked = str(params.get("protocolVersion") or "")
            version = asked if asked in MCP_PROTOCOLS else MCP_PROTOCOL_DEFAULT
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": version,
                    "capabilities": {
                        "tools": {"listChanged": False},
                        "resources": {"listChanged": False, "subscribe": False},
                        "prompts": {"listChanged": False},
                        "logging": {},
                    },
                    "serverInfo": dict(SERVER_INFO),
                    "instructions": INSTRUCTIONS.format(agent=machine),
                },
            }
        if method in ("notifications/initialized", "initialized", "notifications/cancelled"):
            return None
        if method == "ping":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
        if method == "logging/setLevel":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}
        if method == "resources/list":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"resources": RESOURCES}}
        if method == "resources/read":
            uri = str(params.get("uri") or "")
            try:
                contents = self.read_resource(machine, uri)
            except Exception as exc:  # noqa: BLE001
                return _error(msg_id, -32603, f"resource read failed: {type(exc).__name__}: {exc}"[:300])
            if contents is None:
                return _error(msg_id, -32602, f"unknown resource: {uri}")
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"contents": [contents]}}
        if method == "prompts/list":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"prompts": PROMPTS}}
        if method == "prompts/get":
            raw_prompt_args = params.get("arguments")
            prompt_args: dict[str, Any] = raw_prompt_args if isinstance(raw_prompt_args, dict) else {}
            got = self.get_prompt(machine, str(params.get("name") or ""), prompt_args)
            if got is None:
                return _error(msg_id, -32602, f"unknown prompt: {params.get('name')}")
            return {"jsonrpc": "2.0", "id": msg_id, "result": got}
        if method == "tools/call":
            name = TOOL_ALIASES.get(str(params.get("name") or ""), str(params.get("name") or ""))
            raw_args = params.get("arguments")
            args: dict[str, Any] = raw_args if isinstance(raw_args, dict) else {}
            if not any(t["name"] == name for t in TOOLS):
                return _error(msg_id, -32602, f"unknown tool: {name}")
            try:
                data = self.call(machine, name, args)
                text = json.dumps(data, ensure_ascii=False, default=str)
                is_error = isinstance(data, dict) and data.get("ok") is False
            except Exception as exc:  # noqa: BLE001 —— 工具故障回给模型，不炸传输层
                text = json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"[:500]},
                                  ensure_ascii=False)
                is_error = True
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"content": [{"type": "text", "text": text}], "isError": is_error},
            }
        if is_notification:
            return None
        return _error(msg_id, -32601, f"method not found: {method}")

    # ---------------------------------------------------------------- 资源 / 提示词

    def read_resource(self, machine: str, uri: str) -> Optional[dict[str, Any]]:
        """resources/read：只读快照。uri 不认识返回 None（由调用方回 -32602）。"""
        store = self.service.store_for("", agent=machine)
        if uri == "atbrains://context":
            packet = _context_packet(store, machine)
            return {"uri": uri, "mimeType": "text/markdown", "text": _context_markdown(packet)}
        if uri == "atbrains://stats":
            data = self.call(machine, "atbrains_stats", {})
            return {"uri": uri, "mimeType": "application/json",
                    "text": json.dumps(data, ensure_ascii=False, default=str)}
        if uri == "atbrains://recent/memories":
            data = self.call(machine, "atbrains_recent", {"n": 20, "what": "memories"})
            return {"uri": uri, "mimeType": "application/json",
                    "text": json.dumps(data, ensure_ascii=False, default=str)}
        if uri == "atbrains://recent/turns":
            data = self.call(machine, "atbrains_recent", {"n": 20, "what": "turns"})
            return {"uri": uri, "mimeType": "application/json",
                    "text": json.dumps(data, ensure_ascii=False, default=str)}
        return None

    def get_prompt(self, machine: str, name: str, args: dict[str, Any]) -> Optional[dict[str, Any]]:
        """prompts/get：醒来包与交接提示词。"""
        store = self.service.store_for("", agent=machine)
        if name == "wake-up":
            focus = str(args.get("focus") or "")
            packet = _context_packet(store, machine, focus=focus)
            return {"description": "atbrains 醒来包：把这段放在会话最前面。",
                    "messages": [{"role": "user",
                                  "content": {"type": "text", "text": _context_markdown(packet)}}]}
        if name == "handoff":
            next_step = str(args.get("next_step") or "")
            text = ("请把当前这轮工作整理成一段 handoff 便签，然后调用 "
                    "atbrains_handoff(note=..., next_step=..., open_items=[...]) 存进 atbrains。"
                    "便签写清：做到哪、坑在哪、下一步做什么。")
            if next_step:
                text += f"\n下一步：{next_step}"
            return {"description": "交接给下一次会话",
                    "messages": [{"role": "user", "content": {"type": "text", "text": text}}]}
        return None

    # ---------------------------------------------------------------- 工具实现

    def call(self, machine: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
        # MCP 通道：machine 参数实际是令牌绑定的 agent 身份；主机沿用服务端配置。
        store = self.service.store_for("", agent=machine)

        if name == "atbrains_remember":
            return {"ok": True,
                    "saved": store.remember(
                        str(args.get("content") or ""),
                        kind=str(args.get("kind") or "note"),
                        tags=str(args.get("tags") or ""),
                        importance=_as_int(args.get("importance"), 5),
                        source="mcp",
                        session_id=""),
                    "storage": "MariaDB(长期) + Redis(热点)"}

        if name == "atbrains_recall":
            query = str(args.get("query") or "")
            limit = max(1, min(20, _as_int(args.get("limit"), 5)))
            kind = args.get("kind") or None
            want_agent = args.get("agent") or None
            mems = _recall_memories(store, query, limit, str(kind) if kind else None,
                                    str(want_agent) if want_agent else None)
            turns = store.search_turns(query, limit=min(limit, 5)) \
                if _as_bool(args.get("include_turns"), True) else []
            try:
                claims = store.claim_recall(query=query, limit=min(limit, 5)) if query else []
            except Exception:  # noqa: BLE001
                claims = []
            return {"ok": True, "memories": _clean(mems), "turns": _clean(turns),
                    "claims": claims, "agent": machine}

        if name == "atbrains_forget":
            mem_id = args.get("id")
            if mem_id is None:
                return {"ok": False, "error": "需要 id"}
            return {"ok": True, "result": store.forget(_as_int(mem_id))}

        if name == "atbrains_recent":
            n = max(1, min(50, _as_int(args.get("n"), 10)))
            what = str(args.get("what") or "memories")
            if what == "turns":
                turns = store.recent_turns(n)
                for t in turns:
                    t["user"] = str(t.get("user") or "")[:200]
                    t["assistant"] = str(t.get("assistant") or "")[:200]
                return {"ok": True, "recent_turns": _clean(turns)}
            return {"ok": True, "recent_memories": _clean(store.hot_recent(n))}

        if name == "atbrains_stats":
            stats = store.stats()
            stats["agent"] = machine
            stats["database"] = str(store.cfg.get("mariadb_db") or "")
            stats["shared"] = True
            return {"ok": True, "stats": stats}

        if name == "atbrains_turn_detail":
            turn_id = args.get("turn_id")
            result = store.turn_detail(
                _as_int(turn_id) if turn_id is not None else 0,
                session_id=str(args.get("session_id") or ""),
                max_chars=max(200, min(20000, _as_int(args.get("max_chars"), 1500))))
            if result.get("found"):
                result["episode"] = store.episode_get(_as_int(result["turn"]["id"]))
            return {"ok": True, **result}

        if name == "atbrains_claim_remember":
            return {"ok": True, "claim": store.claim_remember(
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
                confidence=_as_int(args.get("confidence"), 80),
                importance=_as_int(args.get("importance"), 5),
            )}

        if name == "atbrains_claim_recall":
            rows = store.claim_recall(
                query=str(args.get("query") or ""),
                subject=str(args.get("subject") or ""),
                predicate=str(args.get("predicate") or ""),
                limit=max(1, min(50, _as_int(args.get("limit"), 10))),
                history=_as_bool(args.get("history"), False),
            )
            return {"ok": True, "claims": rows, "count": len(rows)}

        if name == "atbrains_episode_set":
            turn_id = args.get("turn_id")
            if turn_id is None:
                return {"ok": False, "error": "需要 turn_id（atbrains_recent 可查最近轮次）"}
            return {"ok": True, "episode": store.episode_set(
                _as_int(turn_id),
                goal=args.get("goal"),
                decisions=args.get("decisions"),
                open_items=args.get("open_items"),
                blockers=args.get("blockers"),
                next_step=args.get("next_step"),
                status=args.get("status"),
                session_id="",
            )}

        if name == "atbrains_episode_get":
            turn_id = args.get("turn_id")
            return {"ok": True, **store.episode_get(
                _as_int(turn_id) if turn_id is not None else 0,
                session_id=str(args.get("session_id") or ""))}

        if name == "atbrains_context":
            return {"ok": True, **_context_packet(
                store, machine,
                focus=str(args.get("focus") or ""),
                limit=_as_int(args.get("limit"), 8),
                include_turns=_as_bool(args.get("include_turns"), True),
                include_episodes=_as_bool(args.get("include_episodes"), True),
                max_chars=_as_int(args.get("max_chars"), _CONTEXT_MEMORY_CHARS))}

        if name == "atbrains_handoff":
            note = str(args.get("note") or "").strip()
            if not note:
                return {"ok": False, "error": "需要 note（留给下一次的那段话）"}
            saved = store.remember(
                note, kind="note", tags="handoff",
                importance=max(1, min(10, _as_int(args.get("importance"), 7))),
                source="mcp", session_id="")
            out: dict[str, Any] = {"ok": True, "saved": saved,
                                   "hint": "下一次会话开头用 atbrains_context 读回这段便签"}
            turn_id = args.get("turn_id")
            if turn_id is not None:
                out["episode"] = store.episode_set(
                    _as_int(turn_id),
                    next_step=args.get("next_step") or None,
                    open_items=_as_list(args.get("open_items")) or None,
                    session_id="")
            return out

        return {"ok": False, "error": f"未知工具 {name}"}


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


# 兼容旧名（外部脚本/旧文档仍写 BrainMCP）
BrainMCP = AtbrainsMCP
