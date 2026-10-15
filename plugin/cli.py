"""`hermes atbrains` 命令行 —— 记忆系统的监控与控制。

    hermes atbrains status              一屏总览(三层 + 容器 + 向量覆盖 + 定时器)
    hermes atbrains serve               前台启动网页控制台(打印地址与口令)
    hermes atbrains serve --background  后台启动(写 pidfile,`atbrains stop` 停)
    hermes atbrains stop                停掉后台控制台
    hermes atbrains open                打印控制台地址与口令(便于 SSH 隧道)
    hermes atbrains drain [--force]     手动向量回填(增量;--force 全量重嵌)
    hermes atbrains doctor              系统体检
    hermes atbrains memories [--all] [--q 关键词] [--limit N]
    hermes atbrains turns [--limit N] [--machine M] [--q 关键词]   最近对话归档
    hermes atbrains sessions [--limit N] [--machine M]            会话索引
    hermes atbrains search 关键词       混合检索(向量 + 关键词)
    hermes atbrains remember 内容 [--kind fact] [--tags a,b] [--importance 8]
    hermes atbrains forget ID           停用一条记忆(软删除)
    hermes atbrains restore ID          恢复一条被停用的记忆(与 forget 对偶)
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import probe

HERE = Path(__file__).resolve().parent
CONSOLE_DIR = HERE / "console"
PIDFILE = Path("/tmp/atbrains-console.pid")


def _p(msg: str = "") -> None:
    print(msg, flush=True)


def _ok(cond) -> str:
    return "✅" if cond else "❌"


def _short(s: str, n: int) -> str:
    s = (s or "").replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def _ts(v) -> str:
    """DB 返回的 ts 是 datetime(不可直接切片);统一转成 16 字符时间戳。"""
    return str(v)[:16] if v is not None else ""


# ───────────────────────────── status ─────────────────────────────

def cmd_status(args) -> int:
    try:
        d = probe.overview()
    except Exception as e:
        _p(f"❌ 无法读取记忆系统:{e}")
        return 1
    st = d.get("stats", {}).get("mysql", {}) if isinstance(d.get("stats"), dict) else {}
    rd = d.get("stats", {}).get("redis", {}) if isinstance(d.get("stats"), dict) else {}
    q = d.get("qdrant", {})
    cov = q.get("coverage", {})
    tm = d.get("timer", {})
    v = d.get("vector", {}) if isinstance(d.get("vector"), dict) else {}

    _p(f"🧠 atbrains 记忆系统 · {d.get('ts')}")
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
    _p("─" * 58)
    for c in d.get("containers", []):
        _p(f"  {_ok(c['up'])} 容器 {c['name']:<9} {c['status'] or '未运行':<28} 重启 {c['restarts']} 次")
    if not d.get("containers"):
        _p("  ❌ 未发现 redis/mariadb/qdrant 容器")
    _p("─" * 58)
    active = tm.get("ActiveState") == "active"
    _p(f"  {_ok(active)} 定时器 brain-vector-drain  {tm.get('ActiveState','?')}/{tm.get('UnitFileState','?')}")
    _p(f"     上次运行 {tm.get('lastRun') or '?'} · 退出码 {tm.get('lastExit') or '?'}")
    return 0


# ───────────────────────────── serve ─────────────────────────────

def _console_url_port() -> int:
    import json
    try:
        return int(json.loads((CONSOLE_DIR / "config.json").read_text())["port"])
    except Exception:
        return 8787


def _console_token() -> str:
    f = CONSOLE_DIR / ".token"
    try:
        return f.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return "(首次启动时生成)"


def cmd_serve(args) -> int:
    server = CONSOLE_DIR / "server.py"
    if not server.exists():
        _p(f"❌ 控制台源码缺失:{server}")
        return 1
    port = args.port or _console_url_port()
    env = {**os.environ, "HERMES_HOME": probe.hermes_home(), "ATBRAINS_PORT": str(port)}
    if args.background:
        if PIDFILE.exists():
            pid = PIDFILE.read_text().strip()
            if pid and Path(f"/proc/{pid}").exists():
                _p(f"⚠️ 控制台已在跑(pid {pid})。先 `hermes atbrains stop`")
                return 1
        log = open("/tmp/atbrains-console.log", "ab")
        p = subprocess.Popen([sys.executable, str(server)], cwd=str(CONSOLE_DIR),
                             env=env, stdout=log, stderr=log, start_new_session=True)
        PIDFILE.write_text(str(p.pid))
        time.sleep(3)
        if Path(f"/proc/{p.pid}").exists():
            _p(f"✅ 控制台已在后台启动(pid {p.pid})")
            _p(f"   本机地址 http://127.0.0.1:{port}/")
            _p(f"   口令     {_console_token()}")
            _p(f"   日志     /tmp/atbrains-console.log")
            _p(f"   SSH 隧道 ssh -p 7622 -L {port}:127.0.0.1:{port} root@<服务器>")
        else:
            _p(f"❌ 启动失败,看 /tmp/atbrains-console.log")
            return 1
        return 0
    _p(f"▶ 控制台启动中 http://0.0.0.0:{port}/   (Ctrl-C 停止)")
    _p(f"  口令 {_console_token()}")
    try:
        return subprocess.call([sys.executable, str(server)], cwd=str(CONSOLE_DIR), env=env)
    except KeyboardInterrupt:
        return 0


def cmd_stop(args) -> int:
    if not PIDFILE.exists():
        _p("控制台没在后台跑(无 pidfile)")
        return 0
    pid = PIDFILE.read_text().strip()
    try:
        os.kill(int(pid), signal.SIGTERM)
        time.sleep(1)
        _p(f"✅ 已停止控制台(pid {pid})")
    except ProcessLookupError:
        _p(f"进程 {pid} 已不存在,清理 pidfile")
    except Exception as e:
        _p(f"❌ 停止失败:{e}")
        return 1
    PIDFILE.unlink(missing_ok=True)
    return 0


def cmd_open(args) -> int:
    port = _console_url_port()
    _p("网页控制台(按需启动,不是系统服务):")
    _p(f"  1) hermes atbrains serve --background")
    _p(f"  2) SSH 隧道: ssh -p 7622 -L {port}:127.0.0.1:{port} root@<服务器>")
    _p(f"  3) 浏览器打开 http://127.0.0.1:{port}/")
    _p(f"  口令:{_console_token()}")
    _p(f"  免输入直连(打开即登录): http://127.0.0.1:{port}/?token={_console_token()}")
    _p(f"  停止:hermes atbrains stop")
    return 0


# ───────────────────────────── 控制 ─────────────────────────────

def cmd_drain(args) -> int:
    force = bool(getattr(args, "force", False))
    _p("⏳ 向量回填中(全量重嵌)…" if force else "⏳ 向量回填中(增量,只补缺失)…")
    r = probe.drain(force=force)
    _p(r.get("out") or r.get("err") or "(无输出)")
    _p("✅ 完成" if r.get("ok") else f"❌ 失败(退出码 {r.get('code')})")
    return 0 if r.get("ok") else 1


def cmd_doctor(args) -> int:
    r = probe.doctor()
    _p(r.get("out") or "")
    if r.get("err"):
        _p(r["err"])
    return 0 if r.get("ok") else 1


def cmd_memories(args) -> int:
    active = "all" if args.all else "1"
    d = probe.memories(limit=args.limit, active=active, q=args.q or "")
    _p(f"共 {d['total']} 条,显示 {len(d['items'])} 条(active={active})")
    _p("─" * 70)
    for m in d["items"]:
        flag = "●" if m.get("active") else "○"
        _p(f" {flag} [{m['id']:>4}] {m.get('kind',''):<10} 重要度{m.get('importance','?')} "
           f"{(m.get('tags') or ''):<18} {_short(m.get('content',''), 90)}")
    return 0


def cmd_turns(args) -> int:
    d = probe.turns(limit=args.limit, offset=getattr(args, "offset", 0),
                    machine=getattr(args, "machine", "") or "", q=getattr(args, "q", "") or "")
    _p(f"共 {d['total']} 轮,显示 {len(d['items'])} 轮")
    _p("─" * 70)
    for t in d["items"]:
        _p(f" [{t['id']:>5}] {_ts(t.get('ts'))} {t.get('machine') or '-':<16} {_short(t.get('u',''), 70)}")
    return 0


def cmd_sessions(args) -> int:
    d = probe.sessions(limit=args.limit, machine=getattr(args, "machine", "") or "")
    _p(f"共 {d['total']} 个会话,显示 {len(d['items'])} 个")
    _p("─" * 70)
    for s in d["items"]:
        _p(f" [{_short(s.get('session_id',''),32):<32}] {s.get('machine') or '-':<16} "
           f"{s.get('platform') or '-':<10} {s.get('turn_count','?'):>4} 轮  最近 {_ts(s.get('last_active'))}")
    return 0


def cmd_search(args) -> int:
    q = " ".join(args.query)
    d = probe.search(q, limit=args.limit)
    _p(f"🔍 「{q}」(混合检索:向量 + 关键词)")
    _p(f"── 命中记忆 {len(d['memories'])} 条 " + "─" * 40)
    for i, m in enumerate(d["memories"], 1):
        score = f" score={m['score']:.3f}" if isinstance(m.get("score"), (int, float)) else ""
        _p(f" {i}. {_short(m.get('content') or m.get('preview') or '', 110)}{score}")
    _p(f"── 命中历史对话 {len(d['turns'])} 条 " + "─" * 36)
    for t in d["turns"]:
        _p(f" · {_ts(t.get('ts'))} {_short(t.get('user_content') or '', 90)}")
    return 0


def cmd_remember(args) -> int:
    r = probe.remember(args.content, kind=args.kind, tags=args.tags or "", importance=args.importance)
    _p(f"✅ 已保存:{r}")
    return 0


def cmd_forget(args) -> int:
    r = probe.forget(args.id)
    _p(f"✅ 已停用 {args.id}:{r}")
    return 0


def cmd_restore(args) -> int:
    try:
        r = probe.restore(args.id)
    except Exception as e:  # noqa: BLE001 —— 例如同内容已有活跃条目
        _p(f"❌ 恢复失败:{e}")
        return 1
    _p(f"✅ 已恢复 {args.id}:{r}")
    return 0


# ───────────────────────────── 注册 ─────────────────────────────

def setup_parser(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="atbrains_cmd", metavar="<命令>", required=False)

    p = sub.add_parser("status", help="一屏总览(默认)")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("serve", help="启动网页控制台")
    p.add_argument("--port", type=int, default=None, help="监听端口(默认取 config.json)")
    p.add_argument("--background", action="store_true", help="后台运行(写 pidfile)")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("stop", help="停掉后台控制台")
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("open", help="打印控制台地址与口令")
    p.set_defaults(func=cmd_open)

    p = sub.add_parser("drain", help="手动向量回填")
    p.add_argument("--force", action="store_true", help="全量重嵌(默认增量)")
    p.set_defaults(func=cmd_drain)

    p = sub.add_parser("doctor", help="系统体检")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("memories", help="列出记忆")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--all", action="store_true", help="含已停用")
    p.add_argument("--q", default=None, help="按内容过滤")
    p.set_defaults(func=cmd_memories)

    p = sub.add_parser("turns", help="最近对话归档")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--offset", type=int, default=0, help="分页偏移")
    p.add_argument("--machine", default="", help="只看某来源机")
    p.add_argument("--q", default="", help="按内容过滤(用户/助手)")
    p.set_defaults(func=cmd_turns)

    p = sub.add_parser("sessions", help="会话索引")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--machine", default="", help="只看某来源机")
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

    p = sub.add_parser("forget", help="停用一条记忆")
    p.add_argument("id", type=int)
    p.set_defaults(func=cmd_forget)

    p = sub.add_parser("restore", help="恢复一条被停用的记忆")
    p.add_argument("id", type=int)
    p.set_defaults(func=cmd_restore)


def handle(args) -> int:
    fn = getattr(args, "func", None)
    if fn is None:
        return cmd_status(args)
    return fn(args)
