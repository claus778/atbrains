"""atbrains-memory — 把 AstrBot 接入 atbrains 长期记忆库。

让 QQ 机器人与 Hermes / dsh 等 agent 共享同一套 atbrains 记忆（服务器 MariaDB+Redis）：

1) 自动召回：每条消息触发的 LLM 请求前，按关键词检索长期记忆与历史对话片段，
   拼接注入 system prompt（不改变既有回复逻辑，失败静默降级）。
2) 记忆工具：memory_save / memory_search 两个 function-calling 工具，
   用户说"记住…"或机器人判断值得长期保留时落库；也可在对话中主动检索。
3) 自动归档：每轮问答结束后写入 brain_turns / brain_sessions，并追加到 Redis
   brain:recent 对话流（Hermes 的"最近发生了什么"能看到 QQ 里的对话）。

配置来源（与 Hermes 侧 atbrains-memory 插件共用，不额外维护）：
    /root/.hermes/atbrains.json   —— host/port/db/machine 等非密配置
    /root/.hermes/.env            —— ATBRAINS_MARIADB_PASSWORD / ATBRAINS_REDIS_PASSWORD
                                     （兼容旧名 BRAIN_MARIADB_PASSWORD / BRAIN_REDIS_PASSWORD）
    环境变量 ATBRAINS_HERMES_HOME 可覆盖配置目录（默认 /root/.hermes）。

本插件对记忆库只做「读记忆/写记忆/写回合」三类操作，不写转录层、不做向量化
（向量回填由 Hermes 侧 drainer 负责）。

自检：python main.py --selftest（只读，不改数据）。
没有 AstrBot 的机器（装前预检 / 排障）也能跑 —— 模块顶部对 astrbot 的四个导入是软导入，
失败时退化为占位实现并打一条「使用占位实现」警告；在 AstrBot 进程内始终用真模块。
"""
from __future__ import annotations

import asyncio
import collections
import json
import os
import re
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

_PLUGIN_DIR = Path(__file__).resolve().parent
_VENDOR = _PLUGIN_DIR / "vendor"
if _VENDOR.is_dir() and str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))

try:  # AstrBot 进程内用真模块；只有 import 真的失败才启用下面的占位实现
    from astrbot.api import logger  # noqa: E402
    from astrbot.api.event import AstrMessageEvent, filter  # noqa: E402
    from astrbot.api.provider import ProviderRequest  # noqa: E402
    from astrbot.api.star import Context, Star  # noqa: E402
except ImportError:  # 不在 AstrBot 进程里（`--selftest` / 装前预检）：占位实现兜底
    class _FallbackLogger:
        """最小 logger：`.debug/.info/.warning/.error` 打到 stdout，带 `[atbrains]` 前缀。"""

        def _emit(self, msg: Any) -> None:
            text = str(msg)
            print(text if text.startswith("[atbrains]") else f"[atbrains] {text}", flush=True)

        def debug(self, msg: Any, *args: Any, **kwargs: Any) -> None:
            self._emit(msg)

        def info(self, msg: Any, *args: Any, **kwargs: Any) -> None:
            self._emit(msg)

        def warning(self, msg: Any, *args: Any, **kwargs: Any) -> None:
            self._emit(msg)

        def error(self, msg: Any, *args: Any, **kwargs: Any) -> None:
            self._emit(msg)

    class _FallbackFilter:
        """占位 filter：`on_llm_request()/on_agent_done()/llm_tool(...)` 都原样返回函数。"""

        @staticmethod
        def _identity(*args: Any, **kwargs: Any) -> Any:
            return lambda fn: fn

        on_llm_request = _identity
        on_agent_done = _identity
        llm_tool = _identity

    class _Placeholder:
        """占位基类：AstrMessageEvent / ProviderRequest / Context 只作类型标注用。"""

    class _FallbackStar:
        """占位 Star：`Star(context=None)` 可用，子类只做构造（自检不实例化插件）。"""

        def __init__(self, context: Any = None) -> None:
            self.context = context

    logger = _FallbackLogger()
    filter = _FallbackFilter()
    AstrMessageEvent = ProviderRequest = Context = _Placeholder
    Star = _FallbackStar
    logger.warning("[atbrains] 未检测到 AstrBot 运行环境，使用占位实现（仅 --selftest 可用）")

try:
    import pymysql  # type: ignore
    import redis as _redis_lib  # type: ignore
    from pymysql.constants import CLIENT  # type: ignore  # 归档多语句批用

    _DEPS_OK = True
    _DEPS_ERR = ""
except Exception as _exc:  # pragma: no cover
    pymysql = None  # type: ignore
    _redis_lib = None  # type: ignore
    CLIENT = None  # type: ignore
    _DEPS_OK = False
    _DEPS_ERR = str(_exc)

# ---------------------------------------------------------------- 常量

AGENT_NAME = "astrbot"          # 写入方身份（brain_memories.agent / hot 镜像）
HERMES_HOME = Path(os.environ.get("ATBRAINS_HERMES_HOME") or "/root/.hermes")
DEFAULT_KEY_PREFIX = "brain:"
PREFETCH_MEMORIES = 5           # 每轮召回长期记忆条数（对齐 Hermes 默认）
PREFETCH_TURNS = 3              # 每轮召回历史对话条数
PREFETCH_MAX_CHARS = 1200       # 注入文本上限
TURNS_WINDOW_DAYS = 120         # 历史对话检索时间窗
HOT_TTL_DAYS = 30               # Redis 热点镜像 TTL
STREAM_MAXLEN = 2000            # brain:recent 流长度
TURN_MAX_CHARS = 16000          # brain_turns 正文截断（与 Hermes 侧一致）
CLIP_MEM = 240
CLIP_TURN = 150

_CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]{2,}")
_LATIN_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z_\-.]{1,}")
_CJK_STOPWORDS = {
    "什么", "怎么", "如何", "可以", "我的", "你是", "这个", "那个", "一下", "已经",
    "现在", "今天", "明天", "帮我", "请问", "是否", "以及", "但是", "因为", "所以",
    "然后", "如果", "就是", "还是", "只是", "不是", "没有", "一个", "我们", "他们",
    "需要", "应该", "可能", "这里", "那里", "时候", "问题", "东西", "知道", "觉得",
    "看看", "好的", "谢谢", "麻烦", "想要", "能够", "为了", "关于", "对于", "这么",
}
_LAYER_BY_KIND = {
    "builtin-memory": "profile",
    "fact": "knowledge",
    "decision": "knowledge",
    "insight": "knowledge",
    "preference": "knowledge",
}


def _memory_type(kind: str) -> str:
    k = str(kind or "").strip()
    return _LAYER_BY_KIND.get(k, "episodic")


def search_terms(query: str, max_terms: int = 10) -> list[str]:
    """把自然语言查询拆成 LIKE 检索词：拉丁词 + 中文双字组。"""
    q = (query or "").strip()
    if not q:
        return []
    terms: list[str] = []
    for m in _LATIN_RE.finditer(q):
        terms.append(m.group(0))
    for m in _CJK_RUN_RE.finditer(q):
        run = m.group(0)
        if len(run) <= 4:
            terms.append(run)
        else:
            terms.extend(run[i:i + 2] for i in range(len(run) - 1))
    out: list[str] = []
    for t in terms:
        t = t.strip()
        if len(t) < 2 or t in _CJK_STOPWORDS or t in out:
            continue
        out.append(t)
        if len(out) >= max_terms:
            break
    return out


def _esc_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# 关键词检索走单次 REGEXP 扫描（替代 terms×columns 个 OR-LIKE）：全不命中时
# 不再随词数线性变慢。MariaDB 的 REGEXP 走 PCRE，查询词里的元字符必须逐个转义，
# 否则 `.` `*` 之类会退化成通配符（与 LIKE 不再等价）。
_REGEXP_META_RE = re.compile(r"([\\^$.\[\]|()?*+{}])")


def _esc_regexp(term: str) -> str:
    """转义 PCRE 元字符（\\ ^ $ . [ ] | ( ) ? * + { }）；中文与普通字符原样保留。"""
    return _REGEXP_META_RE.sub(r"\\\1", str(term or ""))


def _regexp_pattern(terms: list[str]) -> str:
    """把检索词拼成单次 REGEXP 的 OR 模式；空词丢弃，无有效词返回 ""（调用方回退旧 SQL）。"""
    pats = []
    for t in terms or []:
        pat = _esc_regexp(str(t or "").strip())
        if pat:
            pats.append(pat)
    return "|".join(pats)


def _regexp_target(columns: tuple[str, ...]) -> str:
    """单列直接用列名；多列先 CONCAT_WS 成一串再整体匹配（NULL 由 COALESCE 兜住）。

    与「逐列 LIKE」同一语义：任一词出现在任一列即命中。词里不含空格，而 CONCAT_WS
    用空格做分隔，因此不会出现跨列拼接产生的假命中。
    """
    cols = tuple(str(c) for c in columns)
    if len(cols) == 1:
        return cols[0]
    return "CONCAT_WS(' ', " + ", ".join(f"COALESCE({c}, '')" for c in cols) + ")"


def _cfg_flag(cfg: dict[str, Any], key: str, default: int = 1) -> bool:
    """int 型开关：缺省 / None / 非法值一律回落到 default。"""
    try:
        return int(cfg.get(key, default)) != 0
    except (TypeError, ValueError):
        return bool(default)


def _clip(text: str, limit: int) -> str:
    text = (text or "").replace("\r", " ").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


# ---------------------------------------------------------------- 配置加载

def _dotenv_get(path: Path, key: str) -> str:
    """解析 Hermes 风格 .env 单键：引号内原样保留；未加引号时空白后的 # 起注释。"""
    try:
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() != key:
                continue
            v = v.strip()
            if len(v) >= 2 and v[0] in ("'", '"') and v[-1] == v[0]:
                return v[1:-1]
            m = re.search(r"\s+#", v)
            if m:
                v = v[:m.start()]
            return v.strip()
    except Exception:
        pass
    return ""


def _load_config() -> dict[str, Any]:
    raw: dict[str, Any] = {}
    # 旧 brain.json 在前、新 atbrains.json 在后：同名键以新文件为准
    for name in ("brain.json", "atbrains.json"):
        p = HERMES_HOME / name
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    raw.update(data)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[atbrains] 读取 {p} 失败：{exc}")
    env_file = HERMES_HOME / ".env"

    def _cred(new: str, old: str) -> str:
        return (
            os.environ.get(new)
            or _dotenv_get(env_file, new)
            or os.environ.get(old)
            or _dotenv_get(env_file, old)
        )

    def _int(key: str, default: int) -> int:
        try:
            return int(raw.get(key, default))
        except (TypeError, ValueError):
            return default

    key_prefix = str(raw.get("redis_key_prefix") or "").strip().strip(":") or DEFAULT_KEY_PREFIX.rstrip(":")
    return {
        "redis_host": str(raw.get("redis_host") or "127.0.0.1"),
        "redis_port": _int("redis_port", 6379),
        "redis_db": _int("redis_db", 0),
        "redis_password": _cred("ATBRAINS_REDIS_PASSWORD", "BRAIN_REDIS_PASSWORD")
        or str(raw.get("redis_password") or ""),
        "mariadb_host": str(raw.get("mariadb_host") or "127.0.0.1"),
        "mariadb_port": _int("mariadb_port", 3306),
        "mariadb_user": str(raw.get("mariadb_user") or "agent"),
        "mariadb_db": str(raw.get("mariadb_db") or "agent_memory"),
        "mariadb_password": _cred("ATBRAINS_MARIADB_PASSWORD", "BRAIN_MARIADB_PASSWORD")
        or str(raw.get("mariadb_password") or ""),
        "machine": str(raw.get("machine") or socket.gethostname())[:48],
        "key_prefix": key_prefix,
        # ----- 往返/时延相关开关（跨公网直连时每轮成本 ≈ 往返次数 × RTT）
        # 每条语句前的 conn.ping() 节流窗口（秒）；0 = 每条语句都 ping（旧行为）
        "mysql_ping_interval": _int("mysql_ping_interval", 30),
        # 轮首召回合并成单条 UNION ALL（1 条语句）；0 = 记忆/对话各一条 SELECT（旧行为）
        "recall_union_enabled": _int("recall_union_enabled", 1),
        # 轮尾归档的两条 INSERT 合并成一次往返（多语句批）；0 = 两条独立语句（旧行为）
        "archive_multi_stmt": _int("archive_multi_stmt", 1),
        # 轮首召回的进程内缓存；0 = 每次召回都上网络（旧行为）
        "recall_cache_enabled": _int("recall_cache_enabled", 1),
        "recall_cache_ttl": _int("recall_cache_ttl", 300),   # 缓存 TTL（秒）；0 = 视为关闭
        "recall_cache_max": _int("recall_cache_max", 256),   # LRU 容量上限（条）
        # 轮首召回的硬预算（毫秒）：超时跳过注入并记 warn；0 = 不设超时（旧行为）
        "recall_timeout_ms": _int("recall_timeout_ms", 900),
        # 归档不挡回合收尾（后台 task）；0 = on_agent_done 里同步 await（旧行为）
        "archive_async": _int("archive_async", 1),
        # 关键词检索走单次 REGEXP；0 = 逐字节回到旧 SQL（OR-LIKE 展开）
        "search_regexp_enabled": _int("search_regexp_enabled", 1),
    }


# ---------------------------------------------------------------- 存储层

class Store:
    """atbrains 双存储（MariaDB 长期 + Redis 短期）的最小读写封装。"""

    def __init__(self) -> None:
        self.cfg = _load_config()
        self.machine = str(self.cfg["machine"])
        self._conn = None
        self._rconn = None
        self._pinged_at = 0.0                     # 上次真正 ping 成功的时刻（monotonic）
        # 同一条连接不允许多线程并发用（背景归档 + 下一条消息的召回会同时发生）
        self._lock = threading.RLock()
        # 召回缓存：(query 规范化, 条数, 时间窗) -> (monotonic, (text, count))
        self._cache: "collections.OrderedDict[tuple, tuple[float, tuple[str, int]]]" = collections.OrderedDict()
        self._cache_lock = threading.Lock()

    # ----- 连接

    def _mysql(self):
        """取（必要时重建）MariaDB 连接。

        内部方法：并发调用必须持 `self._lock`（`_query` / `_write` / `_write_many` 已持）。
        """
        if not _DEPS_OK:
            raise RuntimeError(f"vendor 依赖不可用：{_DEPS_ERR}")
        if self._conn is not None:
            interval = float(self.cfg.get("mysql_ping_interval") or 0)
            if interval <= 0 or (time.monotonic() - self._pinged_at) >= interval:
                # 只在窗口外 ping：窗口内直接复用，断连由 _query/_write 的
                # OperationalError 重试兜底（旧行为是每条语句都 ping = 多一次往返）
                try:
                    self._conn.ping()
                    self._pinged_at = time.monotonic()
                except Exception:  # noqa: BLE001
                    try:
                        self._conn.close()
                    except Exception:  # noqa: BLE001
                        pass
                    self._conn = None
        if self._conn is None:
            self._conn = pymysql.connect(
                host=self.cfg["mariadb_host"],
                port=self.cfg["mariadb_port"],
                user=self.cfg["mariadb_user"],
                password=self.cfg["mariadb_password"],
                database=self.cfg["mariadb_db"],
                charset="utf8mb4",
                autocommit=True,
                connect_timeout=8,
                read_timeout=20,
                write_timeout=20,
                # 归档把两条 INSERT 合成一次往返（CLIENT.MULTI_STATEMENTS）；本插件
                # 只发常量 SQL + 参数化取值，不开任何用户可控的多语句入口。
                client_flag=CLIENT.MULTI_STATEMENTS if _cfg_flag(self.cfg, "archive_multi_stmt", 1) else 0,
            )
            self._pinged_at = time.monotonic()
        return self._conn

    def _r(self):
        if not _DEPS_OK:
            raise RuntimeError(f"vendor 依赖不可用：{_DEPS_ERR}")
        if self._rconn is None:
            self._rconn = _redis_lib.Redis(
                host=self.cfg["redis_host"],
                port=self.cfg["redis_port"],
                db=self.cfg["redis_db"],
                password=self.cfg["redis_password"] or None,
                socket_timeout=6,
                decode_responses=True,
                health_check_interval=30,
            )
        return self._rconn

    def _rk(self, *parts: str) -> str:
        return self.cfg["key_prefix"] + ":" + ":".join(str(p) for p in parts)

    def _query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            try:
                conn = self._mysql()
                with conn.cursor(pymysql.cursors.DictCursor) as cur:
                    cur.execute(sql, params)
                    return list(cur.fetchall())
            except pymysql.err.OperationalError:
                # 连接可能已断（网络抖动 / ping 节流窗口内的陈旧连接）：丢弃后重建再试一次
                self._conn = None
                conn = self._mysql()
                with conn.cursor(pymysql.cursors.DictCursor) as cur:
                    cur.execute(sql, params)
                    return list(cur.fetchall())

    def _write(self, sql: str, params: tuple = ()) -> tuple[int, int]:
        with self._lock:
            try:
                conn = self._mysql()
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    return int(cur.lastrowid or 0), int(cur.rowcount or 0)
            except pymysql.err.OperationalError:
                self._conn = None
                conn = self._mysql()
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    return int(cur.lastrowid or 0), int(cur.rowcount or 0)

    def _write_many(self, statements: list[tuple[str, tuple]]) -> int:
        """一次往返执行多条写语句（需建连时带 CLIENT.MULTI_STATEMENTS）。

        pymysql 只读第一条结果（`Cursor.execute` 会在下次 execute 前排空剩余结果，
        见 vendor/pymysql/cursors.py 的 `while self.nextset(): pass`），因此返回
        **第一条语句**的 lastrowid —— 归档把 brain_turns 的 INSERT 放在第一条。
        OperationalError 与单条写一样：丢弃连接重试一次。
        """
        sql = "; ".join(s for s, _ in statements)
        params: list = []
        for _, p in statements:
            params.extend(p)
        bound = tuple(params)

        def _run() -> int:
            conn = self._mysql()
            with conn.cursor() as cur:
                cur.execute(sql, bound)
                return int(cur.lastrowid or 0)

        with self._lock:
            try:
                return _run()
            except pymysql.err.OperationalError:
                self._conn = None
                return _run()

    # ----- 检索

    def _search_where(self, columns: tuple[str, ...], terms: list[str]) -> tuple[str, list]:
        if _cfg_flag(self.cfg, "search_regexp_enabled", 1) and terms and all(terms):
            pattern = _regexp_pattern(terms)
            # 一次 REGEXP 扫完整列集合；无有效词时回退下面的旧 SQL（行为完全不变）
            if pattern:
                return f"{_regexp_target(columns)} REGEXP %s", [pattern]
        clauses, params = [], []
        for t in terms:
            sub = " OR ".join(f"{c} LIKE %s" for c in columns)
            clauses.append(f"({sub})")
            params.extend([f"%{_esc_like(t)}%"] * len(columns))
        return " OR ".join(clauses), params

    def search_memories(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        terms = search_terms(query)
        if not terms:
            return []
        where, params = self._search_where(("content", "tags"), terms)
        sql = (
            "SELECT id, kind, memory_type, content, tags, importance, source, session_id, machine, agent, "
            "created_at, updated_at "
            f"FROM brain_memories WHERE active = 1 AND superseded_by IS NULL AND ({where}) "
            "ORDER BY importance DESC, updated_at DESC, id DESC LIMIT %s"
        )
        params.append(int(limit))
        return self._query(sql, tuple(params))

    def search_turns(self, query: str, *, limit: int = 3) -> list[dict[str, Any]]:
        terms = search_terms(query)
        if not terms:
            return []
        where, params = self._search_where(("user_content", "assistant_content"), terms)
        sql = (
            "SELECT id, session_id, platform, machine, user_content, assistant_content, ts "
            f"FROM brain_turns WHERE ({where}) "
        )
        if TURNS_WINDOW_DAYS > 0:
            sql += "AND ts >= (NOW() - INTERVAL %s DAY) "
            params.append(TURNS_WINDOW_DAYS)
        sql += "ORDER BY ts DESC, id DESC LIMIT %s"
        params.append(int(limit))
        return self._query(sql, tuple(params))

    def search_all(self, query: str, limit: int = 5) -> tuple[list[dict], list[dict]]:
        mems = self.search_memories(query, limit=limit)
        turns = self.search_turns(query, limit=max(1, limit // 2))
        return mems, turns

    def search_both(self, query: str, *, mem_limit: int = PREFETCH_MEMORIES,
                    turn_limit: int = PREFETCH_TURNS) -> tuple[list[dict], list[dict]]:
        """一条 SQL 同时取记忆与对话（轮首召回用）。

        两个分支各自 ORDER BY + LIMIT（MySQL/MariaDB 要求 UNION 分支的排序用括号包住），
        外层 `ORDER BY src, importance DESC, ts DESC, id DESC` 把两段拼成与
        `search_memories` / `search_turns` **逐行一致**的顺序：
        mem 段的 importance/ts 就是记忆的排序键，turn 段 importance 恒为 0（等于只按 ts/id）。
        """
        terms = search_terms(query)
        if not terms:
            return [], []
        mem_where, mem_params = self._search_where(("content", "tags"), terms)
        turn_where, turn_params = self._search_where(("user_content", "assistant_content"), terms)
        sql = (
            "SELECT * FROM ("
            "(SELECT 'mem' AS src, id, kind, memory_type, content, tags, importance, machine, "
            "session_id, '' AS platform, updated_at AS ts, '' AS user_content, '' AS assistant_content "
            "FROM brain_memories "
            f"WHERE active = 1 AND superseded_by IS NULL AND ({mem_where}) "
            "ORDER BY importance DESC, updated_at DESC, id DESC LIMIT %s) "
            "UNION ALL "
            "(SELECT 'turn' AS src, id, '' AS kind, '' AS memory_type, '' AS content, '' AS tags, "
            "0 AS importance, machine, session_id, platform, ts, user_content, assistant_content "
            f"FROM brain_turns WHERE ({turn_where}) "
            + ("AND ts >= (NOW() - INTERVAL %s DAY) " if TURNS_WINDOW_DAYS > 0 else "")
            + "ORDER BY ts DESC, id DESC LIMIT %s) "
            ") u ORDER BY src, importance DESC, ts DESC, id DESC"
        )
        params: list = list(mem_params) + [int(mem_limit)] + list(turn_params)
        if TURNS_WINDOW_DAYS > 0:
            params.append(TURNS_WINDOW_DAYS)
        params.append(int(turn_limit))
        rows = self._query(sql, tuple(params))
        mems = [r for r in rows if r.get("src") == "mem"]
        turns = [r for r in rows if r.get("src") == "turn"]
        return mems, turns

    # ----- 召回进程内缓存（只缓存 prefetch_block 的结果）

    def _cache_key(self, query: str) -> Optional[tuple]:
        """缓存键 = 规范化 query + 影响召回口径的维度（条数/时间窗）。

        维度进 key：改了预取条数或时间窗后，旧口径的结果不会被复用。
        """
        if not _cfg_flag(self.cfg, "recall_cache_enabled", 1):
            return None
        return (" ".join(str(query).split()), PREFETCH_MEMORIES, PREFETCH_TURNS, TURNS_WINDOW_DAYS)

    def _cache_get(self, key: tuple) -> Optional[tuple[str, int]]:
        ttl = int(self.cfg.get("recall_cache_ttl") or 0)
        if ttl <= 0:
            return None
        now = time.monotonic()
        with self._cache_lock:
            item = self._cache.get(key)
            if item is None:
                return None
            stamp, value = item
            if now - stamp > ttl:
                self._cache.pop(key, None)
                return None
            self._cache.move_to_end(key)
            return value

    def _cache_put(self, key: tuple, value: tuple[str, int]) -> None:
        if int(self.cfg.get("recall_cache_ttl") or 0) <= 0:
            return
        cap = max(1, int(self.cfg.get("recall_cache_max") or 1))
        with self._cache_lock:
            self._cache[key] = (time.monotonic(), value)
            self._cache.move_to_end(key)
            while len(self._cache) > cap:
                self._cache.popitem(last=False)

    def cache_clear(self) -> None:
        """清空召回缓存（remember 写入后调用，避免新记忆在 TTL 内看不到）。"""
        with self._cache_lock:
            self._cache.clear()

    def _compose(self, mems: list[dict[str, Any]], turns: list[dict[str, Any]]) -> str:
        blocks: list[str] = []
        if mems:
            lines = ["[atbrains 长期记忆 · MariaDB]"]
            for m in mems:
                line = f"- [{m['kind']}·重要度{m['importance']}] {_clip(str(m['content']), CLIP_MEM)}"
                if m.get("tags"):
                    line += f" (tags: {m['tags']})"
                if m.get("machine"):
                    line += f" [{m['machine']}]"
                lines.append(line)
            blocks.append("\n".join(lines))
        if turns:
            lines = ["[atbrains 相关历史对话 · 归档]"]
            for t in turns:
                ts = t.get("ts")
                ts_s = ts.strftime("%Y-%m-%d") if hasattr(ts, "strftime") else str(ts)[:16]
                snippet = _clip(str(t.get("user_content") or "").replace("\n", " "), CLIP_TURN)
                if not snippet:
                    continue
                mch = str(t.get("machine") or "")
                tag = f" [{mch}]" if mch else ""
                lines.append(f"- {ts_s}{tag}: {snippet}")
            blocks.append("\n".join(lines))
        text = "\n".join(b for b in blocks if len(b.splitlines()) > 1)
        if len(text) > PREFETCH_MAX_CHARS:
            text = text[:PREFETCH_MAX_CHARS].rstrip() + " …"
        return text

    def prefetch_block(self, query: str) -> tuple[str, int]:
        query = (query or "").strip()
        if not query:
            return "", 0
        key = self._cache_key(query)
        if key is not None:
            hit = self._cache_get(key)
            if hit is not None:
                return hit
        if _cfg_flag(self.cfg, "recall_union_enabled", 1):
            mems, turns = self.search_both(query)
        else:
            mems = self.search_memories(query, limit=PREFETCH_MEMORIES)
            turns = self.search_turns(query, limit=PREFETCH_TURNS)
        out = (self._compose(mems, turns), len(mems) + len(turns))
        if key is not None:
            self._cache_put(key, out)
        return out

    # ----- 写入

    def remember(self, content: str, *, kind: str = "note", tags: str = "",
                 importance: int = 5, session_id: str = "", source: str = AGENT_NAME) -> dict[str, Any]:
        content = str(content or "").strip()
        if not content:
            raise ValueError("content 不能为空")
        try:
            importance = max(1, min(10, int(importance)))
        except (TypeError, ValueError):
            importance = 5
        kind = (str(kind or "note").strip() or "note")[:32]
        mtype = _memory_type(kind)
        tags = str(tags or "").strip()[:255]
        mem_id = 0
        deduped = False
        for attempt in (0, 1):
            try:
                mem_id, _ = self._write(
                    "INSERT INTO brain_memories (kind, memory_type, content, tags, importance, "
                    "source, session_id, machine, agent) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (kind, mtype, content, tags, importance, str(source or "")[:64],
                     str(session_id or "")[:96], self.machine, AGENT_NAME),
                )
                break
            except Exception as exc:  # noqa: BLE001
                if attempt or getattr(exc, "args", [None])[0] != 1062:
                    raise
                rows = self._query(
                    "SELECT id FROM brain_memories WHERE active = 1 AND content_hash = MD5(%s) LIMIT 1",
                    (content,),
                )
                if not rows:
                    continue
                mem_id = int(rows[0]["id"])
                deduped = True
                self._write(
                    "UPDATE brain_memories SET importance = GREATEST(importance, %s), "
                    "memory_type = IF(memory_type = '', %s, memory_type), "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = %s AND active = 1",
                    (importance, mtype, mem_id),
                )
                break
        hot_ok = False
        # 新记忆立刻对召回可见：清掉进程内召回缓存（否则最多 TTL 秒内看不到）
        self.cache_clear()
        try:
            r = self._r()
            ttl = HOT_TTL_DAYS * 86400
            payload = json.dumps({
                "id": mem_id, "kind": kind, "content": _clip(content, 1000), "tags": tags,
                "importance": importance, "source": source, "machine": self.machine,
                "agent": AGENT_NAME, "ts": int(time.time()),
            }, ensure_ascii=False)
            p = r.pipeline()
            p.set(self._rk("hot", mem_id), payload, ex=ttl)
            p.zadd(self._rk("hot", "index"), {str(mem_id): time.time()})
            p.zremrangebyrank(self._rk("hot", "index"), 0, -501)
            p.expire(self._rk("hot", "index"), ttl)
            p.execute()
            hot_ok = True
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[atbrains] Redis 热点镜像失败：{exc}")
        return {"id": mem_id, "kind": kind, "importance": importance,
                "memory_type": mtype, "hot_cache": hot_ok, "deduped": deduped}

    def archive_turn(self, user_content: str, assistant_content: str,
                     session_id: str, platform: str, author: str) -> int:
        u_raw, a_raw = str(user_content or ""), str(assistant_content or "")
        u, a = u_raw[:TURN_MAX_CHARS], a_raw[:TURN_MAX_CHARS]
        truncated = 1 if (len(u_raw) > TURN_MAX_CHARS or len(a_raw) > TURN_MAX_CHARS) else 0
        session_id = str(session_id or "")[:96]
        platform = str(platform or "")[:48]
        author = str(author or "")[:64]
        turn_stmt = (
            "INSERT INTO brain_turns (session_id, platform, author, machine, truncated, "
            "user_content, assistant_content) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (session_id, platform, author, self.machine, truncated, u, a),
        )
        sess_stmt = (
            "INSERT INTO brain_sessions (session_id, platform, machine, started_at, last_active, turn_count) "
            "VALUES (%s, %s, %s, NOW(), NOW(), 1) "
            "ON DUPLICATE KEY UPDATE last_active = NOW(), turn_count = turn_count + 1, "
            "platform = VALUES(platform), machine = VALUES(machine)",
            (session_id, platform, self.machine),
        )
        if _cfg_flag(self.cfg, "archive_multi_stmt", 1):
            # 两条 INSERT 合成一次往返（brain_turns 在第一条 → lastrowid 即回合 id）。
            # 取多语句批而非单语句方案的理由：两张表共用一次 INSERT 做不到，
            # 而改写 brain_sessions 的口径（如只靠触发器）会改库侧行为。
            turn_id = self._write_many([turn_stmt, sess_stmt])
        else:
            turn_id, _ = self._write(*turn_stmt)
            self._write(*sess_stmt)
        try:
            self._r().xadd(
                self._rk("recent"),
                {"sid": session_id, "platform": platform, "author": author,
                 "machine": self.machine, "user": u[:600], "assistant": a[:600],
                 "ts": f"{time.time():.3f}"},
                maxlen=STREAM_MAXLEN, approximate=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[atbrains] Redis 对话流写入失败：{exc}")
        return turn_id


def _normalize_platform(name: str) -> str:
    n = (name or "").strip().lower()
    if n == "aiocqhttp":
        return "qq"
    return n or "unknown"


# 后台归档任务：持引用防止 create_task 的返回值被 GC 提前回收（回调里自摘）
_BG_TASKS: "set[asyncio.Task]" = set()


# ---------------------------------------------------------------- 插件

class AtbrainsMemory(Star):
    """atbrains 长期记忆接入。

    命令：
        /plugin atbrains-memory   —— 查看本帮助
    """

    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self.store = Store()
        if not _DEPS_OK:
            logger.error(f"[atbrains] 插件依赖加载失败，将只做日志降级：{_DEPS_ERR}")
        else:
            logger.info(
                f"[atbrains] 已就绪：machine={self.store.machine} "
                f"mariadb={self.store.cfg['mariadb_host']}:{self.store.cfg['mariadb_port']} "
                f"redis={self.store.cfg['redis_host']}:{self.store.cfg['redis_port']}"
                f"/db{self.store.cfg['redis_db']} agent={AGENT_NAME}"
            )

    # ----- 自动召回

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        try:
            if event.get_extra("atbrains_injected"):
                return
            event.set_extra("atbrains_injected", True)
            query = (event.message_str or "").strip()
            if len(query) < 2:
                return
            timeout_ms = int(self.store.cfg.get("recall_timeout_ms") or 0)
            if timeout_ms > 0:
                # 硬预算：公网抖动不能把首 token 拖到秒级（超时跳过注入，本轮照常回复）
                try:
                    block, count = await asyncio.wait_for(
                        asyncio.to_thread(self.store.prefetch_block, query),
                        timeout=timeout_ms / 1000.0,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        f"[atbrains] 召回超过 {timeout_ms} ms 预算，本轮跳过注入"
                        f"（{event.unified_msg_origin}）"
                    )
                    return
            else:
                block, count = await asyncio.to_thread(self.store.prefetch_block, query)
            if not block:
                return
            header = (
                "\n\n[atbrains 长期记忆 · 自动召回：以下内容提取自与用户的历史会话与长期记忆，"
                "回答时可自然参考，不必逐字复述，也不要主动声称“我查了数据库”]\n"
            )
            req.system_prompt = f"{req.system_prompt or ''}{header}{block}"
            logger.info(f"[atbrains] 已注入召回 {count} 条（{event.unified_msg_origin}）")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[atbrains] 召回注入失败（已跳过）：{exc}")

    # ----- 每轮归档

    @filter.on_agent_done()
    async def on_agent_done(self, event: AstrMessageEvent, run_context, response) -> None:
        try:
            user_text = (event.message_str or "").strip()
            try:
                reply_text = str(getattr(response, "completion_text", "") or "").strip()
            except Exception:  # noqa: BLE001
                reply_text = ""
            if not user_text and not reply_text:
                return
            platform = _normalize_platform(event.get_platform_name())
            author = (event.get_sender_name() or event.get_sender_id() or "")[:64]
            session_id = event.unified_msg_origin
            if _cfg_flag(self.store.cfg, "archive_async", 1):
                # 后台跑：不挡回合收尾（MariaDB 1 次往返 + Redis XADD 1 次）
                task = asyncio.create_task(
                    self._archive_bg(user_text, reply_text, session_id, platform, author)
                )
                _BG_TASKS.add(task)
                task.add_done_callback(_BG_TASKS.discard)
                return
            turn_id = await asyncio.to_thread(
                self.store.archive_turn,
                user_text, reply_text, session_id, platform, author,
            )
            logger.info(f"[atbrains] 已归档回合 #{turn_id}（{platform} · {session_id}）")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[atbrains] 回合归档失败（已跳过）：{exc}")

    async def _archive_bg(self, user_text: str, reply_text: str, session_id: str,
                          platform: str, author: str) -> None:
        """后台归档：异常只记日志（create_task 的异常不会被自动取走）。"""
        try:
            turn_id = await asyncio.to_thread(
                self.store.archive_turn, user_text, reply_text, session_id, platform, author,
            )
            logger.info(f"[atbrains] 已归档回合 #{turn_id}（{platform} · {session_id}）")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[atbrains] 回合归档失败（已跳过）：{exc}")

    # ----- 记忆工具

    @filter.llm_tool(name="memory_save")
    async def memory_save(self, event: AstrMessageEvent, content: str,
                          kind: str = "note", tags: str = "", importance: int = 5) -> str:
        """保存一条长期记忆到 atbrains 记忆库（跨会话、与用户的其他 AI 助手共享）。当用户明确要求记住某事（如“记住…”“记一下…”），或对话中出现值得长期保留的用户事实、偏好、决定、待办时调用。

        Args:
            content(string): 记忆内容，一句自足的话，脱离当前对话也能读懂
            kind(string): 类别，可选 fact / preference / decision / note / todo，默认 note
            tags(string): 逗号分隔的标签，可为空
            importance(number): 重要度 1-10，默认 5
        """
        try:
            result = await asyncio.to_thread(
                self.store.remember, content, kind=kind, tags=tags,
                importance=importance, session_id=event.unified_msg_origin,
                source=AGENT_NAME,
            )
            note = "（该内容已存在，已刷新）" if result.get("deduped") else ""
            return json.dumps(
                {"ok": True, "id": result.get("id"), "kind": result.get("kind"),
                 "importance": result.get("importance"), "note": note},
                ensure_ascii=False,
            )
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"ok": False, "error": str(exc)[:200]}, ensure_ascii=False)

    @filter.llm_tool(name="memory_search")
    async def memory_search(self, event: AstrMessageEvent, query: str, limit: int = 5) -> str:
        """检索 atbrains 长期记忆与历史对话片段。当需要回忆用户的偏好、以往决定、或以前聊过的内容时调用。

        Args:
            query(string): 检索关键词或问题
            limit(number): 返回条数，默认 5
        """
        try:
            mems, turns = await asyncio.to_thread(self.store.search_all, query, int(limit))
            if not mems and not turns:
                return "未找到相关记忆。"
            lines: list[str] = []
            for m in mems:
                line = f"[{m['kind']}·重要度{m['importance']}] {_clip(str(m['content']), 240)}"
                if m.get("tags"):
                    line += f" (tags: {m['tags']})"
                lines.append(line)
            for t in turns:
                ts = t.get("ts")
                ts_s = ts.strftime("%Y-%m-%d") if hasattr(ts, "strftime") else str(ts)[:16]
                snippet = _clip(str(t.get("user_content") or "").replace("\n", " "), 120)
                if snippet:
                    lines.append(f"[对话 {ts_s} · {t.get('platform', '')}] 用户: {snippet}")
            return "\n".join(lines)
        except Exception as exc:  # noqa: BLE001
            return f"检索失败：{exc}"


# ---------------------------------------------------------------- 自检（只读）

def _selftest() -> None:
    print(f"deps ok: {_DEPS_OK} {_DEPS_ERR}")
    st = Store()
    masked = dict(st.cfg)
    for k in ("mariadb_password", "redis_password"):
        v = masked.get(k) or ""
        masked[k] = f"<set:{len(v)}>" if v else "<missing>"
    print("config:", json.dumps(masked, ensure_ascii=False))
    toggles = ("mysql_ping_interval", "recall_union_enabled", "archive_multi_stmt",
               "recall_cache_enabled", "recall_cache_ttl", "recall_cache_max",
               "recall_timeout_ms", "archive_async", "search_regexp_enabled")
    print("toggles:", json.dumps({k: st.cfg.get(k) for k in toggles}, ensure_ascii=False))
    print("mariadb ping:", st._mysql().ping() or "ok")
    print("redis ping:", st._r().ping())
    mems = st.search_memories("AstrBot 部署 记忆", limit=3)
    print(f"search_memories -> {len(mems)} 条")
    for m in mems:
        print("   -", m["id"], m["kind"], _clip(str(m["content"]), 60))
    text, n = st.prefetch_block("AstrBot 长期记忆 接入")
    print(f"prefetch -> {n} 条, {len(text)} 字符")
    print(text[:300])


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
