"""`hermes atbrains-memory` 瘦命令行 —— 通过 HTTP `/v1` 与 atbrains-server 通信。"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

from . import _resolve_client_cfg

try:
    from atbrains_client import AtbrainsClient
except ImportError:
    sdk = Path(__file__).resolve().parents[2] / "sdk" / "python"
    if str(sdk) not in sys.path:
        sys.path.insert(0, str(sdk))
    from atbrains_client import AtbrainsClient  # type: ignore


def _out(text: str) -> None:
    print(text)


def _fmt_bytes(n: int) -> str:
    val = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if val < 1024 or unit == "GB":
            return f"{val:.1f} {unit}" if unit != "B" else f"{int(val)} B"
        val /= 1024.0
    return f"{val:.1f} GB"


def _client() -> AtbrainsClient:
    cfg = _resolve_client_cfg()
    return AtbrainsClient.from_config(cfg)


def _cmd_status(args) -> int:
    cli = _client()
    h = cli.health()
    st = cli.status(light=False)
    sp = cli.spool_status()
    _out("atbrains 客户端状态（HTTP /v1 模式）")
    _out("─" * 46)
    _out(f"服务端   {cli.base_url} (protocol v{h.get('version', '?')})")
    _out(f"身份     machine={st.get('machine')} · agent={st.get('agent')} · role={st.get('role', 'agent')}")
    ping = st.get("ping") or {}
    _out(f"存储     Redis={'✓' if ping.get('redis') else '✗'} · MariaDB={'✓' if ping.get('mariadb') else '✗'}")
    stats = st.get("stats") or {}
    m = stats.get("mysql") or {}
    if m:
        _out(f"记忆     {m.get('memories_active', '?')}/{m.get('memories_total', '?')} 活跃/总 · "
             f"轮次 {m.get('turns', '?')} · 会话 {m.get('sessions', '?')} · "
             f"{_fmt_bytes(int(m.get('size_bytes') or 0))}")
    _out(f"离线队列 {sp.get('pending', 0)} 条待重放 ({_fmt_bytes(int(sp.get('bytes') or 0))})")
    return 0


def _cmd_stats(args) -> int:
    _out(json.dumps(_client().status(light=False), ensure_ascii=False, indent=2))
    return 0


def _cmd_search(args) -> int:
    query = " ".join(args.query)
    res = _client().recall(query, limit=max(1, min(50, args.limit)), include_claims=True, include_turn_items=True)
    items = res.get("items") or []
    turns = res.get("turns") or []
    _out(f"检索: {query!r}  →  记忆 {len(items)} 条 / 对话 {len(turns)} 条")
    _out("─" * 46)
    for m in items:
        tags = f" #{m['tags']}" if m.get("tags") else ""
        mach = f" @{m['machine']}" if m.get("machine") else ""
        _out(f"[{m.get('id')}] ({m.get('kind')}·{m.get('importance')}){tags}{mach} via={m.get('via')}")
        _out(f"    {str(m.get('preview') or '')[:160]}")
    if turns:
        _out("\n相关历史对话：")
        for t in turns:
            _out(f"  {str(t.get('ts'))[:16]} [{str(t.get('session_id'))[:20]}] "
                 f"{str(t.get('user_content') or '')[:120]}")
    return 0


def _cmd_remember(args) -> int:
    res = _client().remember(
        " ".join(args.content),
        kind=args.kind,
        tags=args.tags,
        importance=args.importance,
        source="cli",
    )
    if res.get("queued"):
        _out(f"⏳ 服务端暂不可达，已入本地离线队列（待重放 {res.get('spool_pending')} 条）")
        return 0
    saved = res.get("saved") or {}
    _out(f"已保存: id={saved.get('id')} kind={saved.get('kind')} importance={saved.get('importance')}")
    return 0


def _cmd_recent(args) -> int:
    res = _client().recent(limit=max(1, min(50, args.n)))
    turns = res.get("turns") or []
    if not turns:
        _out("最近对话流为空。")
        return 0
    _out(f"最近 {len(turns)} 条对话")
    for t in turns:
        _out(f"[{str(t.get('session') or '')[:18]}] @{t.get('machine') or '-'} "
             f"{str(t.get('user') or '').replace(chr(10), ' ')[:110]}")
    return 0


def _cmd_forget(args) -> int:
    res = _client().forget(args.id)
    _out(json.dumps(res, ensure_ascii=False))
    return 0


def _cmd_restore(args) -> int:
    res = _client().restore(args.id)
    _out(json.dumps(res, ensure_ascii=False))
    return 0


def _cmd_context(args) -> int:
    res = _client().context(focus=" ".join(args.focus or []), fmt="markdown")
    _out(res.get("markdown") or json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def _cmd_spool_flush(args) -> int:
    cli = _client()
    res = cli.flush_spool(max_items=args.max)
    _out(f"离线队列重放: {json.dumps(res, ensure_ascii=False)}")
    return 0 if res.get("remaining", 0) == 0 else 1


def _cmd_check(args) -> int:
    cli = _client()
    t0 = time.monotonic()
    h = cli.health()
    dt = int((time.monotonic() - t0) * 1000)
    st = cli.status(light=True)
    out = {
        "ok": bool(h.get("ok")),
        "url": cli.base_url,
        "protocol_version": h.get("version"),
        "latency_ms": dt,
        "machine": st.get("machine"),
        "agent": st.get("agent"),
        "spool": cli.spool_status(),
    }
    _out(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out["ok"] else 1


def register_cli(subparser) -> None:
    subs = subparser.add_subparsers(dest="atbrains_memory_command")
    subs.add_parser("status", help="服务端连通性 / 身份 / 统计总览")
    subs.add_parser("stats", help="JSON 格式统计")

    p = subs.add_parser("search", help="检索记忆与历史对话")
    p.add_argument("query", nargs="+")
    p.add_argument("--limit", type=int, default=8)

    p = subs.add_parser("remember", help="保存一条长期记忆")
    p.add_argument("content", nargs="+")
    p.add_argument("--kind", default="note")
    p.add_argument("--tags", default="")
    p.add_argument("--importance", type=int, default=6)

    p = subs.add_parser("recent", help="查看最近对话流")
    p.add_argument("--n", type=int, default=10)

    p = subs.add_parser("forget", help="停用记忆")
    p.add_argument("id", type=int)

    p = subs.add_parser("restore", help="恢复记忆")
    p.add_argument("id", type=int)

    p = subs.add_parser("context", help="拉取醒来包")
    p.add_argument("focus", nargs="*")

    p = subs.add_parser("spool-flush", help="立即重放本地离线队列")
    p.add_argument("--max", type=int, default=100)

    subs.add_parser("check", help="检查 /v1 服务连通性与协议版本")
    subs.add_parser("remote-check", help="check 的兼容别名")
    subs.add_parser("doctor", help="check 的兼容别名")

    subparser.set_defaults(func=atbrains_memory_command)


def atbrains_memory_command(args) -> None:
    sub = getattr(args, "atbrains_memory_command", None)
    handlers = {
        "status": _cmd_status,
        "stats": _cmd_stats,
        "search": _cmd_search,
        "remember": _cmd_remember,
        "recent": _cmd_recent,
        "forget": _cmd_forget,
        "restore": _cmd_restore,
        "context": _cmd_context,
        "spool-flush": _cmd_spool_flush,
        "check": _cmd_check,
        "remote-check": _cmd_check,
        "doctor": _cmd_check,
    }
    fn: Any = handlers.get(sub)
    if fn is None:
        print("用法: hermes atbrains-memory <status|stats|search|remember|recent|forget|restore|context|spool-flush|check>")
        return
    try:
        rc = fn(args)
    except Exception as exc:  # noqa: BLE001
        print(f"atbrains 命令失败: {exc}", file=sys.stderr)
        rc = 1
    if rc:
        raise SystemExit(rc)
