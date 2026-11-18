#!/usr/bin/env python3
"""atbrains MCP 端点 —— 把共享长期记忆工具暴露给任意支持 MCP 的 agent。

传输层（`/mcp` HTTP 与 `mcp_stdio.py` stdio）只负责收发 JSON-RPC 2.0，
工具 schema、醒来包拼装与分发统一走 `tools_registry.py` —— 单一真源，零重复 SQL。
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Optional

from . import __version__
from .tools_registry import (
    INSTRUCTIONS,
    TOOLS,
    TOOL_ALIASES,
    context_markdown,
    context_packet,
    execute_tool,
)

MCP_PROTOCOL_DEFAULT = "2025-06-18"
MCP_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "atbrains", "version": __version__, "title": "atbrains 共享长期记忆"}

RESOURCES: list[dict[str, Any]] = [
    {
        "uri": "atbrains://context",
        "name": "醒来包",
        "title": "atbrains 醒来包（重点记忆 + 主张 + 未完成情节 + 最近对话）",
        "description": "会话开始时读取：我是谁、我记着什么、上次做到哪。markdown 文本。",
        "mimeType": "text/markdown",
    },
    {
        "uri": "atbrains://stats",
        "name": "库统计",
        "title": "atbrains 库统计",
        "description": "记忆/对话/主张/图谱计数、向量覆盖与存储占用。JSON。",
        "mimeType": "application/json",
    },
    {
        "uri": "atbrains://recent/memories",
        "name": "最近记忆",
        "title": "最近写入的记忆（Redis 热点镜像）",
        "description": "最近写入的 20 条记忆。JSON。",
        "mimeType": "application/json",
    },
    {
        "uri": "atbrains://recent/turns",
        "name": "最近对话",
        "title": "最近对话归档（一行摘要，20 条）",
        "description": "最近 20 轮对话的用户/助手摘要。JSON。",
        "mimeType": "application/json",
    },
]

PROMPTS: list[dict[str, Any]] = [
    {
        "name": "wake-up",
        "title": "带记忆开始",
        "description": "把 atbrains 醒来包作为会话的第一条上下文注入。",
        "arguments": [{"name": "focus", "description": "当前任务/问题关键词（可选）", "required": False}],
    },
    {
        "name": "handoff",
        "title": "交接给下一次会话",
        "description": "让模型把当前进度写成一段 handoff 便签并存进 atbrains。",
        "arguments": [{"name": "next_step", "description": "下一步（可选）", "required": False}],
    },
]


class AtbrainsMCP:
    """MCP 的传输无关实现：HTTP `/mcp` 与 `mcp_stdio` 共用。"""

    def __init__(self, service: Any) -> None:
        self.service = service

    def handle(self, agent: str, payload: Any) -> tuple[int, Any, dict[str, str]]:
        headers = {"Mcp-Session-Id": uuid.uuid4().hex}
        if isinstance(payload, list):
            out = []
            for item in payload:
                resp = self._one(agent, item)
                if resp is not None:
                    out.append(resp)
            if not out:
                return 202, None, headers
            return 200, out, headers
        resp = self._one(agent, payload)
        if resp is None:
            return 202, None, headers
        return 200, resp, headers

    def _one(self, agent: str, msg: Any) -> Optional[dict[str, Any]]:
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
                    "instructions": INSTRUCTIONS.format(agent=agent),
                },
            }
        if method in ("notifications/initialized", "initialized", "notifications/cancelled"):
            return None
        if method in ("ping", "logging/setLevel"):
            return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}
        if method == "resources/list":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"resources": RESOURCES}}
        if method == "resources/read":
            uri = str(params.get("uri") or "")
            try:
                contents = self.read_resource(agent, uri)
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
            got = self.get_prompt(agent, str(params.get("name") or ""), prompt_args)
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
                data = self.call(agent, name, args)
                text = json.dumps(data, ensure_ascii=False, default=str)
                is_error = isinstance(data, dict) and data.get("ok") is False
            except Exception as exc:  # noqa: BLE001
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

    def read_resource(self, agent: str, uri: str) -> Optional[dict[str, Any]]:
        store = self.service.store_for("", agent=agent)
        if uri == "atbrains://context":
            packet = context_packet(store, agent)
            return {"uri": uri, "mimeType": "text/markdown", "text": context_markdown(packet)}
        if uri == "atbrains://stats":
            data = self.call(agent, "atbrains_stats", {})
            return {"uri": uri, "mimeType": "application/json",
                    "text": json.dumps(data, ensure_ascii=False, default=str)}
        if uri == "atbrains://recent/memories":
            data = self.call(agent, "atbrains_recent", {"n": 20, "what": "memories"})
            return {"uri": uri, "mimeType": "application/json",
                    "text": json.dumps(data, ensure_ascii=False, default=str)}
        if uri == "atbrains://recent/turns":
            data = self.call(agent, "atbrains_recent", {"n": 20, "what": "turns"})
            return {"uri": uri, "mimeType": "application/json",
                    "text": json.dumps(data, ensure_ascii=False, default=str)}
        return None

    def get_prompt(self, agent: str, name: str, args: dict[str, Any]) -> Optional[dict[str, Any]]:
        store = self.service.store_for("", agent=agent)
        if name == "wake-up":
            focus = str(args.get("focus") or "")
            packet = context_packet(store, agent, focus=focus)
            return {
                "description": "atbrains 醒来包：把这段放在会话最前面。",
                "messages": [{"role": "user", "content": {"type": "text", "text": context_markdown(packet)}}],
            }
        if name == "handoff":
            next_step = str(args.get("next_step") or "")
            text = ("请把当前这轮工作整理成一段 handoff 便签，然后调用 "
                    "atbrains_handoff(note=..., next_step=..., open_items=[...]) 存进 atbrains。"
                    "便签写清：做到哪、坑在哪、下一步做什么。")
            if next_step:
                text += f"\n下一步：{next_step}"
            return {
                "description": "交接给下一次会话",
                "messages": [{"role": "user", "content": {"type": "text", "text": text}}],
            }
        return None

    def call(self, agent: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
        store = self.service.store_for("", agent=agent)
        return execute_tool(store, agent, name, args, source="mcp")


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


BrainMCP = AtbrainsMCP
