"""通用记忆工具注册表 —— 唯一真源（MCP HTTP / MCP stdio / HTTP /v1 / 各 Agent 适配器共用）。

本模块不依赖任何底层存储驱动或第三方框架，只定义：
  * 12 个通用记忆工具的 JSON Schema 与 annotations；
  * 醒来包（context packet + markdown）拼装；
  * 统一的工具分发执行器 `execute_tool(store, agent_label, name, args)`。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional

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
        "description": "从 atbrains 共享长期库检索记忆（语义向量 + 关键词 + 图谱混合召回），"
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
        "description": "atbrains 库统计：当前 agent 身份、记忆/归档/会话数量、Redis 键数、存储占用。",
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
    _tool["annotations"] = {
        "title": _title,
        "readOnlyHint": _ro,
        "destructiveHint": _destructive,
        "idempotentHint": _idem,
        "openWorldHint": False,
    }

TOOL_ALIASES = {t["name"].replace("atbrains_", "brain_"): t["name"] for t in TOOLS}

INSTRUCTIONS = """\
你接入了 atbrains 共享长期记忆（MariaDB + Redis + Qdrant，通用多 Agent 记忆服务）。多个 agent
（CLI、桌面、编辑器、机器人、各 harness）共用同一个长期库；每条记忆都会标注来源 agent（本会话来源 = {agent}）。

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

_CONTEXT_MEMORY_CHARS = 400


def openai_tool_schemas() -> list[dict[str, Any]]:
    """导出通用 function-calling 格式（OpenAI / Hermes / LiteLLM 兼容）。"""
    return [
        {
            "name": t["name"],
            "description": t["description"],
            "parameters": t["inputSchema"],
        }
        for t in TOOLS
    ]


def clip_text(text: Any, limit: int) -> str:
    s = str(text if text is not None else "")
    if limit <= 0 or len(s) <= limit:
        return s
    return s[: max(limit - 1, 0)] + "…"


def as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if str(v).strip()]
    return [str(value)] if str(value).strip() else []


def iso_val(v: Any) -> Any:
    if isinstance(v, (datetime, date)):
        return v.isoformat(sep=" ", timespec="seconds") if isinstance(v, datetime) else v.isoformat()
    return v


def clean_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        item = dict(row)
        for k, v in list(item.items()):
            if isinstance(v, (datetime, date)):
                item[k] = iso_val(v)
        out.append(item)
    return out


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def context_packet(store: Any, agent_label: str, *, focus: str = "", limit: int = 8,
                   include_turns: bool = True, include_episodes: bool = True,
                   max_chars: int = _CONTEXT_MEMORY_CHARS) -> dict[str, Any]:
    """醒来包：一次调用取回「我是谁 + 记着什么 + 上次做到哪」。"""
    limit = max(1, min(20, int(limit)))
    max_chars = max(80, min(2000, int(max_chars)))
    if focus:
        memories = clean_rows(store.recall_memories(focus, limit=limit))
    else:
        memories = clean_rows(store.top_memories(limit=limit))
    for m in memories:
        raw = str(m.get("content") or "")
        m["content"] = clip_text(raw, max_chars)
        m["truncated"] = len(raw) > max_chars
    try:
        claims = store.claim_recall(limit=5)
    except Exception:  # noqa: BLE001
        claims = []
    episodes = clean_rows(store.open_episodes(5)) if include_episodes else []
    packet: dict[str, Any] = {
        "agent": agent_label,
        "focus": focus,
        "memories": memories,
        "claims": claims,
        "open_episodes": episodes,
        "recent_turns": [],
    }
    if include_turns:
        for t in store.recent_turns(5):
            packet["recent_turns"].append({
                "id": t.get("id"),
                "ts": iso_val(t.get("ts")),
                "machine": t.get("machine") or "",
                "user": clip_text(t.get("user_content") or t.get("user"), 120),
                "assistant": clip_text(t.get("assistant_content") or t.get("assistant"), 160),
            })
    return packet


def context_markdown(packet: dict[str, Any]) -> str:
    """醒来包的 markdown 形式。"""
    lines = [f"# atbrains 醒来包（agent={packet.get('agent') or '-'}）", ""]
    if packet.get("focus"):
        lines += [f"焦点：{packet['focus']}", ""]
    mems = packet.get("memories") or []
    lines.append(f"## 记忆（{len(mems)}）")
    for m in mems:
        lines.append(
            f"- [{m.get('kind')}/{m.get('importance')}] {m.get('content')}"
            f"  (id={m.get('id')}{'，截断' if m.get('truncated') else ''})"
        )
    if not mems:
        lines.append("- （无）")
    claims = packet.get("claims") or []
    if claims:
        lines += ["", f"## 结构化主张（{len(claims)}）"]
        for c in claims:
            lines.append(f"- {c.get('subject')} · {c.get('predicate')} = {clip_text(c.get('value_text') or c.get('value'), 200)}")
    eps = packet.get("open_episodes") or []
    if eps:
        lines += ["", f"## 未完成情节（{len(eps)}）"]
        for e in eps:
            parts = [f"turn#{e.get('turn_id')}"]
            if e.get("goal"):
                parts.append(f"目标={clip_text(e.get('goal'), 120)}")
            if e.get("next_step"):
                parts.append(f"下一步={clip_text(e.get('next_step'), 120)}")
            lines.append("- " + " · ".join(parts))
    turns = packet.get("recent_turns") or []
    if turns:
        lines += ["", f"## 最近对话（{len(turns)}）"]
        for t in turns:
            lines.append(f"- [{t.get('ts')}] 用户：{t.get('user')} ／ 助手：{t.get('assistant')}")
    lines += ["", "_用 atbrains_recall / atbrains_turn_detail 取原文；用 atbrains_remember / atbrains_handoff 写回。_"]
    return "\n".join(lines)


def execute_tool(store: Any, agent_label: str, raw_name: str, args: dict[str, Any], *,
                 source: str = "mcp", session_id: str = "") -> dict[str, Any]:
    """统一工具执行器：被 MCP、HTTP 工具桥接或直接调用方共用。"""
    name = TOOL_ALIASES.get(str(raw_name or ""), str(raw_name or ""))

    if name == "atbrains_remember":
        return {
            "ok": True,
            "saved": store.remember(
                str(args.get("content") or ""),
                kind=str(args.get("kind") or "note"),
                tags=str(args.get("tags") or ""),
                importance=as_int(args.get("importance"), 5),
                source=source,
                session_id=session_id,
            ),
            "storage": "MariaDB(长期) + Redis(热点)",
        }

    if name == "atbrains_recall":
        query = str(args.get("query") or "")
        limit = max(1, min(20, as_int(args.get("limit"), 5)))
        kind = args.get("kind") or None
        want_agent = args.get("agent") or None
        mems = store.recall_memories(
            query, limit=limit,
            kind=str(kind) if kind else None,
            agent=str(want_agent) if want_agent else None,
        )
        turns = store.search_turns(query, limit=min(limit, 5)) if as_bool(args.get("include_turns"), True) else []
        try:
            claims = store.claim_recall(query=query, limit=min(limit, 5)) if query else []
        except Exception:  # noqa: BLE001
            claims = []
        return {
            "ok": True,
            "memories": clean_rows(mems),
            "turns": clean_rows(turns),
            "claims": claims,
            "agent": agent_label,
        }

    if name == "atbrains_forget":
        mem_id = args.get("id")
        if mem_id is None:
            return {"ok": False, "error": "需要 id"}
        return {"ok": True, "result": store.forget(as_int(mem_id))}

    if name == "atbrains_recent":
        n = max(1, min(50, as_int(args.get("n"), 10)))
        what = str(args.get("what") or "memories")
        if what == "turns":
            turns = store.recent_turns(n)
            for t in turns:
                t["user"] = str(t.get("user") or "")[:200]
                t["assistant"] = str(t.get("assistant") or "")[:200]
            return {"ok": True, "recent_turns": clean_rows(turns)}
        return {"ok": True, "recent_memories": clean_rows(store.hot_recent(n))}

    if name == "atbrains_stats":
        stats = store.stats()
        stats["agent"] = agent_label
        stats["database"] = str(store.cfg.get("mariadb_db") or "")
        stats["shared"] = True
        return {"ok": True, "stats": stats}

    if name == "atbrains_turn_detail":
        turn_id = args.get("turn_id")
        result = store.turn_detail(
            as_int(turn_id) if turn_id is not None else 0,
            session_id=str(args.get("session_id") or ""),
            max_chars=max(200, min(20000, as_int(args.get("max_chars"), 1500))),
        )
        if result.get("found"):
            result["episode"] = store.episode_get(as_int(result["turn"]["id"]))
        return {"ok": True, **result}

    if name == "atbrains_claim_remember":
        return {
            "ok": True,
            "claim": store.claim_remember(
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
                confidence=as_int(args.get("confidence"), 80),
                importance=as_int(args.get("importance"), 5),
            ),
        }

    if name == "atbrains_claim_recall":
        rows = store.claim_recall(
            query=str(args.get("query") or ""),
            subject=str(args.get("subject") or ""),
            predicate=str(args.get("predicate") or ""),
            limit=max(1, min(50, as_int(args.get("limit"), 10))),
            history=as_bool(args.get("history"), False),
        )
        return {"ok": True, "claims": rows, "count": len(rows)}

    if name == "atbrains_episode_set":
        turn_id = args.get("turn_id")
        if turn_id is None:
            return {"ok": False, "error": "需要 turn_id（atbrains_recent 可查最近轮次）"}
        return {
            "ok": True,
            "episode": store.episode_set(
                as_int(turn_id),
                goal=args.get("goal"),
                decisions=args.get("decisions"),
                open_items=args.get("open_items"),
                blockers=args.get("blockers"),
                next_step=args.get("next_step"),
                status=args.get("status"),
                session_id=session_id,
            ),
        }

    if name == "atbrains_episode_get":
        turn_id = args.get("turn_id")
        return {
            "ok": True,
            **store.episode_get(
                as_int(turn_id) if turn_id is not None else 0,
                session_id=str(args.get("session_id") or ""),
            ),
        }

    if name == "atbrains_context":
        return {
            "ok": True,
            **context_packet(
                store,
                agent_label,
                focus=str(args.get("focus") or ""),
                limit=as_int(args.get("limit"), 8),
                include_turns=as_bool(args.get("include_turns"), True),
                include_episodes=as_bool(args.get("include_episodes"), True),
                max_chars=as_int(args.get("max_chars"), _CONTEXT_MEMORY_CHARS),
            ),
        }

    if name == "atbrains_handoff":
        note = str(args.get("note") or "").strip()
        if not note:
            return {"ok": False, "error": "需要 note（留给下一次的那段话）"}
        saved = store.remember(
            note, kind="note", tags="handoff",
            importance=max(1, min(10, as_int(args.get("importance"), 7))),
            source=source, session_id=session_id,
        )
        out: dict[str, Any] = {
            "ok": True,
            "saved": saved,
            "hint": "下一次会话开头用 atbrains_context 读回这段便签",
        }
        turn_id = args.get("turn_id")
        if turn_id is not None:
            out["episode"] = store.episode_set(
                as_int(turn_id),
                next_step=args.get("next_step") or None,
                open_items=as_list(args.get("open_items")) or None,
                session_id=session_id,
            )
        return out

    return {"ok": False, "error": f"未知工具 {name}"}
