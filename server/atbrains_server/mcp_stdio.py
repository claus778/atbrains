#!/usr/bin/env python3
"""atbrains MCP（stdio 传输）—— 让任意 MCP 客户端直接跑起共享长期记忆。

与 `/mcp` HTTP 端点共用同一份工具实现（`atbrains_mcp.TOOLS` / `AtbrainsMCP`）：
本文件只补两件事 —— 传输层（stdio，一行一条 JSON-RPC）和本地 store 工厂。

为什么要 stdio
--------------
HTTP 端点要控制台常驻、要令牌、要隧道；Claude Code / Codex / Cursor /
Claude Desktop 这类客户端更愿意自己拉起一个子进程（`command` + `args`）。
stdio 模式下不经过网络，身份由 `--agent` 决定（默认 `$ATBRAINS_AGENT` 或 `stdio`）。

用法
----
    python3 mcp_stdio.py                    # 给 MCP 客户端当 command 用
    python3 mcp_stdio.py --agent omp        # 指定写入方身份
    python3 mcp_stdio.py --selftest         # 进程内自检（initialize/list/call）
    python3 mcp_stdio.py --print-config     # 打印客户端配置片段

约定
----
* stdout 只走协议（每条一行 JSON），日志一律 stderr —— 否则客户端解析失败。
* 不 import 插件 `__init__.py`（那是 Hermes 宿主的 provider 入口）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
SERVER_NAME = "atbrains-mcp"


def _stderr(msg: str) -> None:
    print(f"[atbrains-mcp] {msg}", file=sys.stderr, flush=True)


def _memory_dir() -> Path:
    """定位 atbrains-memory 插件目录（core.py 所在处）。"""
    candidates: list[Path] = []
    env = os.environ.get("ATBRAINS_MEMORY_DIR")
    if env:
        candidates.append(Path(env).expanduser())
    home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    candidates.append(Path(home) / "plugins" / "atbrains-memory")
    candidates.append(HERE.parent / "atbrains-memory")      # 仓库里直接跑
    candidates.append(HERE / "atbrains-memory")
    for path in candidates:
        if (path / "core.py").is_file():
            return path
    raise SystemExit(
        "找不到 atbrains-memory 插件目录（core.py）。"
        "设置 ATBRAINS_MEMORY_DIR，或先跑仓库根目录 ./install.sh"
    )


def _load_core() -> Any:
    memory_dir = _memory_dir()
    if str(memory_dir) not in sys.path:
        sys.path.insert(0, str(memory_dir))
    import core  # type: ignore[import-not-found]  # noqa: E402 —— 目录由 _memory_dir() 决定
    return core


class LocalService:
    """stdio 模式下的 store 工厂：按 agent 缓存一个 store（复用连接池）。

    `AtbrainsMCP` 只要求 service 有 `store_for(machine, agent) -> store`，
    所以 HTTP 的 MemoryService 与本类可以互换。
    """

    def __init__(self, hermes_home: Optional[str] = None, agent: str = "stdio") -> None:
        self._core = _load_core()
        self._home = hermes_home
        self._default_agent = agent
        self._config: Optional[dict[str, Any]] = None
        self._stores: dict[str, Any] = {}

    @property
    def config(self) -> dict[str, Any]:
        if self._config is None:
            self._config = dict(self._core.resolve_config(self._home))
        return self._config

    def store_for(self, machine: str = "", agent: str = "hermes") -> Any:
        who = (agent or self._default_agent or "stdio").strip().lower()[:32] or "stdio"
        store = self._stores.get(who)
        if store is None:
            cfg = dict(self.config)
            cfg["agent"] = who
            store = self._core.AtbrainsStore(cfg)
            self._stores[who] = store
        return store


def _write(obj: Any) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


def serve(agent: str, hermes_home: Optional[str] = None) -> int:
    """stdio 主循环：一行一条 JSON-RPC 2.0（单条或批量）。"""
    from atbrains_mcp import AtbrainsMCP

    mcp = AtbrainsMCP(LocalService(hermes_home=hermes_home, agent=agent))
    _stderr(f"ready (agent={agent}, transport=stdio, memory_dir={_memory_dir()})")
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
        except Exception as exc:  # noqa: BLE001 —— 传输层不许因为一次调用崩掉
            _stderr(traceback.format_exc())
            _write({"jsonrpc": "2.0", "id": payload.get("id") if isinstance(payload, dict) else None,
                    "error": {"code": -32603, "message": f"{type(exc).__name__}: {exc}"[:300]}})
            continue
        if obj is not None:
            _write(obj)
    return 0


def print_config(agent: str, transport: str = "stdio", url: str = "http://127.0.0.1:8787/mcp") -> int:
    """打印可直接粘进客户端配置的片段（stdio 或 http）。"""
    script = str(HERE / "mcp_stdio.py")
    if transport == "http":
        snippet = {"mcpServers": {"atbrains": {"type": "http", "url": url,
                                               "headers": {"Authorization": "Bearer <服务令牌>"}}}}
    else:
        snippet = {"mcpServers": {"atbrains": {"command": sys.executable,
                                               "args": [script, "--agent", agent]}}}
    print(json.dumps(snippet, ensure_ascii=False, indent=2))
    return 0


def selftest(agent: str = "selftest") -> int:
    """进程内自检：initialize → tools/list → resources/read → tools/call。

    不写库（只读调用），失败即返回非 0 —— 给 `hermes atbrains mcp doctor` 用。
    """
    from atbrains_mcp import MCP_PROTOCOLS, PROMPTS, RESOURCES, TOOLS, AtbrainsMCP

    mcp = AtbrainsMCP(LocalService(agent=agent))
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
    parser = argparse.ArgumentParser(
        prog=SERVER_NAME, description="atbrains MCP 服务端（stdio）")
    parser.add_argument("--agent", default=os.environ.get("ATBRAINS_AGENT") or "stdio",
                        help="写入方身份（记忆的 agent 列），默认 $ATBRAINS_AGENT 或 stdio")
    parser.add_argument("--hermes-home", default=None, help="Hermes home（默认 $HERMES_HOME 或 ~/.hermes）")
    parser.add_argument("--selftest", action="store_true", help="进程内自检后退出")
    parser.add_argument("--print-config", action="store_true", help="打印客户端配置片段")
    parser.add_argument("--transport", default="stdio", choices=("stdio", "http"),
                        help="--print-config 用的传输方式")
    parser.add_argument("--url", default="http://127.0.0.1:8787/mcp", help="http 模式的地址")
    parser.add_argument("--version", action="store_true", help="打印版本后退出")
    args = parser.parse_args(argv)

    if args.version:
        from atbrains_mcp import SERVER_INFO
        print(f"{SERVER_INFO['name']} {SERVER_INFO['version']}")
        return 0
    if args.selftest:
        return selftest(args.agent)
    if args.print_config:
        return print_config(args.agent, args.transport, args.url)
    return serve(args.agent, args.hermes_home)


if __name__ == "__main__":
    raise SystemExit(main())
