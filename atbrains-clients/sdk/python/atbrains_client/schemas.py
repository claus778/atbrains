"""atbrains 通用工具 Schema 定义（纯标准库，供服务端与各 Python Agent 适配器共用）。"""
from __future__ import annotations

from typing import Any

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


def openai_tool_schemas() -> list[dict[str, Any]]:
    """返回 OpenAI / Hermes / LiteLLM 通用的 function schema 列表。"""
    return [
        {
            "name": t["name"],
            "description": t["description"],
            "parameters": t["inputSchema"],
        }
        for t in TOOLS
    ]
