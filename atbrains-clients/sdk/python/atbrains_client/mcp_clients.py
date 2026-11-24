#!/usr/bin/env python3
"""atbrains MCP 客户端接入 —— 把服务端配置写进各家 MCP 客户端的配置文件。

参考 mnemos-mcp 的 `mnemos connect <client> --write` 思路：安装脚本自己算出
绝对路径、自己写配置、自己报告改了什么；用户不用手抄 JSON。

支持：
    claude-code   ~/.claude.json                     mcpServers.<name>
    claude-desktop ~/.config/Claude/claude_desktop_config.json
    cursor        ~/.cursor/mcp.json
    codex         ~/.codex/config.toml               [mcp_servers.<name>]
    generic       不写文件，只打印片段

约定：
* 默认 **不写入**，只打印计划和片段；`--write` 才落盘，并且先备份成 *.bak-atbrains。
* JSON 客户端用 json 读写（保留原有键）；TOML 客户端只在缺少该表时追加，
  已有的表不覆盖（codex 配置里可能有用户手写的 env/timeout）。
"""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

SERVER_NAME = "atbrains"


@dataclass
class ServerSpec:
    """要写进客户端的一条 MCP server 定义。"""

    command: str = ""
    args: list[str] = field(default_factory=list)
    transport: str = "stdio"           # stdio | http
    url: str = ""
    token: str = ""

    def json_entry(self) -> dict[str, Any]:
        if self.transport == "http":
            entry: dict[str, Any] = {"type": "http", "url": self.url}
            if self.token:
                entry["headers"] = {"Authorization": f"Bearer {self.token}"}
            return entry
        return {"command": self.command, "args": list(self.args)}

    def toml_block(self) -> str:
        if self.transport == "http":
            lines = [f"[mcp_servers.{SERVER_NAME}]",
                     f'url = "{self.url}"']
            if self.token:
                lines += [f"[mcp_servers.{SERVER_NAME}.headers]",
                          f'Authorization = "Bearer {self.token}"']
            return "\n".join(lines)
        args = ", ".join(json.dumps(a, ensure_ascii=False) for a in self.args)
        return "\n".join([f"[mcp_servers.{SERVER_NAME}]",
                          f'command = {json.dumps(self.command, ensure_ascii=False)}',
                          f"args = [{args}]"])


@dataclass
class Client:
    key: str
    label: str
    kind: str                     # json | toml | external
    path: Optional[Path] = None
    note: str = ""


def _home() -> Path:
    return Path(os.path.expanduser("~"))


def clients() -> dict[str, Client]:
    home = _home()
    return {
        "claude-code": Client("claude-code", "Claude Code", "json", home / ".claude.json"),
        "claude-desktop": Client("claude-desktop", "Claude Desktop", "json",
                                 home / ".config" / "Claude" / "claude_desktop_config.json"),
        "cursor": Client("cursor", "Cursor", "json", home / ".cursor" / "mcp.json"),
        "codex": Client("codex", "Codex CLI", "toml", home / ".codex" / "config.toml"),
        "generic": Client("generic", "任意 MCP 客户端", "external",
                          note="不写文件；把打印出的片段粘进客户端配置即可"),
    }


def snippet(client_key: str, spec: ServerSpec) -> str:
    """客户端可直接使用的配置片段（字符串）。"""
    cl = clients().get(client_key)
    if cl is None:
        raise KeyError(client_key)
    if cl.kind == "toml":
        return cl_toml_snippet(spec)
    return json.dumps({"mcpServers": {SERVER_NAME: spec.json_entry()}},
                      ensure_ascii=False, indent=2)


def cl_toml_snippet(spec: ServerSpec) -> str:
    return spec.toml_block()


def _backup(path: Path) -> Path:
    bak = path.with_suffix(path.suffix + ".bak-atbrains")
    shutil.copy2(path, bak)
    return bak


def _install_json(path: Path, spec: ServerSpec, write: bool) -> tuple[bool, str]:
    data: dict[str, Any] = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
            data = raw if isinstance(raw, dict) else {}
        except Exception as exc:  # noqa: BLE001
            return False, f"现有配置不是合法 JSON，放弃写入：{exc}"
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
        data["mcpServers"] = servers
    old = servers.get(SERVER_NAME)
    new = spec.json_entry()
    if old == new:
        return True, f"已是最新（{path}）"
    if not write:
        return True, f"将写入 {path}（当前{'有旧配置' if old else '没有 atbrains 条目'}）"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        bak = _backup(path)
    else:
        bak = None
    servers[SERVER_NAME] = new
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True, f"已写入 {path}" + (f"（备份 {bak.name}）" if bak else "")


def _install_toml(path: Path, spec: ServerSpec, write: bool) -> tuple[bool, str]:
    block = spec.toml_block()
    text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    if f"[mcp_servers.{SERVER_NAME}]" in text:
        return True, f"{path} 里已有 [mcp_servers.{SERVER_NAME}]，保持不动（需要改就手工编辑）"
    if not write:
        return True, f"将追加到 {path}：\n{block}"
    path.parent.mkdir(parents=True, exist_ok=True)
    bak = _backup(path) if path.exists() else None
    prefix = "" if not text else ("" if text.endswith("\n") else "\n")
    path.write_text(text + (prefix if text else "") + block + "\n", encoding="utf-8")
    return True, f"已追加到 {path}" + (f"（备份 {bak.name}）" if bak else "")


def install(client_key: str, spec: ServerSpec, write: bool = False) -> tuple[bool, str]:
    """把 spec 写进指定客户端。返回 (ok, 人话)。"""
    table = clients()
    cl = table.get(client_key)
    if cl is None:
        return False, f"不认识的客户端 {client_key!r}；可选：{', '.join(table)}"
    if cl.kind == "external":
        return True, f"{cl.label}：把下面这段粘进配置\n\n{snippet(client_key, spec)}"
    assert cl.path is not None
    if cl.kind == "toml":
        return _install_toml(cl.path, spec, write)
    return _install_json(cl.path, spec, write)


def status() -> list[tuple[str, str]]:
    """各客户端当前是否已接入（只看配置文件里有没有 atbrains 条目）。"""
    out: list[tuple[str, str]] = []
    for key, cl in clients().items():
        if cl.kind == "external" or cl.path is None:
            out.append((cl.label, "—"))
            continue
        if not cl.path.exists():
            out.append((cl.label, "未安装/未配置"))
            continue
        text = cl.path.read_text(encoding="utf-8", errors="replace")
        if cl.kind == "toml":
            out.append((cl.label, "已接入" if f"[mcp_servers.{SERVER_NAME}]" in text else "未接入"))
        else:
            try:
                data = json.loads(text or "{}")
                servers = data.get("mcpServers") if isinstance(data, dict) else None
                out.append((cl.label, "已接入" if isinstance(servers, dict)
                            and SERVER_NAME in servers else "未接入"))
            except Exception:  # noqa: BLE001
                out.append((cl.label, "配置文件无法解析"))
    return out
