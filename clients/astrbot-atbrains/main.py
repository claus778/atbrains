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

自检：python main.py --selftest（只读，不改数据）
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import sys
import time
from pathlib import Path
from typing import Any, Optional

_PLUGIN_DIR = Path(__file__).resolve().parent
_VENDOR = _PLUGIN_DIR / "vendor"
if _VENDOR.is_dir() and str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))

from astrbot.api import logger  # noqa: E402
from astrbot.api.event import AstrMessageEvent, filter  # noqa: E402
from astrbot.api.provider import ProviderRequest  # noqa: E402
from astrbot.api.star import Context, Star  # noqa: E402

try:
    import pymysql  # type: ignore
    import redis as _redis_lib  # type: ignore

    _DEPS_OK = True
    _DEPS_ERR = ""
except Exception as _exc:  # pragma: no cover
    pymysql = None  # type: ignore
    _redis_lib = None  # type: ignore
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
    }


# ---------------------------------------------------------------- 存储层

class Store:
    """atbrains 双存储（MariaDB 长期 + Redis 短期）的最小读写封装。"""

    def __init__(self) -> None:
        self.cfg = _load_config()
        self.machine = str(self.cfg["machine"])
        self._conn = None
        self._rconn = None

    # ----- 连接

    def _mysql(self):
        if not _DEPS_OK:
            raise RuntimeError(f"vendor 依赖不可用：{_DEPS_ERR}")
        if self._conn is not None:
            try:
                self._conn.ping()
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
            )
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
        try:
            conn = self._mysql()
            with conn.cursor(pymysql.cursors.DictCursor) as cur:
                cur.execute(sql, params)
                return list(cur.fetchall())
        except pymysql.err.OperationalError:
            # 连接可能已断（网络抖动）：丢弃后重建再试一次
            self._conn = None
            conn = self._mysql()
            with conn.cursor(pymysql.cursors.DictCursor) as cur:
                cur.execute(sql, params)
                return list(cur.fetchall())

    def _write(self, sql: str, params: tuple = ()) -> tuple[int, int]:
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

    # ----- 检索

    def _search_where(self, columns: tuple[str, ...], terms: list[str]) -> tuple[str, list]:
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
        mems = self.search_memories(query, limit=PREFETCH_MEMORIES)
        turns = self.search_turns(query, limit=PREFETCH_TURNS)
        text = self._compose(mems, turns)
        return text, len(mems) + len(turns)

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
        turn_id, _ = self._write(
            "INSERT INTO brain_turns (session_id, platform, author, machine, truncated, "
            "user_content, assistant_content) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (session_id, platform, author, self.machine, truncated, u, a),
        )
        self._write(
            "INSERT INTO brain_sessions (session_id, platform, machine, started_at, last_active, turn_count) "
            "VALUES (%s, %s, %s, NOW(), NOW(), 1) "
            "ON DUPLICATE KEY UPDATE last_active = NOW(), turn_count = turn_count + 1, "
            "platform = VALUES(platform), machine = VALUES(machine)",
            (session_id, platform, self.machine),
        )
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
            turn_id = await asyncio.to_thread(
                self.store.archive_turn,
                user_text, reply_text, event.unified_msg_origin, platform, author,
            )
            logger.info(f"[atbrains] 已归档回合 #{turn_id}（{platform} · {event.unified_msg_origin}）")
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
