"""`hermes atbrains-memory` 瘦命令行 —— 通过 HTTP `/v1` 与 atbrains-server 通信。"""
from __future__ import annotations

import argparse
import getpass
import json
import sys
import time
from pathlib import Path
from typing import Any

try:
    from . import _hermes_home, _resolve_client_cfg
except ImportError:  # 直接 python3 cli.py 独立运行时的兜底
    here = Path(__file__).resolve().parent
    if str(here.parent) not in sys.path:
        sys.path.insert(0, str(here.parent))
    sdk = here.parents[1] / "sdk" / "python"
    if (sdk / "atbrains_client").is_dir() and str(sdk) not in sys.path:
        sys.path.insert(0, str(sdk))
    from atbrains_client import resolve_client_config as _rcfg  # type: ignore
    import os as _os

    def _hermes_home(hermes_home=None):  # type: ignore[misc]
        if hermes_home:
            return Path(hermes_home).expanduser()
        return Path(_os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")).expanduser()

    def _resolve_client_cfg(hermes_home=None):  # type: ignore[misc]
        home = _hermes_home(hermes_home)
        cfg = _rcfg(str(home), client_name="hermes")
        if not cfg.spool_dir:
            cfg.spool_dir = str(home / "atbrains-spool")
        return cfg

try:
    from atbrains_client import AtbrainsClient, AtbrainsClientError, mask_token, save_client_token
except ImportError:
    sdk = Path(__file__).resolve().parents[2] / "sdk" / "python"
    if str(sdk) not in sys.path:
        sys.path.insert(0, str(sdk))
    from atbrains_client import AtbrainsClient, AtbrainsClientError, mask_token, save_client_token  # type: ignore


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


def _cmd_tokset(args) -> int:
    tok = str(getattr(args, "token", "") or "").strip()
    if not tok:
        if sys.stdin.isatty():
            tok = getpass.getpass("输入 atbrains Token: ").strip()
        else:
            tok = sys.stdin.read().strip()
    if not tok:
        _out("❌ token 不能为空（用法: hermes atbrains-memory tokset <token> [--url <service_url>]）")
        return 1
    home = _hermes_home(getattr(args, "home", None))
    url = str(getattr(args, "url", "") or "").strip() or None
    saved = save_client_token(
        tok,
        config_home=str(home),
        base_url=url,
        json_filename="atbrains.json",
        env_key="ATBRAINS_TOKEN",
    )
    _out(f"✅ 已保存 Token ({saved['masked']}) → {saved['env_path']} (权限 600)")
    if saved.get("base_url"):
        _out(f"✅ 已保存服务地址 {saved['base_url']} → {saved['json_path']}")

    if getattr(args, "no_verify", False):
        return 0
    cfg = _resolve_client_cfg(str(home))
    if not cfg.base_url:
        _out("ℹ️  尚未配置 service_url，可用 `--url http://<server>:8080` 一并设置并校验连通性。")
        return 0
    cli = AtbrainsClient(
        base_url=cfg.base_url,
        token=tok,
        client_name="hermes",
        timeout_ms=cfg.timeout_ms,
        write_timeout_ms=cfg.write_timeout_ms,
    )
    try:
        info = cli.verify()
    except AtbrainsClientError as exc:
        if exc.status == 401:
            _out(f"❌ 令牌已写入但服务端拒绝认证 (401)：请确认令牌未撤销且与服务端一致 ({cfg.base_url})")
            return 1
        _out(f"⚠️  令牌已写入，但连接服务端校验失败：{exc}")
        return 1
    _out(
        f"✅ 校验通过 ({info['latency_ms']}ms) · 身份 {info.get('machine') or '-'} | {info.get('agent') or '-'} "
        f"· 客户端 {info.get('client') or 'hermes'} · 已上报在线"
    )
    return 0


def _cmd_tokshow(args) -> int:
    home = _hermes_home(getattr(args, "home", None))
    cfg = _resolve_client_cfg(str(home))
    _out(f"配置目录   {home}")
    _out(f"服务地址   {cfg.base_url or '(未配置)'}")
    _out(f"Token     {mask_token(cfg.token)}")
    if getattr(args, "verify", False) and cfg.base_url and cfg.token:
        info = AtbrainsClient.from_config(cfg).verify()
        _out(
            f"校验结果   ✅ 在线 ({info['latency_ms']}ms) · "
            f"{info.get('machine') or '-'} | {info.get('agent') or '-'} ({info.get('client') or '-'})"
        )
    return 0


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

    for cmd_name, help_txt in (
        ("tokset", "设置 atbrains Token（写入 ~/.hermes/.env 并校验在线）"),
        ("token-set", "tokset 的别名"),
        ("set-token", "tokset 的别名"),
    ):
        p = subs.add_parser(cmd_name, help=help_txt)
        p.add_argument("token", nargs="?", default="", help="Bearer 令牌（留空则交互式/stdin 读取）")
        p.add_argument("--url", default="", help="可选：同时设置服务端地址（写入 atbrains.json）")
        p.add_argument("--home", default=None, help="可选：指定 Hermes 配置目录（默认 ~/.hermes）")
        p.add_argument("--no-verify", action="store_true", help="仅写入配置文件，跳过联网校验")

    for cmd_name, help_txt in (
        ("tokshow", "查看当前已配置的 Token（脱敏）与服务地址"),
        ("token-show", "tokshow 的别名"),
    ):
        p = subs.add_parser(cmd_name, help=help_txt)
        p.add_argument("--home", default=None, help="可选：指定 Hermes 配置目录")
        p.add_argument("--verify", action="store_true", help="顺带联网校验令牌")

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
        "tokset": _cmd_tokset,
        "token-set": _cmd_tokset,
        "set-token": _cmd_tokset,
        "tokshow": _cmd_tokshow,
        "token-show": _cmd_tokshow,
        "check": _cmd_check,
        "remote-check": _cmd_check,
        "doctor": _cmd_check,
    }
    fn: Any = handlers.get(sub)
    if fn is None:
        print("用法: hermes atbrains-memory <status|stats|search|remember|recent|forget|restore|context|spool-flush|tokset|tokshow|check>")
        return
    try:
        rc = fn(args)
    except Exception as exc:  # noqa: BLE001
        print(f"atbrains 命令失败: {exc}", file=sys.stderr)
        rc = 1
    if rc:
        raise SystemExit(rc)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog="hermes atbrains-memory", description="atbrains Hermes 客户端命令")
    register_cli(parser)
    atbrains_memory_command(parser.parse_args())
