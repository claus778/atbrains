"""`hermes atbrains-memory` 子命令：运维与手工操作 atbrains 双存储。

命令：status | stats | search | remember | recent | forget | prune | doctor | vector-status | vector-drain | vector-test | supersede-detect | remote-check | unsupersede | distill
仅在 atbrains 是活动记忆提供者时可见（由 Hermes CLI 发现机制门控）。
"""
from __future__ import annotations

import json
import sys

try:  # 作为插件包模块加载时
    from .core import AtbrainsStore, resolve_config
except ImportError:  # 独立执行时兜底
    from core import AtbrainsStore, resolve_config  # type: ignore


def _out(text: str) -> None:
    print(text)


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0
    return f"{n:.1f} GB"


def _store() -> AtbrainsStore:
    return AtbrainsStore(resolve_config())


# ---------------------------------------------------------------- 各子命令

def _cmd_status(args) -> int:
    store = _store()
    cfg = store.cfg
    _out("atbrains 记忆库状态")
    _out("─" * 46)
    _out(f"配置    ~/.hermes/atbrains.json + .env（HERMES_HOME={cfg.get('hermes_home')}）")
    _out(f"Redis   {cfg['redis_host']}:{cfg['redis_port']} (db={cfg.get('redis_db', 0)})"
         f"  短期层：最近对话流 / 会话状态 / 热点镜像 / 检索缓存")
    _out(f"MariaDB {cfg['mariadb_host']}:{cfg['mariadb_port']} db={cfg['mariadb_db']} "
         f"user={cfg['mariadb_user']}  长期层：记忆 / 对话归档 / 会话 / 存档")
    ping = store.ping()
    _out(f"连通性  Redis: {'✓' if ping.get('redis') else '✗ ' + str(ping.get('redis_error', ''))}")
    _out(f"        MariaDB: {'✓' if ping.get('mariadb') else '✗ ' + str(ping.get('mariadb_error', ''))}")
    if ping.get("mariadb") and ping.get("redis"):
        try:
            stats = store.stats()
            m, r = stats["mysql"], stats["redis"]
            _out(f"记忆    {m['memories_active']} 活跃 / {m['memories_total']} 总计")
            _out(f"归档    对话 {m['turns']} 条 · 会话 {m['sessions']} 个 · 压缩存档 {m['archives']} 份")
            _out(f"转录    {m.get('turn_messages', 0)} 条消息 · "
                 f"{_fmt_bytes(m.get('turn_messages_raw_bytes', 0))} 原始（zlib 压缩存）")
            _out(f"知识层  主张 {m.get('claims_active', 0)} 活跃 · 版本 {m.get('claim_versions', 0)} · "
                 f"情节 {m.get('episodes', 0)}")
            _out(f"占用    MariaDB {_fmt_bytes(m['size_bytes'])} · Redis {r.get('keys', '?')} keys"
                 f" · 流 {r.get('recent_stream', '?')} 条 · {r.get('used_memory', '')}")
        except Exception as exc:  # noqa: BLE001
            _out(f"统计读取失败: {exc}")
    return 0


def _cmd_stats(args) -> int:
    store = _store()
    _out(json.dumps(store.stats(), ensure_ascii=False, indent=2))
    return 0


def _cmd_search(args) -> int:
    store = _store()
    query = " ".join(args.query)
    limit = max(1, min(50, args.limit))
    mems = store.search_memories_hybrid(query, limit=limit)
    turns = store.search_turns(query, limit=min(limit, 8))
    _out(f"检索: {query!r}  →  记忆 {len(mems)} 条 / 对话 {len(turns)} 条")
    _out("─" * 46)
    for m in mems:
        ts = m["created_at"].strftime("%Y-%m-%d") if m.get("created_at") else "?"
        tags = f" #{m['tags']}" if m.get("tags") else ""
        mach = f" @{m['machine']}" if m.get("machine") else ""
        via = " ·语义" if m.get("via") in ("vec", "both") else ""
        _out(f"[{m['id']}] {ts} ({m['kind']}·{m['importance']}){tags}{mach}{via}")
        _out(f"    {m['content'][:160]}")
    if turns:
        _out("")
        _out("相关历史对话：")
        for t in turns:
            ts = t["ts"].strftime("%Y-%m-%d %H:%M") if t.get("ts") else "?"
            line = str(t.get("user_content") or "").replace("\n", " ")[:120]
            mach = f" @{t['machine']}" if t.get("machine") else ""
            _out(f"  {ts} [{t['session_id'][:20]}]{mach} {line}")
    return 0


def _cmd_remember(args) -> int:
    store = _store()
    content = " ".join(args.content)
    result = store.remember(content, kind=args.kind, tags=args.tags,
                            importance=args.importance, source="cli")
    # 远程模式的 saved 由服务端返回，字段集合不受本机控制：全部用 get，避免写入成功后 KeyError
    _out(f"已保存: id={result.get('id')} kind={result.get('kind')} "
         f"importance={result.get('importance')} hot={result.get('hot_cache')}")
    if result.get("error"):
        _out(f"写入失败（远程明确失败，未回落）: {result['error']}")
        return 1
    return 0


def _cmd_recent(args) -> int:
    store = _store()
    turns = store.recent_turns(max(1, min(50, args.n)))
    if not turns:
        _out("Redis 最近对话流为空。")
        return 0
    _out(f"最近 {len(turns)} 条对话（Redis brain:recent）")
    _out("─" * 46)
    for t in turns:
        import time as _t
        ts = _t.strftime("%m-%d %H:%M", _t.localtime(t.get("ts") or 0))
        mach = f"@{t['machine']} " if t.get("machine") else ""
        _out(f"{ts} [{t.get('session', '')[:18]}] {mach}{str(t.get('user') or '').replace(chr(10), ' ')[:110]}")
    return 0


def _cmd_forget(args) -> int:
    store = _store()
    result = store.forget(args.id)
    _out(f"记忆 {result['id']} 已废止（active=0）；受影响行数 {result['deactivated']}")
    return 0


def _cmd_prune(args) -> int:
    store = _store()
    result = store.prune(turns_keep_days=args.turns_days,
                         archive_keep_days=args.archive_days,
                         inactive_keep_days=args.inactive_days,
                         sessions_keep_days=args.sessions_days,
                         dry_run=args.dry_run)
    mode = "预演（未删除）" if args.dry_run else "已执行"
    _out(f"prune {mode}: {json.dumps(result, ensure_ascii=False)}")
    return 0


def _cmd_doctor(args) -> int:
    ok = True
    _out("atbrains doctor")
    _out("─" * 46)
    for pkg in ("redis", "pymysql"):
        try:
            __import__(pkg)
            _out(f"依赖 {pkg}: ✓")
        except Exception as exc:  # noqa: BLE001
            ok = False
            _out(f"依赖 {pkg}: ✗ ({exc})")
    cfg = resolve_config()
    for key, var in (("redis_password", "ATBRAINS_REDIS_PASSWORD"),
                     ("mariadb_password", "ATBRAINS_MARIADB_PASSWORD")):
        _out(f"凭据 {var}: {'✓' if cfg.get(key) else '✗ 缺失'}")
        ok = ok and bool(cfg.get(key))
    store = AtbrainsStore(cfg)
    ping = store.ping()
    for name, val in ping.items():
        _out(f"{name}: {val}")
    ok = ok and ping.get("redis") is True and ping.get("mariadb") is True
    try:
        store.ensure_schema()
        _out(f"表结构: ✓ v{store.schema_version()}"
             " (brain_memories / brain_claims / brain_claim_versions / brain_claim_evidence / brain_episodes"
             " / brain_turns / brain_turn_messages / brain_thoughts / brain_tool_calls"
             " / brain_sessions / brain_archive / brain_meta)")
    except Exception as exc:  # noqa: BLE001
        ok = False
        _out(f"表结构: ✗ ({exc})")
    _out("结论: " + ("全部正常 ✓" if ok else "存在问题 ✗"))
    return 0 if ok else 1


def _cmd_vector_status(args) -> int:
    store = _store()
    st = store.vector_status()
    _out("atbrains 向量层状态（语义召回）")
    _out("─" * 46)
    _out(f"开关    {'开启' if st['enabled'] else '关闭（纯关键词模式）'}")
    _out(f"模型    {st['embed_model']}（{st['embed_dim']} 维）")
    _out(f"Qdrant  {st['qdrant']}")
    found = st.get("keys_found", {})
    _out(f"key 来源 {st['key_source']}  embed={found.get('embed')} qdrant={found.get('qdrant')}")
    m = st.get("memories", {})
    _out(f"记忆    {m.get('active', '?')} 活跃 · 已向量化 {m.get('ok', '?')} · "
         f"失败 {m.get('err', '?')} · 缺失 {m.get('missing', '?')} · 过期 {m.get('stale', '?')}")
    if m.get("last_ok"):
        _out(f"最近向量化  {m['last_ok']}")
    qs = st.get("qdrant_state", {})
    if "error" in qs:
        _out(f"Qdrant 读取失败: {qs['error']}")
    else:
        _out(f"集合状态  {qs.get('status')} · 向量点数 {qs.get('points')}")
    return 0


def _cmd_vector_drain(args) -> int:
    store = _store()
    result = store.drain_vectors(max_items=max(0, int(args.max or 0)), force=bool(args.force))
    _out("向量回填/同步: " + json.dumps(result, ensure_ascii=False))
    return 0


def _cmd_vector_test(args) -> int:
    store = _store()
    query = " ".join(args.query)
    limit = max(1, min(20, int(args.limit)))
    _out(f"向量检索直测: {query!r}")
    _out("─" * 46)
    kw = store.search_memories(query, limit=limit)
    _out(f"[关键词] {len(kw)} 条")
    for m in kw:
        _out(f"  [{m['id']}] {str(m['content'])[:110]}")
    try:
        vec = store.vector_search_memories(query, limit=limit)
        _out(f"[向量] {len(vec)} 条")
        for m in vec:
            _out(f"  [{m['id']}] score={m.get('vscore')} {str(m['content'])[:110]}")
    except Exception as exc:  # noqa: BLE001
        _out(f"[向量] 失败: {exc}")
    hyb = store.search_memories_hybrid(query, limit=limit)
    _out(f"[融合] {len(hyb)} 条")
    for m in hyb:
        _out(f"  [{m['id']}] via={m.get('via')} {str(m['content'])[:110]}")
    return 0


def _cmd_supersede_detect(args) -> int:
    store = _store()
    result = store.detect_supersedes(dry_run=not args.apply, threshold=args.threshold)
    mode = "已应用" if args.apply else "预演（未写库）"
    _out(f"取代检测 {mode}：候选对 {len(result['pairs'])} 组 · 已标记 {result['applied']} 条 · "
         f"缺向量跳过 {result['skipped']} 条")
    if result.get("error"):
        _out(f"Qdrant 读取失败：{result['error']}")
        return 1
    _out("─" * 46)
    for p in result["pairs"]:
        _out(f"[{p['id']}] ({p['kind']}/{p['memory_type']}) → 被 [{p['superseded_by']}] "
             f"({p['winner_kind']}) 取代 · 相似度 {p['similarity']}")
        _out(f"    旧: {p['content']}")
        _out(f"    新: {p['winner_content']}")
    if not result["pairs"]:
        _out("（无重叠记忆，或所有活跃记忆都还没有向量）")
    return 0


def _cmd_remote_check(args) -> int:
    store = _store()
    st = store.remote_check()
    if getattr(args, "json", False):
        _out(json.dumps(st, ensure_ascii=False, indent=2))
        return 0 if st.get("ok") or not st["url"] else 1
    _out("atbrains 远程模式自检（Memory Service 客户端）")
    _out("─" * 46)
    _out(f"总开关   {'开启（远程优先，失败回落）' if st['enabled'] else '关闭（本地直连：Redis + MariaDB）'}")
    _out(f"服务端   {st['url'] or '（未配置 remote_url）'}")
    if st.get("token_configured"):
        _out(f"令牌     已配置 {st['token_masked']}（来源：$HERMES_HOME/.env 的 ATBRAINS_REMOTE_TOKEN）")
    else:
        _out("令牌     未配置 → 把令牌写进 $HERMES_HOME/.env：")
        _out("         echo 'ATBRAINS_REMOTE_TOKEN=<令牌>' >> ~/.hermes/.env   # 令牌由服务端签发：hermes atbrains token-add <machine>")
    _out(f"超时     {st['timeout_ms']} ms（关键路径召回）· 失败回落本地："
         f"{'开' if st['fallback'] else '关（失败即返回空）'} · 回合归档走远程：{'开' if st['turn_sync'] else '关'}")
    if not st["url"]:
        _out("健康检查 跳过：在 $HERMES_HOME/atbrains.json 里配 remote_url + remote_enabled=1 后重跑")
        return 0
    if st.get("ok"):
        _out(f"健康检查 GET /v1/health → ok ✓ · 往返 {st['latency_ms']} ms")
        _out(f"         {json.dumps(st.get('health') or {}, ensure_ascii=False)}")
        return 0
    detail = st.get("error") or ("HTTP %s" % st.get("http_status"))
    _out(f"健康检查 ✗ {detail} · 往返 {st['latency_ms']} ms")
    return 1


def _cmd_unsupersede(args) -> int:
    store = _store()
    result = store.unsupersede(args.id)
    if result["cleared"]:
        _out(f"记忆 {result['id']} 的取代标记已清除，重新参与召回。")
    else:
        _out(f"记忆 {result['id']} 没有取代标记（无需处理）。")
    return 0


def _cmd_distill(args) -> int:
    store = _store()
    if not (args.dry_run or args.now):
        st = store.distill_status()
        c = st["counters"]
        _out("atbrains 结论蒸馏（distill）")
        _out("─" * 46)
        _out(f"开关     {'开启' if st['enabled'] else '关闭'}"
             f"（巡检间隔 {st['interval_min']} 分钟）")
        if st["watermark"] is None:
            _out("水位     未初始化（首次启用只记当前最大 id，不回灌历史）")
        else:
            _out(f"水位     {st['watermark']} · 待蒸馏 {st['pending']} 轮")
        _out(f"上次蒸馏 {st['last_distill'] or '从未'}")
        _out(f"计数     runs={c['distill_runs']} added={c['distill_added']} "
             f"updated={c['distill_updated']} skipped={c['distill_skipped']} "
             f"errors={c['distill_errors']}")
        _out(f"API      {st['api_base_url']} · model={st['api_model']} · "
             f"key={'已配置' if st['api_key_present'] else '缺失'}")
        return 0

    turns = max(0, int(args.turns or 0))
    if turns <= 0:
        turns = max(1, int(store.cfg.get("distill_max_turns") or 16))
    # 手动命令只由本轮驱动：进程内关掉自动线程，避免 60 秒后后台线程并发写库/烧 API。
    store.cfg["distill_enabled"] = 0
    dry = bool(args.dry_run)
    _out(f"结论蒸馏 {'预演（dry-run）' if dry else '执行（--now）'}：最近 {turns} 轮")
    _out("─" * 46)
    result = store._distill_tick(force=True, turns_override=turns, dry_run=dry)
    if result.get("reason") == "extract_failed":
        _out(f"提取失败（未推水位）：{result.get('error') or '未知错误'}")
        return 1
    items = result.get("items") or []
    for it in items:
        sim = "  ?  " if it.get("sim") is None else f"{it['sim']:.3f}"
        why = f" · {it['reason']}" if it.get("reason") else ""
        _out(f"[{it['decision']}] sim={sim} ({it['kind']}·{it['importance']}) "
             f"{it['content'][:120]}{why}")
        if it["decision"] == "update" and it.get("target_id"):
            _out(f"    → 取代记忆 [{it['target_id']}]")
        if it.get("id"):
            _out(f"    → 新记忆 id={it['id']}")
    if not items:
        _out("（本批无可提炼结论）")
    _out("─" * 46)
    if dry:
        _out("未写库；水位未动")
    else:
        _out(f"写入新记忆 id: {result.get('added') or []}")
        _out(f"新水位: {result.get('watermark')}")
    return 0


# ---------------------------------------------------------------- 注册

def register_cli(subparser) -> None:
    subs = subparser.add_subparsers(dest="atbrains_memory_command")

    subs.add_parser("status", help="连通性 / 配置 / 统计总览")
    subs.add_parser("stats", help="JSON 格式统计")

    p = subs.add_parser("search", help="检索记忆与历史对话")
    p.add_argument("query", nargs="+", help="检索关键词")
    p.add_argument("--limit", type=int, default=8, help="记忆条数上限（默认 8）")

    p = subs.add_parser("remember", help="保存一条长期记忆")
    p.add_argument("content", nargs="+", help="记忆内容")
    p.add_argument("--kind", default="note", help="fact|preference|decision|note|todo")
    p.add_argument("--tags", default="", help="逗号分隔标签")
    p.add_argument("--importance", type=int, default=6, help="1-10，默认 6")

    p = subs.add_parser("recent", help="查看 Redis 最近对话流")
    p.add_argument("--n", type=int, default=10, help="条数（默认 10）")

    p = subs.add_parser("forget", help="废止一条记忆（软删除）")
    p.add_argument("id", type=int, help="记忆 id")

    p = subs.add_parser("prune", help="按保留期清理归档")
    p.add_argument("--turns-days", type=int, default=None, help="对话保留天数（0=不清理，默认；>0 才启用）")
    p.add_argument("--archive-days", type=int, default=None, help="存档保留天数（默认 365）")
    p.add_argument("--inactive-days", type=int, default=None, help="软删记忆保留天数（默认 365）")
    p.add_argument("--sessions-days", type=int, default=None, help="会话索引保留天数（默认 365）")
    p.add_argument("--dry-run", action="store_true", help="只统计不删除")

    subs.add_parser("doctor", help="自检：依赖 / 凭据 / 连接 / 表结构")

    subs.add_parser("vector-status", help="向量层状态：开关/模型/key 来源/已向量化统计")

    p = subs.add_parser("vector-drain", help="手动回填向量（默认只补缺；--force 全量重嵌）")
    p.add_argument("--max", type=int, default=0, help="最多处理条数（0=不限）")
    p.add_argument("--force", action="store_true", help="忽略索引全量重嵌")

    p = subs.add_parser("vector-test", help="直测：同一查询的关键词/向量/融合对比")
    p.add_argument("query", nargs="+", help="检索查询")
    p.add_argument("--limit", type=int, default=6, help="返回条数")

    p = subs.add_parser("supersede-detect", help="检测语义重叠的记忆并标记取代（默认预演）")
    p.add_argument("--apply", action="store_true", help="真正写库（默认只预演，不修改任何行）")
    p.add_argument("--threshold", type=float, default=0.90, help="余弦相似度阈值（默认 0.90）")

    p = subs.add_parser("remote-check", help="远程模式自检：开关 / URL / 令牌掩码 / 健康检查往返耗时")
    p.add_argument("--json", action="store_true", help="以 JSON 输出（默认人类可读）")

    p = subs.add_parser("unsupersede", help="撤销一条记忆的取代标记（可回滚）")
    p.add_argument("id", type=int, help="记忆 id")

    p = subs.add_parser("distill", help="结论蒸馏：状态 / 预演 / 手动执行")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--status", action="store_true", help="查看开关/水位/计数/API 配置（默认）")
    g.add_argument("--dry-run", action="store_true", help="预演：真实提取+对账，零写入、水位不动")
    g.add_argument("--now", action="store_true", help="手动强制跑一批并真实写入（无视开关）")
    p.add_argument("--turns", type=int, default=0, help="本批轮次数（0=distill_max_turns）")

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
        "prune": _cmd_prune,
        "doctor": _cmd_doctor,
        "vector-status": _cmd_vector_status,
        "vector-drain": _cmd_vector_drain,
        "vector-test": _cmd_vector_test,
        "supersede-detect": _cmd_supersede_detect,
        "remote-check": _cmd_remote_check,
        "unsupersede": _cmd_unsupersede,
        "distill": _cmd_distill,
    }
    handler = handlers.get(sub)
    if handler is None:
        print("用法: hermes atbrains-memory <status|stats|search|remember|recent|forget|prune|doctor|"
              "vector-status|vector-drain|vector-test|supersede-detect|remote-check|unsupersede|"
              "distill>")
        return
    try:
        rc = handler(args)
    except Exception as exc:  # noqa: BLE001
        print(f"atbrains 命令失败: {exc}", file=sys.stderr)
        rc = 1
    if rc:
        raise SystemExit(rc)
