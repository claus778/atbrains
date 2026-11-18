#!/usr/bin/env python3
"""`atbrains-server` 独立命令行 —— 通用记忆服务端的管理、迁移与 MCP 入口。

示例:
    atbrains-server serve [--port 8787] [--background]
    atbrains-server stop
    atbrains-server status
    atbrains-server migrate
    atbrains-server doctor
    atbrains-server drain [--force]
    atbrains-server token-list | token-add <machine|agent> | token-rm <name>
    atbrains-server mcp <serve|install|print|doctor> ...
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from . import PROTOCOL_VERSION, __version__
from .config import resolve_config, resolve_data_dir, resolve_server_http_config
from .store import AtbrainsStore
from .tokens import TokenStore, mask


def _p(msg: str = "") -> None:
    print(msg, flush=True)


def _ok(cond: object) -> str:
    return "✅" if cond else "❌"


def _short(s: str, n: int) -> str:
    s = (s or "").replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def _ts(v: object) -> str:
    return str(v)[:16] if v is not None else ""


def _pidfile(config_home: Optional[str] = None) -> Path:
    return resolve_data_dir(config_home) / "atbrains-server.pid"


def _tokens(config_home: Optional[str] = None) -> TokenStore:
    cfg = resolve_config(config_home)
    return TokenStore(Path(str(cfg["data_dir"])) / "service-tokens.json")


def _store(config_home: Optional[str] = None) -> AtbrainsStore:
    return AtbrainsStore(resolve_config(config_home), start_workers=False)


def cmd_status(args) -> int:
    try:
        d = _store(args.config_home).overview()
    except Exception as e:  # noqa: BLE001
        _p(f"❌ 无法读取记忆系统: {e}")
        return 1
    st = d.get("stats", {}).get("mysql", {}) if isinstance(d.get("stats"), dict) else {}
    rd = d.get("stats", {}).get("redis", {}) if isinstance(d.get("stats"), dict) else {}
    q = d.get("qdrant", {})
    cov = q.get("coverage", {})
    v = d.get("vector", {}) if isinstance(d.get("vector"), dict) else {}

    _p(f"🧠 atbrains-server v{__version__} (protocol v{PROTOCOL_VERSION}) · {d.get('ts')}")
    _p("─" * 58)
    if st:
        _p(f"  MariaDB(长期)   记忆 {st.get('memories_active','?')}/{st.get('memories_total','?')} 活跃/总")
        _p(f"                  轮次 {st.get('turns','?')} · 会话 {st.get('sessions','?')} · "
           f"{round((st.get('size_bytes', 0) / 1048576), 2)} MB")
    else:
        _p(f"  MariaDB(长期)   ❌ {d.get('stats', {}).get('error', '不可用')}")
    if rd:
        _p(f"  Redis(短期)     {rd.get('keys','?')} keys · 热点 {rd.get('hot_index','?')} · "
           f"流 {rd.get('recent_stream','?')} · {rd.get('used_memory','?')}")
    else:
        _p("  Redis(短期)     ❌ 不可用")
    if q.get("error"):
        _p(f"  Qdrant(向量)    ❌ {q['error']}")
    else:
        cov_ok = cov.get("active_memories") is not None and cov.get("with_vector") == cov.get("active_memories")
        _p(f"  Qdrant(向量)    {q.get('points','?')} 点 · {q.get('dim','?')} 维 · {q.get('distance','')} · "
           f"{q.get('status','?')}")
        _p(f"                  覆盖 {cov.get('with_vector','?')}/{cov.get('active_memories','?')} "
           f"{_ok(cov_ok)}" + (f" · 缺 {len(cov.get('missing', []))}" if cov.get("missing") else "")
           + (f" · 残留 {len(cov.get('stale_vectors', []))}" if cov.get("stale_vectors") else ""))
    if v and not v.get("error"):
        _p(f"  向量开关        {v.get('enabled')} · 模型 {str(v.get('embed_model') or '?')}")
    return 0


def cmd_migrate(args) -> int:
    cfg = resolve_config(args.config_home)
    st = AtbrainsStore(cfg, start_workers=False)
    ver = st.migrate()
    _p(f"✅ schema 迁移完成，当前版本 v{ver} (db={cfg['mariadb_db']})")
    return 0


def cmd_serve(args) -> int:
    http_cfg = resolve_server_http_config(args.config_home)
    port = args.port or int(http_cfg["port"])
    data_dir = Path(http_cfg["data_dir"])
    data_dir.mkdir(parents=True, exist_ok=True)
    pidfile = _pidfile(args.config_home)
    env = {**os.environ, "ATBRAINS_PORT": str(port), "ATBRAINS_DATA_DIR": str(data_dir)}
    if args.config_home:
        env["ATBRAINS_HOME"] = str(args.config_home)
    if args.background:
        if pidfile.exists():
            pid = pidfile.read_text().strip()
            if pid and Path(f"/proc/{pid}").exists():
                _p(f"⚠️ 服务端已在运行 (pid {pid})。先 `atbrains-server stop`")
                return 1
        logfile = data_dir / "server.log"
        log = open(logfile, "ab")
        p = subprocess.Popen(
            [sys.executable, "-m", "atbrains_server.http"],
            env=env, stdout=log, stderr=log, start_new_session=True,
        )
        pidfile.write_text(str(p.pid), encoding="utf-8")
        time.sleep(2)
        if Path(f"/proc/{p.pid}").exists():
            tok_file = data_dir / ".token"
            tok = tok_file.read_text(encoding="utf-8").strip() if tok_file.exists() else "(生成中)"
            _p(f"✅ atbrains-server 已在后台启动 (pid {p.pid})")
            _p(f"   控制台   http://127.0.0.1:{port}/")
            _p(f"   口令     {tok}")
            _p(f"   日志     {logfile}")
            return 0
        _p(f"❌ 启动失败，查看 {logfile}")
        return 1
    from .http import main as http_main
    os.environ.update(env)
    http_main()
    return 0


def cmd_stop(args) -> int:
    pidfile = _pidfile(args.config_home)
    if not pidfile.exists():
        _p("服务端未在后台运行（无 pidfile）")
        return 0
    pid = pidfile.read_text().strip()
    try:
        os.kill(int(pid), signal.SIGTERM)
        time.sleep(1)
        _p(f"✅ 已停止服务端 (pid {pid})")
    except ProcessLookupError:
        _p(f"进程 {pid} 已不存在，清理 pidfile")
    except Exception as e:  # noqa: BLE001
        _p(f"❌ 停止失败: {e}")
        return 1
    pidfile.unlink(missing_ok=True)
    return 0


def cmd_open(args) -> int:
    http_cfg = resolve_server_http_config(args.config_home)
    port = int(http_cfg["port"])
    tok_file = Path(http_cfg["data_dir"]) / ".token"
    tok = tok_file.read_text(encoding="utf-8").strip() if tok_file.exists() else "(首次启动时生成)"
    _p(f"网页控制台: http://127.0.0.1:{port}/")
    _p(f"口令:       {tok}  (在页面右上角或 X-Auth-Token 头填入，勿写入 URL)")
    return 0


def cmd_token_list(args) -> int:
    ts = _tokens(args.config_home)
    entries = ts.entries()
    _p(f"Memory Service 令牌表 (权限 600): {ts.path}")
    _p("─" * 58)
    if not entries:
        _p("  (空) 用 `atbrains-server token-add <machine|agent>` 生成")
        return 0
    for name in sorted(entries):
        ent = entries[name]
        _p(f"  {name:<24} [{ent.get('role','agent'):<5}] {mask(ent.get('token',''))}")
    return 0


def cmd_token_add(args) -> int:
    ts = _tokens(args.config_home)
    existed = args.machine in ts.entries()
    tok = ts.add(args.machine, role=args.role or "")
    _p(f"{'🔄 已轮换' if existed else '✅ 已生成'} {args.machine} 的令牌（只显示一次）:")
    _p(f"   {tok}")
    _p("   用法: Authorization: Bearer <令牌>")
    return 0


def cmd_token_rm(args) -> int:
    ts = _tokens(args.config_home)
    if not ts.remove(args.machine):
        _p(f"❌ {args.machine} 没有令牌")
        return 1
    _p(f"✅ 已删除 {args.machine} 的令牌")
    return 0


def _http_json(url: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception:  # noqa: BLE001
        return 0, {}


def cmd_service(args) -> int:
    http_cfg = resolve_server_http_config(args.config_home)
    port = int(http_cfg["port"])
    ts = _tokens(args.config_home)
    code, health = _http_json(f"http://127.0.0.1:{port}/v1/health")
    _p(f"🔌 atbrains-server v{__version__} (protocol v{PROTOCOL_VERSION})")
    _p("─" * 58)
    _p(f"  端口     {port}")
    _p(f"  令牌表   {ts.path} ({len(ts.entries())} 个)")
    if code == 200:
        _p(f"  健康     ✅ HTTP 200 {json.dumps(health, ensure_ascii=False)}")
    else:
        _p(f"  健康     ❌ HTTP {code or '连接被拒'}")
    try:
        ping = _store(args.config_home).ping()
        _p(f"  存储     Redis {_ok(ping.get('redis'))} · MariaDB {_ok(ping.get('mariadb'))}")
    except Exception as e:  # noqa: BLE001
        _p(f"  存储     ❌ {e}")
        return 1
    return 0


def cmd_drain(args) -> int:
    force = bool(getattr(args, "force", False))
    r = _store(args.config_home).drain_vectors(force=force)
    _p(json.dumps(r, ensure_ascii=False, indent=2))
    return 0


def cmd_doctor(args) -> int:
    r = _store(args.config_home).doctor()
    _p(json.dumps(r, ensure_ascii=False, indent=2, default=str))
    return 0 if r.get("ok") else 1


def cmd_memories(args) -> int:
    active = "all" if args.all else "1"
    d = _store(args.config_home).list_memories(limit=args.limit, active=active, q=args.q or "")
    _p(f"共 {d['total']} 条,显示 {len(d['items'])} 条 (active={active})")
    for m in d["items"]:
        flag = "●" if m.get("active") else "○"
        _p(f" {flag} [{m['id']:>4}] {m.get('kind',''):<10} @{m.get('agent','')} "
           f"重要度{m.get('importance','?')} {_short(m.get('content',''), 90)}")
    return 0


def cmd_turns(args) -> int:
    d = _store(args.config_home).list_turns(
        limit=args.limit, offset=getattr(args, "offset", 0),
        machine=getattr(args, "machine", "") or "", q=getattr(args, "q", "") or "")
    _p(f"共 {d['total']} 轮,显示 {len(d['items'])} 轮")
    for t in d["items"]:
        _p(f" [{t['id']:>5}] {_ts(t.get('ts'))} {t.get('machine') or '-':<16} {_short(t.get('u',''), 70)}")
    return 0


def cmd_sessions(args) -> int:
    d = _store(args.config_home).list_sessions(limit=args.limit, machine=getattr(args, "machine", "") or "")
    _p(f"共 {d['total']} 个会话,显示 {len(d['items'])} 个")
    for s in d["items"]:
        _p(f" [{_short(s.get('session_id',''),32):<32}] {s.get('machine') or '-':<16} "
           f"{s.get('platform') or '-':<10} {s.get('turn_count','?'):>4} 轮  最近 {_ts(s.get('last_active'))}")
    return 0


def cmd_search(args) -> int:
    q = " ".join(args.query)
    st = _store(args.config_home)
    try:
        mems = st.search_memories_hybrid(q, limit=args.limit)
    except Exception:  # noqa: BLE001
        mems = st.search_memories(q, limit=args.limit)
    turns = st.search_turns(q, limit=min(args.limit, 10))
    _p(f"🔍 「{q}」→ 记忆 {len(mems)} 条 / 对话 {len(turns)} 条")
    for i, m in enumerate(mems, 1):
        _p(f" {i}. [{m['id']}] ({m.get('agent','')}) {_short(m.get('content') or '', 110)}")
    return 0


def cmd_remember(args) -> int:
    r = _store(args.config_home).remember(
        args.content, kind=args.kind, tags=args.tags or "", importance=args.importance)
    _p(f"✅ 已保存: {r}")
    return 0


def cmd_forget(args) -> int:
    r = _store(args.config_home).forget(args.id)
    _p(f"✅ 已停用 {args.id}: {r}")
    return 0


def cmd_restore(args) -> int:
    r = _store(args.config_home).restore(args.id)
    _p(f"✅ 已恢复 {args.id}: {r}")
    return 0


def _mcp_clients_mod():
    try:
        from atbrains_client import mcp_clients
        return mcp_clients
    except ImportError:
        sdk_root = Path(__file__).resolve().parents[2] / "sdk" / "python"
        if sdk_root.is_dir() and str(sdk_root) not in sys.path:
            sys.path.insert(0, str(sdk_root))
        from atbrains_client import mcp_clients  # type: ignore
        return mcp_clients


def _mcp_spec(args):
    mcp_clients = _mcp_clients_mod()
    agent = (getattr(args, "agent", None) or os.environ.get("ATBRAINS_AGENT") or "stdio").strip()
    if getattr(args, "transport", "stdio") == "http":
        http_cfg = resolve_server_http_config(args.config_home)
        token = getattr(args, "token", None) or _tokens(args.config_home).load().get(agent, "")
        url = getattr(args, "url", None) or f"http://127.0.0.1:{http_cfg['port']}/mcp"
        return mcp_clients.ServerSpec(transport="http", url=url, token=token)
    script = str(Path(__file__).resolve().parent / "mcp_stdio.py")
    return mcp_clients.ServerSpec(command=sys.executable, args=[script, "--agent", agent])


def cmd_mcp_serve(args) -> int:
    from .mcp_stdio import serve
    agent = (args.agent or os.environ.get("ATBRAINS_AGENT") or "stdio").strip()
    return serve(agent, args.config_home)


def cmd_mcp_print(args) -> int:
    mcp_clients = _mcp_clients_mod()
    _p(mcp_clients.snippet(args.client, _mcp_spec(args)))
    return 0


def cmd_mcp_install(args) -> int:
    mcp_clients = _mcp_clients_mod()
    ok, msg = mcp_clients.install(args.client, _mcp_spec(args), write=args.write)
    _p(("✅ " if ok else "❌ ") + msg)
    return 0 if ok else 1


def cmd_mcp_doctor(args) -> int:
    from .mcp_stdio import selftest
    mcp_clients = _mcp_clients_mod()
    rc = selftest((args.agent or "stdio").strip(), args.config_home)
    for label, state in mcp_clients.status():
        _p(f"  {label:<16} {state}")
    return rc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="atbrains-server", description="atbrains 通用记忆服务端")
    parser.add_argument("--config-home", default=None, help="配置目录（默认自动探测）")
    parser.add_argument("--version", action="version", version=f"atbrains-server {__version__} (protocol {PROTOCOL_VERSION})")
    sub = parser.add_subparsers(dest="cmd", metavar="<命令>", required=False)

    sub.add_parser("status", help="一屏总览").set_defaults(func=cmd_status)
    sub.add_parser("migrate", help="显式执行数据库建表与迁移").set_defaults(func=cmd_migrate)

    p = sub.add_parser("serve", help="启动 HTTP /v1 + /mcp + 控制台")
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--background", action="store_true")
    p.set_defaults(func=cmd_serve)

    sub.add_parser("stop", help="停止后台服务端").set_defaults(func=cmd_stop)
    sub.add_parser("open", help="打印控制台地址与口令").set_defaults(func=cmd_open)
    sub.add_parser("token-list", help="列出令牌").set_defaults(func=cmd_token_list)

    p = sub.add_parser("token-add", help="生成令牌（建议 `<machine>|<agent>`）")
    p.add_argument("machine", help="身份名（如 laptop|codex、qq|astrbot、server）")
    p.add_argument("--role", choices=("agent", "admin"), default="", help="权限角色")
    p.set_defaults(func=cmd_token_add)

    p = sub.add_parser("token-rm", help="删除令牌")
    p.add_argument("machine")
    p.set_defaults(func=cmd_token_rm)

    sub.add_parser("service", help="服务自检").set_defaults(func=cmd_service)

    p = sub.add_parser("drain", help="向量回填")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_drain)

    sub.add_parser("doctor", help="系统体检").set_defaults(func=cmd_doctor)

    p = sub.add_parser("memories", help="列出记忆")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--all", action="store_true")
    p.add_argument("--q", default=None)
    p.set_defaults(func=cmd_memories)

    p = sub.add_parser("turns", help="最近对话归档")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--machine", default="")
    p.add_argument("--q", default="")
    p.set_defaults(func=cmd_turns)

    p = sub.add_parser("sessions", help="会话索引")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--machine", default="")
    p.set_defaults(func=cmd_sessions)

    p = sub.add_parser("search", help="混合检索")
    p.add_argument("query", nargs="+")
    p.add_argument("--limit", type=int, default=8)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("remember", help="写入一条记忆")
    p.add_argument("content")
    p.add_argument("--kind", default="note")
    p.add_argument("--tags", default="")
    p.add_argument("--importance", type=int, default=5)
    p.set_defaults(func=cmd_remember)

    p = sub.add_parser("forget", help="停用记忆")
    p.add_argument("id", type=int)
    p.set_defaults(func=cmd_forget)

    p = sub.add_parser("restore", help="恢复记忆")
    p.add_argument("id", type=int)
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("mcp", help="MCP 服务端与客户端接入")
    msub = p.add_subparsers(dest="mcp_cmd", required=False)
    ms = msub.add_parser("serve", help="stdio MCP")
    ms.add_argument("--agent", default=None)
    ms.set_defaults(func=cmd_mcp_serve)

    mi = msub.add_parser("install", help="写入 MCP 客户端配置")
    mi.add_argument("client", help="claude-code | claude-desktop | cursor | codex | generic")
    mi.add_argument("--agent", default=None)
    mi.add_argument("--transport", default="stdio", choices=("stdio", "http"))
    mi.add_argument("--url", default=None)
    mi.add_argument("--token", default=None)
    mi.add_argument("--write", action="store_true")
    mi.set_defaults(func=cmd_mcp_install)

    mp = msub.add_parser("print", help="打印 MCP 配置片段")
    mp.add_argument("client")
    mp.add_argument("--agent", default=None)
    mp.add_argument("--transport", default="stdio", choices=("stdio", "http"))
    mp.add_argument("--url", default=None)
    mp.add_argument("--token", default=None)
    mp.set_defaults(func=cmd_mcp_print)

    md = msub.add_parser("doctor", help="MCP 自检")
    md.add_argument("--agent", default=None)
    md.set_defaults(func=cmd_mcp_doctor)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    fn = getattr(args, "func", None)
    if fn is None:
        return cmd_status(args)
    return int(fn(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
