#!/usr/bin/env python3
"""atbrains MCP（stdio 传输）—— 让任意本地 MCP 客户端直接跑起共享长期记忆。

与 `/mcp` HTTP 端点共用 `AtbrainsMCP` 和 `tools_registry`。
不再寻找任何 Hermes 插件目录：直接导入 `atbrains_server`。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Optional

# 允许直接作为脚本运行（`python3 path/to/mcp_stdio.py`）
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from atbrains_server.config import resolve_config
from atbrains_server.mcp import MCP_PROTOCOLS, PROMPTS, RESOURCES, SERVER_INFO, TOOLS, AtbrainsMCP
from atbrains_server.store import AtbrainsStore

SERVER_NAME = "atbrains-mcp"


def _stderr(msg: str) -> None:
    print(f"[atbrains-mcp] {msg}", file=sys.stderr, flush=True)


class LocalService:
    """stdio 模式下的 store 工厂：按 agent 缓存一个 store（不启重复后台线程）。"""

    def __init__(self, config_home: Optional[str] = None, agent: str = "stdio") -> None:
        self._home = config_home
        self._default_agent = agent
        self._config: Optional[dict[str, Any]] = None
        self._stores: dict[str, AtbrainsStore] = {}

    @property
    def config(self) -> dict[str, Any]:
        if self._config is None:
            self._config = dict(resolve_config(self._home))
        return self._config

    def store_for(self, machine: str = "", agent: str = "default") -> AtbrainsStore:
        who = (agent or self._default_agent or "stdio").strip().lower()[:32] or "stdio"
        store = self._stores.get(who)
        if store is None:
            cfg = dict(self.config)
            cfg["agent"] = who
            if machine:
                cfg["machine"] = machine
            store = AtbrainsStore(cfg, start_workers=False)
            self._stores[who] = store
        return store


def _write(obj: Any) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


def serve(agent: str, config_home: Optional[str] = None) -> int:
    mcp = AtbrainsMCP(LocalService(config_home=config_home, agent=agent))
    _stderr(f"ready (agent={agent}, transport=stdio)")
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            _write({"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32700, "message": f"parse error: {exc}"}})
            continue
        try:
            _code, obj, _headers = mcp.handle(agent, payload)
        except Exception as exc:  # noqa: BLE001
            _stderr(traceback.format_exc())
            _write({"jsonrpc": "2.0",
                    "id": payload.get("id") if isinstance(payload, dict) else None,
                    "error": {"code": -32603, "message": f"{type(exc).__name__}: {exc}"[:300]}})
            continue
        if obj is not None:
            _write(obj)
    return 0


def print_config(agent: str, transport: str = "stdio", url: str = "http://127.0.0.1:8787/mcp") -> int:
    script = str(Path(__file__).resolve())
    if transport == "http":
        snippet = {"mcpServers": {"atbrains": {"type": "http", "url": url,
                                               "headers": {"Authorization": "Bearer <服务令牌>"}}}}
    else:
        snippet = {"mcpServers": {"atbrains": {"command": sys.executable,
                                               "args": [script, "--agent", agent]}}}
    print(json.dumps(snippet, ensure_ascii=False, indent=2))
    return 0


def selftest(agent: str = "selftest", config_home: Optional[str] = None) -> int:
    """进程内自检：initialize → tools/list → resources/read → tools/call（只读）。"""
    mcp = AtbrainsMCP(LocalService(config_home=config_home, agent=agent))
    problems: list[str] = []

    def call(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
        _code, obj, _h = mcp.handle(agent, payload)
        return obj

    init = call({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": MCP_PROTOCOLS[0], "capabilities": {},
                            "clientInfo": {"name": "selftest", "version": "0"}}})
    if not init or init.get("result", {}).get("protocolVersion") not in MCP_PROTOCOLS:
        problems.append(f"initialize 失败: {init}")
    tools = call({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    names = [t["name"] for t in (tools or {}).get("result", {}).get("tools", [])]
    if len(names) != len(TOOLS):
        problems.append(f"tools/list 数量不符: {len(names)} != {len(TOOLS)}")
    for required in ("atbrains_context", "atbrains_handoff", "atbrains_recall"):
        if required not in names:
            problems.append(f"缺工具 {required}")
    res = call({"jsonrpc": "2.0", "id": 3, "method": "resources/list", "params": {}})
    if len((res or {}).get("result", {}).get("resources", [])) != len(RESOURCES):
        problems.append("resources/list 不符")
    prompts = call({"jsonrpc": "2.0", "id": 4, "method": "prompts/list", "params": {}})
    if len((prompts or {}).get("result", {}).get("prompts", [])) != len(PROMPTS):
        problems.append("prompts/list 不符")
    for uri in ("atbrains://context", "atbrains://stats"):
        got = call({"jsonrpc": "2.0", "id": 5, "method": "resources/read", "params": {"uri": uri}})
        text = ((got or {}).get("result", {}).get("contents") or [{}])[0].get("text") or ""
        if not text:
            problems.append(f"resources/read {uri} 为空")
    ctx = call({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                "params": {"name": "atbrains_context",
                           "arguments": {"limit": 3, "include_turns": False}}})
    payload = json.loads(((ctx or {}).get("result", {}).get("content") or [{}])[0].get("text") or "{}")
    if not payload.get("ok"):
        problems.append(f"atbrains_context 失败: {payload}")
    stats = call({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                  "params": {"name": "atbrains_stats", "arguments": {}}})
    if not stats:
        problems.append("atbrains_stats 无响应")

    if problems:
        for p in problems:
            print(f"❌ {p}", file=sys.stderr)
        return 1
    print(f"✅ stdio MCP 自检通过：{len(names)} 工具 / {len(RESOURCES)} 资源 / {len(PROMPTS)} 提示词"
          f"（agent={agent}）")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog=SERVER_NAME, description="atbrains MCP 服务端（stdio）")
    parser.add_argument("--agent", default=os.environ.get("ATBRAINS_AGENT") or "stdio",
                        help="写入方身份（记忆的 agent 列），默认 $ATBRAINS_AGENT 或 stdio")
    parser.add_argument("--config-home", "--hermes-home", dest="config_home", default=None,
                        help="配置目录（默认按 ATBRAINS_HOME / ~/.config/atbrains / /etc/atbrains 自动解析）")
    parser.add_argument("--selftest", action="store_true", help="进程内自检后退出")
    parser.add_argument("--print-config", action="store_true", help="打印客户端配置片段")
    parser.add_argument("--transport", default="stdio", choices=("stdio", "http"),
                        help="--print-config 用的传输方式")
    parser.add_argument("--url", default="http://127.0.0.1:8787/mcp", help="http 模式的地址")
    parser.add_argument("--version", action="store_true", help="打印版本后退出")
    args = parser.parse_args(argv)

    if args.version:
        print(f"{SERVER_INFO['name']} {SERVER_INFO['version']}")
        return 0
    if args.selftest:
        return selftest(args.agent, args.config_home)
    if args.print_config:
        return print_config(args.agent, args.transport, args.url)
    return serve(args.agent, args.config_home)


if __name__ == "__main__":
    raise SystemExit(main())
