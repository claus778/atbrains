"""brain 核心引擎：Redis（短期）+ MariaDB（长期）双存储。

独立模块：不依赖 Hermes 即可使用（需要 `redis` 和 `pymysql`）。
被记忆提供者（./__init__.py）和 CLI（./cli.py）共用。

分层设计：
  Redis（短期）
    brain:recent        最近对话流（Stream，MAXLEN 截断，TTL 自动过期）
    brain:session:<id>  会话状态哈希（turns/last_active，TTL）
    brain:hot:<id>      热点记忆镜像 + brain:hot:index（ZSET 索引）
    brain:qcache:<h>    检索结果缓存（短期记忆命中，避免重复查库）
  上述短期键名中的 "brain" 段由配置 redis_key_prefix 决定（默认空 = 仍为 "brain"，
  多实例共库时置为如 "brain:server"，键名即变为 brain:server:hot:index 等）。
  MariaDB（长期，库 hermes_agent_memory）
    brain_memories      长期事实/偏好/决策（永久）
    brain_turns         完整对话归档（永久）
    brain_turn_messages 全量转录层：每轮的工具调用/工具输出/推理（zlib JSON，永久）
    brain_sessions      会话索引/计数
    brain_archive       上下文压缩前存档（防止细节丢失）
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import socket
import sys
import threading
import time
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ 依赖引导
#
# redis / PyMySQL 优先使用 Hermes 环境里已安装的版本；缺失时回退到插件内置的
# vendor/ 目录（纯 Python 包，随插件携带，不依赖 pip / PM 构建）。
_VENDOR_DIR = Path(__file__).resolve().parent / "vendor"


def _ensure_deps() -> None:
    try:
        import pymysql  # noqa: F401
        import redis  # noqa: F401
        return
    except ImportError:
        pass
    if _VENDOR_DIR.is_dir() and str(_VENDOR_DIR) not in sys.path:
        sys.path.append(str(_VENDOR_DIR))


_ensure_deps()

# ------------------------------------------------------------------ 配置

DEFAULT_CONFIG: dict[str, Any] = {
    "redis_host": "127.0.0.1",
    "redis_port": 9079,
    "redis_db": 0,
    # 多实例隔离：空串 = 保持 "brain:<后缀>"（现状）；非空 = "<prefix>:<后缀>"，
    # 例："brain:server" → brain:server:hot:index。允许与 redis_db 叠加使用。
    "redis_key_prefix": "",
    "mariadb_host": "127.0.0.1",
    "mariadb_port": 57306,
    "mariadb_user": "agent",
    "mariadb_db": "hermes_agent_memory",
    "machine": "",
    # 行为参数（可在 brain.json 覆盖，不进入 setup 向导）
    "short_term_ttl_days": 14,   # Redis 流/会话 TTL
    "hot_ttl_days": 30,          # 热点镜像 TTL
    "stream_maxlen": 2000,       # 最近对话流最大条数
    "prefetch_max_chars": 1200,  # 每轮注入的召回文本上限
    "prefetch_memories": 5,      # 召回长期记忆条数
    "prefetch_turns": 3,         # 召回历史对话条数
    "search_cache_ttl": 600,     # Redis 检索缓存秒数
    "search_turns_days": 120,    # 召回检索的对话时间窗（天；0=不限。更老的对话仍可用 SQL/审计访问）
    "turns_keep_days": 0,        # prune：对话保留天数（0=不清理，保留全量）
    # 全量转录层（brain_turn_messages）：除正文外，把本轮的工具调用/工具输出/推理也存下来（zlib 压缩）
    "turn_messages_enabled": 1,        # 1 存 / 0 只存正文（退回旧行为）
    "turn_msg_max_bytes": 1048576,     # 单条消息原始 JSON 上限（超出按 content 截断并留 _truncated 标记）
    "turn_total_max_bytes": 16777216,  # 单轮写入原始上限（超出本轮弃掉剩余，下一轮从断点续存）
    "archive_keep_days": 365,    # prune：存档保留天数
    "inactive_keep_days": 365,   # prune：软删记忆保留天数
    "sessions_keep_days": 365,   # prune：会话索引保留天数（无近期轮次时才删）
    # 进程内维护循环（不依赖 systemd）：定期检查，距上次清理超过 prune_every_days 天才真正执行
    "maintenance_enabled": 1,        # 维护循环总开关（1 开 / 0 关）
    "maintenance_interval_min": 60,  # 巡检间隔（分钟）
    "prune_every_days": 7,           # 距上次清理超过该天数才清一次（0=每轮都清）
    # 向量层（语义召回）：默认开启；key 从 qdrant 容器配置读取（不落盘、不新增环境变量）
    "vector_enabled": 1,             # 1 开 / 0 关（关闭后即时退回纯关键词）
    "embed_base_url": "https://api.siliconflow.cn/v1",
    "embed_model": "Qwen/Qwen3-Embedding-4B",
    "embed_dim": 2560,
    "embed_timeout": 60,             # 回填/后台向量化单次请求超时（秒）
    "vector_query_timeout": 20,      # 查询路径 embed 超时上限（超时降级关键词）
    "qdrant_url": "http://127.0.0.1:6333",
    "qdrant_collection": "brain_memories",
    "qdrant_config_file": "/root/qdrant/qdrant-config/production.yaml",  # key 来源：qdrant 配置文件（service.api_key）
    "vector_topk": 12,               # 向量候选条数
    "vector_min_score": 0.25,        # 余弦相似度下限
    "vector_rrf_k": 60,              # RRF 融合常数
    "vector_drain_batch": 8,         # 每批向量化条数
    "vector_drain_interval": 60,     # 后台 drainer 兜底扫描周期（秒）
    "vector_text_max": 12000,        # 单条记忆向量化截取字符数上限
    "vector_qemb_ttl": 3600,         # 查询向量缓存秒数（Redis）
    # 记忆分层与时间衰减（decay_types：逗号分隔的 memory_type 白名单，只衰减这些层）
    "decay_enabled": 1,              # 1 开 / 0 关（关闭后所有记忆等权参与排序）
    "decay_half_life_days": 45,      # 衰减半衰期（天）：factor = 0.5 ** (age_days / 半衰期)
    "decay_types": "episodic",       # profile / knowledge 不衰减
    # rerank（默认关闭；服务端手动开启，失败自动回落 RRF 顺序，绝不阻断检索）
    "rerank_enabled": 0,             # 1 开 / 0 关
    "rerank_url": "https://api.siliconflow.cn/v1/rerank",
    "rerank_model": "BAAI/bge-reranker-v2-m3",
    "rerank_topn": 20,               # 送进 rerank 的候选条数上限
    "rerank_min_score": 0.0,         # 低于该 relevance_score 的候选丢弃
    "rerank_timeout": 20,            # rerank 请求超时（秒）
    # 远程模式（Memory Service 客户端）：mind=remote 时走 HTTP 调服务端；默认全关
    # → remote_enabled=0 时下面这些键一行远程代码都不执行，行为与本地直连逐字节一致。
    "remote_enabled": 0,             # 总开关（1 开 / 0 关）
    "remote_url": "",                # 例 "http://111.230.71.79:8787"（/v1 与 console 共用端口）
    "remote_token": "",              # Bearer 令牌：不写进 brain.json，从 $HERMES_HOME/.env 读 BRAIN_REMOTE_TOKEN
    "remote_timeout_ms": 800,        # 关键路径（每轮召回）超时，必须短：绝不允许无限等待
    "remote_fallback": 1,            # 远程失败是否回落本地（0=明确失败，不静默写本地）
    "remote_turn_sync": 0,           # 对话归档是否也走远程（默认关：本地写）
    # 结论蒸馏（进程内后台线程，默认关）：把对话轮次交给 LLM 提炼成长期结论写回记忆库
    "distill_enabled": 0,               # 1 开 / 0 关（默认关：零行为变化、零 API 花费）
    "distill_interval_min": 60,         # 巡检间隔（分钟）
    "distill_min_turns": 8,             # 攒够多少轮才蒸馏
    "distill_max_turns": 16,            # 单批最多送多少轮
    "distill_turn_chars": 1000,         # user/assistant 各自截取字符数
    "distill_stagnation_hours": 24,     # 不足 min_turns 但最老一条已滞留该小时数 → 也蒸馏
    "distill_max_items": 6,             # 单批最多产出多少条结论
    "distill_min_importance": 4,        # importance 下限（裁剪）
    "distill_max_importance": 7,        # importance 上限（裁剪）
    "distill_low_sim": 0.85,            # 余弦相似度低于该值 → 新结论
    "distill_high_sim": 0.94,           # 余弦相似度达到该值 → 视为重复跳过；两者之间交 LLM 裁决
    "distill_api_base_url": "https://api.commandcode.ai/provider/v1",
    "distill_api_model": "deepseek/deepseek-v4.1-flash",
    "distill_api_timeout": 120,         # 单次 LLM 请求超时（秒）
}

SECRET_ENV_VARS = {
    "redis_password": "BRAIN_REDIS_PASSWORD",
    "mariadb_password": "BRAIN_MARIADB_PASSWORD",
    "distill_api_key": "HERMES_CUSTOM_API_COMMANDCODE_AI_API_KEY",
}

PROVIDER_NAME = "brain"
KEY_PREFIX = "brain:"


def hermes_home_path(hermes_home: Optional[str] = None) -> Path:
    if hermes_home:
        return Path(hermes_home)
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    return Path.home() / ".hermes"


def _dotenv_value(raw: str) -> str:
    """解析 .env 单值：引号内原样保留；未加引号时只有空白后的 `#` 才开始行内注释。

    旧实现只在「首尾都是引号」时才去引号，`KEY="pw"  # 注释` 会把注释当值的一部分，
    导致密码/令牌静默变成错误字符串。
    """
    value = raw.strip()
    if value[:1] in ('"', "'"):
        quote = value[0]
        out: list[str] = []
        i = 1
        while i < len(value):
            ch = value[i]
            if quote == '"' and ch == "\\" and value[i + 1:i + 2] in ('"', "\\"):
                out.append(value[i + 1])
                i += 2
                continue
            if ch == quote:
                return "".join(out)
            out.append(ch)
            i += 1
        return value  # 引号未闭合：保持旧行为（原样返回），不做猜测
    if value.startswith("#"):
        return ""
    m = re.search(r"[ \t]#", value)
    if m:
        value = value[:m.start()].rstrip()
    return value


def _dotenv_get(env_path: Path, key: str) -> Optional[str]:
    """解析 Hermes 写法 .env 子集的单键取值（bare / 'single' / "double"）。"""
    try:
        text = env_path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, sep, value = line.partition("=")
        if not sep or name.strip() != key:
            continue
        # 引号、行内注释、空值统一由 _dotenv_value 处理
        return _dotenv_value(value)
    return None


def resolve_config(hermes_home: Optional[str] = None) -> dict[str, Any]:
    """brian.json（非密） + 环境变量/.env（密码） + 默认值 合并。"""
    home = hermes_home_path(hermes_home)
    cfg: dict[str, Any] = dict(DEFAULT_CONFIG)
    cfg_path = home / "brain.json"
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
        if isinstance(data, dict):
            for k, v in data.items():
                if k in DEFAULT_CONFIG and v is not None:
                    cfg[k] = v
    except FileNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.warning("brain: 忽略无法解析的 %s: %s", cfg_path, exc)

    env_file = home / ".env"
    for key, var in SECRET_ENV_VARS.items():
        val = os.environ.get(var) or _dotenv_get(env_file, var)
        if val:
            cfg[key] = val

    for k, default in DEFAULT_CONFIG.items():
        if isinstance(default, int):
            try:
                cfg[k] = int(cfg[k])
            except (TypeError, ValueError):
                cfg[k] = default

    cfg["hermes_home"] = str(home)
    return cfg


# ------------------------------------------------------------------ 检索词

_CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]{2,}")
_LATIN_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z_\-.]{1,}")

# 常见虚词双字组合，屏蔽以免召回噪声
_CJK_STOPWORDS = {
    "什么", "怎么", "如何", "可以", "我的", "你是", "这个", "那个", "一下", "已经",
    "现在", "今天", "明天", "帮我", "请问", "是否", "以及", "但是", "因为", "所以",
    "然后", "如果", "就是", "还是", "只是", "不是", "没有", "一个", "我们", "他们",
    "需要", "应该", "可能", "这里", "那里", "时候", "问题", "东西", "知道", "觉得",
    "看看", "好的", "谢谢", "麻烦", "想要", "能够", "为了", "关于", "对于", "这么",
}


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


def _mask_secret(value: str) -> str:
    """令牌掩码：最多露前 4 位（短令牌整体隐藏）—— 绝不完整回显、绝不进日志。"""
    v = str(value or "")
    if not v:
        return ""
    return v[:4] + "…" if len(v) > 8 else "…"


# ------------------------------------------------------------------ 记忆分层 / 衰减

_LAYER_BY_KIND = {
    "builtin-memory": "profile",   # 用户画像 / 偏好：长期稳定，不衰减
    "fact": "knowledge",           # 事实：长期有效，不衰减
    "decision": "knowledge",       # 决策：长期有效，不衰减
    "insight": "knowledge",        # 归纳类结论：长期有效，不衰减
    "preference": "knowledge",     # 偏好：长期有效，不衰减
}


def _memory_type(kind: str, explicit: str = "") -> str:
    """记忆分层：显式指定优先，否则按 kind 映射（其余归为 episodic）。"""
    explicit = str(explicit or "").strip()
    if explicit:
        return explicit[:16]
    k = str(kind or "").strip()
    return _LAYER_BY_KIND.get(k, "episodic")


def _age_days(value: Any, now_ts: float) -> Optional[float]:
    """库内 DATETIME（UTC 口径）到当前的天数；拿不到或异常返回 None（调用方保持不衰减）。"""
    if value is None:
        return None
    try:
        if hasattr(value, "timestamp"):
            ts = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
            return max(0.0, (now_ts - ts.timestamp()) / 86400.0)
        if isinstance(value, (int, float)):
            return max(0.0, (now_ts - float(value)) / 86400.0)
        text = str(value).strip().replace("T", " ")
        dt = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return max(0.0, (now_ts - dt.timestamp()) / 86400.0)
    except Exception:  # noqa: BLE001
        return None


def _utc_epoch(value: Any) -> Optional[float]:
    """库内 DATETIME（UTC 口径）→ epoch 秒；naive 必须按 UTC 解释。

    直接对 naive datetime 调 .timestamp() 会按本机时区解释：容器是 UTC 时看不出问题，
    Windows 桌面版（UTC+8）会把所有库内时间整体偏移 8 小时。
    """
    if value is None:
        return None
    try:
        if hasattr(value, "timestamp"):
            ts = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
            return float(ts.timestamp())
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value).strip().replace("T", " ")
        dt = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return float(dt.timestamp())
    except Exception:  # noqa: BLE001
        return None


def _cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度（纯 Python 兜底实现；维度不一致返回 0）。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / ((na ** 0.5) * (nb ** 0.5))


# ------------------------------------------------------------------ 存储引擎

class BrainError(RuntimeError):
    pass


class BrainHTTPError(BrainError):
    """带 HTTP 状态码的错误（用于 401 重读 key / 404 建集合 等分支）。"""

    def __init__(self, code: int, url: str, body: str = ""):
        super().__init__(f"HTTP {code} {url}: {body[:200]}")
        self.code = int(code)
        self.url = url
        self.body = body


_DDL = (
    """CREATE TABLE IF NOT EXISTS brain_memories (
        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        kind VARCHAR(32) NOT NULL DEFAULT 'note',
        content MEDIUMTEXT NOT NULL,
        tags VARCHAR(255) NOT NULL DEFAULT '',
        importance TINYINT NOT NULL DEFAULT 5,
        source VARCHAR(64) NOT NULL DEFAULT '',
        session_id VARCHAR(96) NOT NULL DEFAULT '',
        machine VARCHAR(48) NOT NULL DEFAULT '',
        active TINYINT NOT NULL DEFAULT 1,
        deactivated_at DATETIME NULL DEFAULT NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        content_hash CHAR(32) GENERATED ALWAYS AS (IF(active = 1, MD5(content), NULL)) STORED,
        PRIMARY KEY (id),
        KEY idx_active_updated (active, updated_at),
        KEY idx_kind (kind),
        KEY idx_recall (active, importance, updated_at),
        UNIQUE KEY uniq_active_content (content_hash),
        CONSTRAINT chk_importance CHECK (importance BETWEEN 1 AND 10),
        CONSTRAINT chk_active CHECK (active IN (0, 1))
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
    """CREATE TABLE IF NOT EXISTS brain_turns (
        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        session_id VARCHAR(96) NOT NULL DEFAULT '',
        platform VARCHAR(48) NOT NULL DEFAULT '',
        author VARCHAR(64) NOT NULL DEFAULT '',
        machine VARCHAR(48) NOT NULL DEFAULT '',
        truncated TINYINT NOT NULL DEFAULT 0,
        user_content MEDIUMTEXT NOT NULL,
        assistant_content MEDIUMTEXT NOT NULL,
        ts DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (id),
        KEY idx_ts (ts),
        KEY idx_session_ts (session_id, ts)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
    # 全量转录层：一行一条消息（含 tool 调用/工具输出/推理），body = zlib(JSON)
    """CREATE TABLE IF NOT EXISTS brain_turn_messages (
        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        turn_id BIGINT UNSIGNED NOT NULL,
        seq SMALLINT UNSIGNED NOT NULL DEFAULT 0,
        role VARCHAR(16) NOT NULL DEFAULT '',
        tool_name VARCHAR(64) NOT NULL DEFAULT '',
        tool_call_id VARCHAR(128) NOT NULL DEFAULT '',
        raw_bytes INT UNSIGNED NOT NULL DEFAULT 0,
        body LONGBLOB NOT NULL,
        ts DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (id),
        KEY idx_turn_seq (turn_id, seq),
        KEY idx_tm_ts (ts)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
    """CREATE TABLE IF NOT EXISTS brain_sessions (
        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        session_id VARCHAR(96) NOT NULL,
        platform VARCHAR(48) NOT NULL DEFAULT '',
        machine VARCHAR(48) NOT NULL DEFAULT '',
        started_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        last_active DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        turn_count INT NOT NULL DEFAULT 0,
        archived_messages INT UNSIGNED NOT NULL DEFAULT 0,
        note VARCHAR(512) NOT NULL DEFAULT '',
        PRIMARY KEY (id),
        UNIQUE KEY uniq_session_id (session_id),
        KEY idx_sessions_time (started_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
    """CREATE TABLE IF NOT EXISTS brain_archive (
        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        session_id VARCHAR(96) NOT NULL DEFAULT '',
        machine VARCHAR(48) NOT NULL DEFAULT '',
        platform VARCHAR(48) NOT NULL DEFAULT '',
        kind VARCHAR(24) NOT NULL DEFAULT 'precompress',
        ts DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        payload MEDIUMTEXT NOT NULL,
        PRIMARY KEY (id),
        KEY idx_archive (session_id, ts)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
    """CREATE TABLE IF NOT EXISTS brain_meta (
        meta_key VARCHAR(64) NOT NULL,
        meta_value VARCHAR(255) NOT NULL DEFAULT '',
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        PRIMARY KEY (meta_key)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
    """CREATE TABLE IF NOT EXISTS brain_vector_index (
        mem_id BIGINT UNSIGNED NOT NULL,
        status VARCHAR(16) NOT NULL DEFAULT 'ok',
        model VARCHAR(64) NOT NULL DEFAULT '',
        dim SMALLINT UNSIGNED NOT NULL DEFAULT 0,
        content_hash CHAR(32) NULL DEFAULT NULL,
        error VARCHAR(255) NOT NULL DEFAULT '',
        embedded_at DATETIME NULL DEFAULT NULL,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        PRIMARY KEY (mem_id),
        KEY idx_vec_status (status)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
)

# 模式版本与迁移（幂等；多进程/多机并发用 GET_LOCK 串行化）
SCHEMA_VERSION = 5

_MIGRATIONS: dict[int, tuple[str, tuple[str, ...]]] = {
    1: ("machine 列（多设备归属）", (
        "ALTER TABLE brain_memories ADD COLUMN IF NOT EXISTS machine VARCHAR(48) NOT NULL DEFAULT '' AFTER session_id",
        "ALTER TABLE brain_turns ADD COLUMN IF NOT EXISTS machine VARCHAR(48) NOT NULL DEFAULT '' AFTER author",
        "ALTER TABLE brain_sessions ADD COLUMN IF NOT EXISTS machine VARCHAR(48) NOT NULL DEFAULT '' AFTER platform",
    )),
    2: ("防重唯一键 / 召回索引 / 停用时间 / CHECK / 截断标记 / 存档来源列", (
        "ALTER TABLE brain_memories MODIFY COLUMN content MEDIUMTEXT NOT NULL",
        "ALTER TABLE brain_memories ADD COLUMN IF NOT EXISTS deactivated_at DATETIME NULL DEFAULT NULL AFTER active",
        "ALTER TABLE brain_memories ADD COLUMN IF NOT EXISTS content_hash CHAR(32) "
        "GENERATED ALWAYS AS (IF(active = 1, MD5(content), NULL)) STORED",
        "ALTER TABLE brain_memories ADD UNIQUE KEY IF NOT EXISTS uniq_active_content (content_hash)",
        "ALTER TABLE brain_memories ADD INDEX IF NOT EXISTS idx_recall (active, importance, updated_at)",
        "ALTER TABLE brain_memories ADD CONSTRAINT IF NOT EXISTS chk_importance CHECK (importance BETWEEN 1 AND 10)",
        "ALTER TABLE brain_memories ADD CONSTRAINT IF NOT EXISTS chk_active CHECK (active IN (0, 1))",
        "ALTER TABLE brain_turns ADD COLUMN IF NOT EXISTS truncated TINYINT NOT NULL DEFAULT 0 AFTER machine",
        "ALTER TABLE brain_archive ADD COLUMN IF NOT EXISTS machine VARCHAR(48) NOT NULL DEFAULT '' AFTER session_id",
        "ALTER TABLE brain_archive ADD COLUMN IF NOT EXISTS platform VARCHAR(48) NOT NULL DEFAULT '' AFTER machine",
    )),
    3: ("向量索引表（brain_vector_index）", (
        "CREATE TABLE IF NOT EXISTS brain_vector_index ("
        "mem_id BIGINT UNSIGNED NOT NULL, status VARCHAR(16) NOT NULL DEFAULT 'ok', "
        "model VARCHAR(64) NOT NULL DEFAULT '', dim SMALLINT UNSIGNED NOT NULL DEFAULT 0, "
        "content_hash CHAR(32) NULL DEFAULT NULL, error VARCHAR(255) NOT NULL DEFAULT '', "
        "embedded_at DATETIME NULL DEFAULT NULL, "
        "updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP, "
        "PRIMARY KEY (mem_id), KEY idx_vec_status (status)) "
        "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci",
    )),
    4: ("取代关系与记忆分层", (
        "ALTER TABLE brain_memories ADD COLUMN IF NOT EXISTS superseded_by BIGINT UNSIGNED NULL DEFAULT NULL AFTER active",
        "ALTER TABLE brain_memories ADD COLUMN IF NOT EXISTS superseded_at DATETIME NULL DEFAULT NULL AFTER superseded_by",
        "ALTER TABLE brain_memories ADD COLUMN IF NOT EXISTS memory_type VARCHAR(16) NOT NULL DEFAULT '' AFTER kind",
        "ALTER TABLE brain_memories ADD INDEX IF NOT EXISTS idx_supersede (active, superseded_by)",
        # 回填：老行按 kind 推导分层（与 _memory_type() 的映射一致）。
        # 末尾 updated_at = updated_at 是必须的：不显式写该列会触发它的
        # ON UPDATE CURRENT_TIMESTAMP，把全部记忆的“最后更新/年龄”信号冲成当下。
        "UPDATE brain_memories SET memory_type = CASE "
        "WHEN kind = 'builtin-memory' THEN 'profile' "
        "WHEN kind IN ('fact', 'decision') THEN 'knowledge' "
        "ELSE 'episodic' END, updated_at = updated_at WHERE memory_type = ''",
    )),
    5: ("全量转录层（brain_turn_messages + 每轮计数 + 会话增量游标）", (
        "CREATE TABLE IF NOT EXISTS brain_turn_messages ("
        "id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT, turn_id BIGINT UNSIGNED NOT NULL, "
        "seq SMALLINT UNSIGNED NOT NULL DEFAULT 0, role VARCHAR(16) NOT NULL DEFAULT '', "
        "tool_name VARCHAR(64) NOT NULL DEFAULT '', tool_call_id VARCHAR(128) NOT NULL DEFAULT '', "
        "raw_bytes INT UNSIGNED NOT NULL DEFAULT 0, body LONGBLOB NOT NULL, "
        "ts DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "PRIMARY KEY (id), KEY idx_turn_seq (turn_id, seq), KEY idx_tm_ts (ts)"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci",
        "ALTER TABLE brain_turns ADD COLUMN IF NOT EXISTS message_count SMALLINT UNSIGNED NOT NULL DEFAULT 0 AFTER truncated",
        "ALTER TABLE brain_turns ADD COLUMN IF NOT EXISTS message_bytes INT UNSIGNED NOT NULL DEFAULT 0 AFTER message_count",
        "ALTER TABLE brain_sessions ADD COLUMN IF NOT EXISTS archived_messages INT UNSIGNED NOT NULL DEFAULT 0 AFTER turn_count",
    )),
}

# DDL 幂等重跑时的“已存在”类报错（迁移时容忍）
_BENIGN_DDL_ERRNOS = {1050, 1060, 1061, 1826}


def _benign_ddl_error(exc: Exception) -> bool:
    return bool(exc.args) and exc.args[0] in _BENIGN_DDL_ERRNOS


def _is_duplicate_error(exc: Exception) -> bool:
    """唯一键冲突（active 行内容防重）→ errno 1062。"""
    return bool(exc.args) and exc.args[0] == 1062


def _db_safe(value: Any) -> Any:
    """把无法编码成 utf-8 的 str（lone surrogate）替换掉。

    PyMySQL 在绑定参数时会做 utf-8 编码，遇到 JSON 里的 `\\ud800` 这类非法代理字符会抛
    UnicodeEncodeError，导致整条 INSERT（整轮对话/整条记忆）失败。这里只对确有非法字符的
    字符串做一次替换，正常值原样返回。
    """
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return value.encode("utf-8", "replace").decode("utf-8")
    return value


def _cursor_start(msgs: list[dict[str, Any]], done: int) -> int:
    """转录游标切片：正常从 done 续写；历史变短（压缩/回退）时退回最后一个 user。

    独立成纯函数便于自测。done 为负数按 0 处理（坏数据不应导致永久跳过）。
    """
    try:
        done = int(done)
    except (TypeError, ValueError):
        return 0
    if done < 0:
        return 0
    if done > len(msgs):
        for i in range(len(msgs) - 1, -1, -1):
            if str(msgs[i].get("role") or "") == "user":
                return i
        return 0
    return done


class BrainStore:
    """Redis + MariaDB 双存储引擎。线程安全（连接按线程持有）。"""

    def __init__(self, config: Optional[dict[str, Any]] = None):
        self.cfg: dict[str, Any] = dict(config or resolve_config())
        self.machine: str = str(self.cfg.get("machine") or "").strip() or socket.gethostname()
        self._redis = None
        self._redis_lock = threading.Lock()
        self._tls = threading.local()
        self._schema_ready = False
        self._schema_lock = threading.Lock()
        self._vec_wake = threading.Event()   # 写入后唤醒本进程 drainer
        self._maint_started = False           # 维护线程每进程最多一个
        self._maint_lock = threading.Lock()
        self._distill_started = False         # 蒸馏线程每进程最多一个
        self._distill_lock = threading.Lock()
        self._vsec: Optional[dict] = None    # 向量层 key 缓存（来源：qdrant 容器配置）
        self._vsec_ts = 0.0

    # -------------------------------------------------- 连接

    def redis(self):
        import redis as redis_lib

        with self._redis_lock:
            if self._redis is None:
                self._redis = redis_lib.Redis(
                    host=self.cfg["redis_host"],
                    port=int(self.cfg["redis_port"]),
                    db=int(self.cfg.get("redis_db", 0)),
                    password=self.cfg.get("redis_password") or None,
                    socket_connect_timeout=3,
                    socket_timeout=3,
                    decode_responses=True,
                    health_check_interval=30,
                )
            return self._redis

    def _conn(self):
        import pymysql

        conn = getattr(self._tls, "conn", None)
        if conn is not None:
            try:
                conn.ping(reconnect=True)
                return conn
            except Exception:  # noqa: BLE001
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
                self._tls.conn = None
        conn = pymysql.connect(
            host=self.cfg["mariadb_host"],
            port=int(self.cfg["mariadb_port"]),
            user=self.cfg["mariadb_user"],
            password=self.cfg.get("mariadb_password") or "",
            database=self.cfg["mariadb_db"],
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=True,
            connect_timeout=3,
            read_timeout=10,
            write_timeout=10,
        )
        self._tls.conn = conn
        return conn

    def _exec(self, sql: str, params: tuple = (), *, fetch: bool = False):
        conn = self._conn()
        safe = tuple(_db_safe(p) for p in params) if params else params
        with conn.cursor() as cur:
            cur.execute(sql, safe)
            if fetch:
                return cur.fetchall()
            return cur.rowcount, cur.lastrowid

    def ensure_schema(self) -> None:
        with self._schema_lock:
            if self._schema_ready:
                return
            conn = self._conn()
            with conn.cursor() as cur:
                for ddl in _DDL:
                    cur.execute(ddl)
                cur.execute("SELECT meta_value FROM brain_meta WHERE meta_key = 'schema_version'")
                row = cur.fetchone()
                current = int(row["meta_value"]) if row and str(row["meta_value"]).isdigit() else 0
                if current < SCHEMA_VERSION:
                    cur.execute("SELECT GET_LOCK('brain_schema_migrate', 15) AS got")
                    got = (cur.fetchone() or {}).get("got")
                    try:
                        # 拿到锁后重读：可能已被其它进程迁移完
                        cur.execute("SELECT meta_value FROM brain_meta WHERE meta_key = 'schema_version'")
                        row = cur.fetchone()
                        current = int(row["meta_value"]) if row and str(row["meta_value"]).isdigit() else 0
                        for ver in sorted(_MIGRATIONS):
                            if ver <= current:
                                continue
                            desc, stmts = _MIGRATIONS[ver]
                            for stmt in stmts:
                                try:
                                    cur.execute(stmt)
                                except Exception as exc:  # noqa: BLE001
                                    if not _benign_ddl_error(exc):
                                        raise
                                    logger.debug("brain: 迁移 v%s 语句已存在，跳过: %s", ver, exc)
                            logger.info("brain: schema 迁移 v%s 完成（%s）", ver, desc)
                        cur.execute(
                            "INSERT INTO brain_meta (meta_key, meta_value) VALUES ('schema_version', %s) "
                            "ON DUPLICATE KEY UPDATE meta_value = VALUES(meta_value)",
                            (str(SCHEMA_VERSION),))
                    finally:
                        if got:
                            cur.execute("SELECT RELEASE_LOCK('brain_schema_migrate')")
            self._schema_ready = True
        # schema 就绪即惰性启动维护线程（与向量 drainer 同属进程内后台线程）
        self.start_maintenance()
        self.start_distill()

    def schema_version(self) -> int:
        self.ensure_schema()
        rows = self._exec(
            "SELECT meta_value FROM brain_meta WHERE meta_key = 'schema_version'", fetch=True)
        try:
            return int(rows[0]["meta_value"]) if rows else 0
        except (KeyError, TypeError, ValueError):
            return 0

    def close(self) -> None:
        conn = getattr(self._tls, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._tls.conn = None
        with self._redis_lock:
            if self._redis is not None:
                try:
                    self._redis.close()
                except Exception:  # noqa: BLE001
                    pass
                self._redis = None

    # -------------------------------------------------- 远程模式（Memory Service 客户端）
    #
    # mind=remote：召回/写入改走 HTTP 调服务端 /v1（与 console 共用端口，Bearer 鉴权），
    # 服务端用令牌反查 machine，客户端不在 body 里传 machine。
    # 默认 remote_enabled=0 → 本段一行都不执行，行为与改造前逐字节一致。
    # 关键路径（prefetch_block）超时用 remote_timeout_ms（默认 800ms），绝不无限等待；
    # 任何远程异常都不向上抛：remote_fallback=1 回落本地，=0 明确失败（返回空/False）。

    def _remote_on(self) -> bool:
        """远程模式是否生效：总开关打开且配了 URL（缺一不可）。"""
        return bool(int(self.cfg.get("remote_enabled") or 0)) and bool(
            str(self.cfg.get("remote_url") or "").strip())

    def _remote_token(self) -> str:
        """Bearer 令牌：优先 cfg；否则读 $HERMES_HOME/.env 的 BRAIN_REMOTE_TOKEN。

        逐行解析 .env 并去引号（不用 shell source）。约定的存放点是 .env：不落 brain.json、
        不进日志、不完整回显（CLI 只打掩码）；cfg["remote_token"] 仅作显式覆盖/调试用。
        """
        tok = str(self.cfg.get("remote_token") or "").strip()
        if tok:
            return tok
        home = str(self.cfg.get("hermes_home") or "").strip() or str(hermes_home_path())
        return str(_dotenv_get(Path(home) / ".env", "BRAIN_REMOTE_TOKEN") or "").strip()

    def _remote_call(self, path: str, payload: dict, *, timeout_ms: int = 0) -> dict:
        """POST 一个 /v1 端点并校验返回体。

        非 200 或 ok != true → 抛异常；网络异常同样向上抛，由调用方统一兜底（回落/明确失败）。
        """
        url = str(self.cfg.get("remote_url") or "").rstrip("/") + str(path)
        ms = int(timeout_ms or self.cfg.get("remote_timeout_ms") or 800)
        code, data = self._http_json(
            "POST", url, headers={"Authorization": f"Bearer {self._remote_token()}"},
            payload=payload, timeout=max(0.05, ms / 1000.0))
        if int(code) != 200 or not isinstance(data, dict) or data.get("ok") is not True:
            raise BrainHTTPError(int(code), url, json.dumps(data, ensure_ascii=False)[:200])
        return data

    def remote_check(self) -> dict[str, Any]:
        """远程模式自检：开关 / URL / 令牌是否配置（只给掩码）/ GET /v1/health 往返耗时。"""
        tok = self._remote_token()
        url = str(self.cfg.get("remote_url") or "").strip()
        out: dict[str, Any] = {
            "enabled": self._remote_on(),
            "url": url,
            "token_configured": bool(tok),
            "token_masked": _mask_secret(tok),
            "timeout_ms": int(self.cfg.get("remote_timeout_ms") or 0),
            "fallback": bool(int(self.cfg.get("remote_fallback") or 0)),
            "turn_sync": bool(int(self.cfg.get("remote_turn_sync") or 0)),
            "ok": False,
        }
        if not url:
            out["error"] = "remote_url 未配置"
            return out
        t0 = time.time()
        try:
            code, data = self._http_json(
                "GET", url.rstrip("/") + "/v1/health",
                headers={"Authorization": f"Bearer {tok}"},
                timeout=max(0.05, int(self.cfg.get("remote_timeout_ms") or 800) / 1000.0))
            out["http_status"] = int(code)
            out["health"] = data if isinstance(data, dict) else {}
            out["ok"] = bool(int(code) == 200 and out["health"].get("ok") is True)
        except Exception as exc:  # noqa: BLE001
            out["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        out["latency_ms"] = round((time.time() - t0) * 1000, 1)
        return out

    # -------------------------------------------------- Redis 辅助

    def _rk(self, *parts) -> str:
        # redis_key_prefix 空 → KEY_PREFIX（"brain:"），键名与历史版本逐字节一致；
        # 非空 → 用它替换 "brain"（去尾部冒号），实现「统一 db0 + 每实例一前缀」。
        pfx = str(self.cfg.get("redis_key_prefix") or "").strip().strip(":") or KEY_PREFIX.rstrip(":")
        return pfx + ":" + ":".join(str(p) for p in parts)

    def _hot_put(self, mem_id: int, kind: str, content: str, tags: str,
                 importance: int, source: str) -> bool:
        r = self.redis()
        ttl = int(self.cfg["hot_ttl_days"]) * 86400
        payload = json.dumps({
            "id": mem_id, "kind": kind, "content": _clip(content, 1000),
            "tags": tags, "importance": importance, "source": source,
            "machine": self.machine[:48],
            "ts": int(time.time()),
        }, ensure_ascii=False)
        pipe = r.pipeline()
        pipe.set(self._rk("hot", mem_id), payload, ex=ttl)
        pipe.zadd(self._rk("hot", "index"), {str(mem_id): time.time()})
        pipe.zremrangebyrank(self._rk("hot", "index"), 0, -501)
        pipe.expire(self._rk("hot", "index"), ttl)
        pipe.execute()
        return True

    # -------------------------------------------------- 写路径

    def remember(self, content: str, *, kind: str = "note", tags: str = "",
                 importance: int = 5, source: str = "tool",
                 session_id: str = "", memory_type: str = "") -> dict[str, Any]:
        # list/dict 直接 .strip() 会抛 AttributeError（调用方只看到模糊异常）；
        # 统一转成字符串，空值仍按 BrainError 处理。
        content = str(content or "").strip()
        if not content:
            raise BrainError("content 不能为空")
        try:
            importance = max(1, min(10, int(importance)))
        except (TypeError, ValueError):
            importance = 5
        kind = (str(kind or "note").strip() or "note")[:32]
        mtype = _memory_type(kind, memory_type)
        tags = str(tags or "").strip()[:255]
        if self._remote_on():
            # 服务端写的是权威库：成功后直接返回，不再写本地 MariaDB/Redis（避免双写不一致）
            try:
                data = self._remote_call("/v1/memory/remember", {
                    "content": content, "kind": kind, "tags": tags,
                    "importance": importance, "memory_type": mtype})
                saved = data.get("saved")
                if isinstance(saved, dict):
                    return dict(saved)
                return {"id": None, "kind": kind, "importance": importance,
                        "memory_type": mtype, "hot_cache": False, "deduped": False}
            except Exception as exc:  # noqa: BLE001
                if not int(self.cfg.get("remote_fallback") or 0):
                    logger.warning("brain: 远程写入失败，未启用回落（明确失败）: %s", str(exc)[:200])
                    return {"id": None, "kind": kind, "importance": importance,
                            "memory_type": mtype, "hot_cache": False, "deduped": False,
                            "error": str(exc)[:200]}
                logger.warning("brain: 远程写入失败，回落本地: %s", str(exc)[:200])
        self.ensure_schema()
        mem_id = None
        mem_vals: tuple = (kind, content, tags, importance)
        deduped = False
        for attempt in (0, 1):
            try:
                _, mem_id = self._exec(
                    "INSERT INTO brain_memories (kind, memory_type, content, tags, importance, "
                    "source, session_id, machine) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (kind, mtype, content, tags, importance, str(source or "")[:64],
                     str(session_id or "")[:96], self.machine[:48]),
                )
                break
            except Exception as exc:  # noqa: BLE001
                if attempt or not _is_duplicate_error(exc):
                    raise
                # 与某条活跃记忆内容完全相同（跨机器镜像/回填/手工重复）：
                # 不新建行，改为刷新既有行（重要度取大、时间前移、空分层补全）。
                row = self._exec(
                    "SELECT id FROM brain_memories WHERE active = 1 AND content_hash = MD5(%s) LIMIT 1",
                    (content,), fetch=True)
                if not row:
                    continue  # 竞态：恰被停用；重试一次插入
                mem_id = int(row[0]["id"])
                deduped = True
                self._exec(
                    "UPDATE brain_memories SET importance = GREATEST(importance, %s), "
                    "memory_type = IF(memory_type = '', %s, memory_type), "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = %s AND active = 1",
                    (importance, mtype, mem_id))
                row2 = self._exec(
                    "SELECT kind, content, tags, importance FROM brain_memories WHERE id = %s",
                    (mem_id,), fetch=True)[0]
                mem_vals = (row2["kind"], row2["content"], row2["tags"], row2["importance"])
                break
        hot_ok = False
        try:
            hot_ok = self._hot_put(int(mem_id), mem_vals[0], mem_vals[1], mem_vals[2], mem_vals[3],
                                   str(source or ""))
        except Exception as exc:  # noqa: BLE001
            logger.debug("brain: Redis 热点镜像失败: %s", exc)
        try:
            self._vec_wake.set()   # 通知本进程 drainer 尽快向量化（不阻塞保存）
        except Exception:  # noqa: BLE001
            pass
        return {"id": mem_id, "kind": mem_vals[0], "importance": mem_vals[3],
                "memory_type": mtype, "hot_cache": hot_ok, "deduped": deduped}

    def deactivate_by_content(self, content: str) -> int:
        content = str(content or "")
        if not content.strip():
            return 0
        self.ensure_schema()
        rows = self._exec(
            "SELECT id FROM brain_memories WHERE active = 1 AND content = %s", (content,), fetch=True)
        ids = [r["id"] for r in rows]
        if not ids:
            return 0
        marks = ", ".join(["%s"] * len(ids))
        rowcount, _ = self._exec(
            f"UPDATE brain_memories SET active = 0, deactivated_at = CURRENT_TIMESTAMP "
            f"WHERE id IN ({marks})", tuple(ids))
        try:
            r = self.redis()
            pipe = r.pipeline()
            for mid in ids:
                pipe.delete(self._rk("hot", mid))
                pipe.zrem(self._rk("hot", "index"), str(mid))
            pipe.execute()
        except Exception:  # noqa: BLE001
            pass
        return rowcount

    def forget(self, mem_id: int) -> dict[str, Any]:
        if self._remote_on():
            # 服务端写的是权威库：成功后直接返回，不再动本地 MariaDB/Redis
            try:
                data = self._remote_call("/v1/memory/forget", {"id": int(mem_id)})
                res = data.get("result")
                return dict(res) if isinstance(res, dict) else {"id": int(mem_id), "deactivated": 0}
            except Exception as exc:  # noqa: BLE001
                if not int(self.cfg.get("remote_fallback") or 0):
                    logger.warning("brain: 远程废止失败，未启用回落（明确失败）: %s", str(exc)[:200])
                    return {"id": int(mem_id), "deactivated": 0, "error": str(exc)[:200]}
                logger.warning("brain: 远程废止失败，回落本地: %s", str(exc)[:200])
        self.ensure_schema()
        rowcount, _ = self._exec(
            "UPDATE brain_memories SET active = 0, deactivated_at = CURRENT_TIMESTAMP "
            "WHERE id = %s AND active = 1", (int(mem_id),))
        try:
            r = self.redis()
            r.delete(self._rk("hot", mem_id))
            r.zrem(self._rk("hot", "index"), str(mem_id))
        except Exception:  # noqa: BLE001
            pass
        return {"id": int(mem_id), "deactivated": rowcount}

    def record_turn(self, session_id: str, user_content: str, assistant_content: str,
                    *, platform: str = "", author: str = "",
                    messages: Optional[list[dict[str, Any]]] = None) -> int:
        if self._remote_on() and int(self.cfg.get("remote_turn_sync") or 0):
            # 回合归档走远程（默认关）：服务端自己截断/写库，与本地路径口径一致
            try:
                data = self._remote_call("/v1/memory/turn", {
                    "session_id": str(session_id or ""), "user": str(user_content or ""),
                    "assistant": str(assistant_content or ""), "platform": str(platform or "")})
                return int(data.get("id") or 0)
            except Exception as exc:  # noqa: BLE001
                if not int(self.cfg.get("remote_fallback") or 0):
                    logger.warning("brain: 远程回合归档失败，未启用回落（明确失败）: %s", str(exc)[:200])
                    return 0
                logger.warning("brain: 远程回合归档失败，回落本地: %s", str(exc)[:200])
        session_id = str(session_id or "")[:96]
        platform = str(platform or "")[:48]
        author = str(author or "")[:64]
        u_raw, a_raw = str(user_content or ""), str(assistant_content or "")
        u, a = u_raw[:16000], a_raw[:16000]
        truncated = 1 if (len(u_raw) > 16000 or len(a_raw) > 16000) else 0
        self.ensure_schema()
        _, turn_id = self._exec(
            "INSERT INTO brain_turns (session_id, platform, author, machine, truncated, "
            "user_content, assistant_content) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (session_id, platform, author, self.machine[:48], truncated, u, a),
        )
        self._exec(
            "INSERT INTO brain_sessions (session_id, platform, machine, started_at, last_active, turn_count) "
            "VALUES (%s, %s, %s, NOW(), NOW(), 1) "
            "ON DUPLICATE KEY UPDATE last_active = NOW(), turn_count = turn_count + 1, "
            "platform = VALUES(platform), machine = VALUES(machine)",
            (session_id, platform, self.machine[:48]),
        )
        # 全量转录层（可关）：工具调用/工具输出/推理按 zlib 增量落库
        stored = stored_bytes = 0
        if messages:
            try:
                stored, stored_bytes = self.store_turn_messages(turn_id, session_id, messages)
            except Exception as exc:  # noqa: BLE001
                logger.warning("brain: 转录层写入失败（正文已存）: %s", str(exc)[:200])
            if stored:
                try:
                    self._exec(
                        "UPDATE brain_turns SET message_count = %s, message_bytes = %s WHERE id = %s",
                        (min(stored, 65535), min(stored_bytes, 4294967295), turn_id))
                except Exception as exc:  # noqa: BLE001
                    logger.debug("brain: 转录计数更新失败: %s", exc)
        try:
            r = self.redis()
            stream = self._rk("recent")
            r.xadd(
                stream,
                {"sid": session_id, "platform": platform, "author": author, "machine": self.machine[:48],
                 "user": u[:600], "assistant": a[:600], "ts": f"{time.time():.3f}"},
                maxlen=int(self.cfg["stream_maxlen"]), approximate=True,
            )
            r.expire(stream, int(self.cfg["short_term_ttl_days"]) * 86400)
            skey = self._rk("session", session_id)
            pipe = r.pipeline()
            pipe.hset(skey, mapping={"platform": platform,
                                     "last_active": str(int(time.time()))})
            pipe.hincrby(skey, "turns", 1)
            pipe.expire(skey, int(self.cfg["short_term_ttl_days"]) * 86400)
            pipe.execute()
        except Exception as exc:  # noqa: BLE001
            logger.debug("brain: Redis 回合写入失败: %s", exc)
        return turn_id

    # -------------------------------------------------- 全量转录层

    def store_turn_messages(self, turn_id: int, session_id: str,
                            messages: list[dict[str, Any]]) -> tuple[int, int]:
        """把本轮的完整消息链（含工具调用/工具输出/推理）压进 brain_turn_messages。

        Hermes 每轮传的是「到目前为至的完整对话」，所以只写增量：已归档条数记在
        brain_sessions.archived_messages，这里只取尾部新增部分，避免 N² 重复。
        历史被压缩或回退（条数变少）时，退回最后一个 user 消息重新起算。
        返回 (写入条数, 原始字节数)。
        """
        msgs = [m for m in (messages or []) if isinstance(m, dict)]
        if not msgs or not int(self.cfg.get("turn_messages_enabled") or 0):
            return 0, 0
        sid = _db_safe(str(session_id or ""))[:96]
        max_msg = int(self.cfg.get("turn_msg_max_bytes") or 1048576)
        max_total = int(self.cfg.get("turn_total_max_bytes") or 16777216)
        conn = self._conn()
        rows: list[tuple] = []
        total = 0
        try:
            # 读游标 → 写消息 → 推游标必须在同一事务里，并对会话行加锁：
            # 否则网关与 CLI/cron 同时同步同一会话时会各自从同一 done 起写，重复归档；
            # 中途崩溃也会留下「消息已写、游标没推」的重复窗口。
            conn.begin()
            with conn.cursor() as cur:
                # 会话行可能不存在（旧库/手工删行）：先补齐。否则下面的游标 UPDATE
                # 会命中 0 行且不报错，archived_messages 永远停在 0，每轮重复全量归档。
                cur.execute(
                    "INSERT INTO brain_sessions (session_id, platform, machine, started_at, "
                    "last_active, turn_count) VALUES (%s, '', %s, NOW(), NOW(), 0) "
                    "ON DUPLICATE KEY UPDATE session_id = VALUES(session_id)",
                    (sid, self.machine[:48]))
                cur.execute(
                    "SELECT archived_messages FROM brain_sessions WHERE session_id = %s FOR UPDATE",
                    (sid,))
                row = cur.fetchone()
                done = _cursor_start(msgs, int(row["archived_messages"] or 0) if row else 0)
                delta = msgs[done:]
                for seq, m in enumerate(delta):
                    role = _db_safe(str(m.get("role") or ""))[:16]
                    tool_name = _db_safe(str(m.get("tool_name") or m.get("name") or ""))[:64]
                    tool_call_id = _db_safe(str(m.get("tool_call_id") or ""))[:128]
                    payload = {k: v for k, v in m.items()
                               if k not in ("role", "tool_name", "name", "tool_call_id")}
                    try:
                        raw = json.dumps(payload, ensure_ascii=False,
                                         default=str).encode("utf-8", "replace")
                    except Exception:  # noqa: BLE001
                        raw = json.dumps({"_unserializable": str(payload)[:4096]},
                                         ensure_ascii=False).encode("utf-8", "replace")
                    if len(raw) > max_msg:
                        content = payload.get("content")
                        payload = {"_truncated_bytes": len(raw),
                                   "content": str(content if content is not None else "")[:max_msg // 4]}
                        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8", "replace")
                    if total + len(raw) > max_total:
                        logger.warning("brain: 单轮转录超过 %s 字节上限，剩余 %s 条将在下一轮续存",
                                       max_total, len(delta) - seq)
                        break
                    total += len(raw)
                    rows.append((turn_id, seq, role, tool_name, tool_call_id, len(raw),
                                 zlib.compress(raw, 6)))
                if rows:
                    cur.executemany(
                        "INSERT INTO brain_turn_messages (turn_id, seq, role, tool_name, "
                        "tool_call_id, raw_bytes, body) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s)", rows)
                    cur.execute(
                        "UPDATE brain_sessions SET archived_messages = %s WHERE session_id = %s",
                        (done + len(rows), sid))
            if rows:
                conn.commit()
            else:
                conn.rollback()
        except BaseException:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            raise
        return len(rows), total

    def turn_detail(self, turn_id: int = 0, *, session_id: str = "",
                    max_chars: int = 1500, latest: bool = False) -> dict[str, Any]:
        """按 turn_id（或 session_id+latest）取回一轮的完整过程（工具调用/输出/推理）。"""
        self.ensure_schema()
        tid = int(turn_id or 0)
        if tid <= 0 and session_id:
            rows = self._exec(
                "SELECT id FROM brain_turns WHERE session_id = %s ORDER BY ts DESC, id DESC LIMIT 1",
                (str(session_id)[:96],), fetch=True)
            tid = int(rows[0]["id"]) if rows else 0
        if tid <= 0:
            return {"found": False, "reason": "需要 turn_id，或可用的 session_id",
                    "hint": "brain_recent 可看到最近轮次"}
        head = self._exec(
            "SELECT id, session_id, platform, author, machine, truncated, message_count, "
            "message_bytes, ts, user_content, assistant_content FROM brain_turns WHERE id = %s",
            (tid,), fetch=True)
        if not head:
            return {"found": False, "turn_id": tid}
        msgs = self._exec(
            "SELECT seq, role, tool_name, tool_call_id, raw_bytes, body "
            "FROM brain_turn_messages WHERE turn_id = %s ORDER BY seq", (tid,), fetch=True)
        cap = max(200, min(20000, int(max_chars or 1500)))
        out_msgs: list[dict[str, Any]] = []
        for r in msgs:
            try:
                payload = json.loads(zlib.decompress(r["body"]).decode("utf-8", "replace"))
                if not isinstance(payload, dict):
                    payload = {"content": payload}
            except Exception:  # noqa: BLE001
                payload = {"_decode_error": True}
            content = payload.get("content")
            if isinstance(content, list):
                content = " ".join(str(p.get("text") or "") for p in content
                                   if isinstance(p, dict) and p.get("type") == "text")
            text = str(content or "")
            cut = len(text) - cap
            if cut > 0:
                text = text[:cap] + f"...[+{cut} 字符]"
            item: dict[str, Any] = {"seq": int(r["seq"]), "role": r["role"],
                                    "tool_name": r["tool_name"] or "",
                                    "raw_bytes": int(r["raw_bytes"] or 0), "content": text}
            if payload.get("tool_calls"):
                item["tool_calls"] = payload["tool_calls"]
            if payload.get("reasoning"):
                item["reasoning"] = str(payload["reasoning"])[:cap]
            if payload.get("_truncated_bytes"):
                item["truncated_bytes"] = payload["_truncated_bytes"]
            out_msgs.append(item)
        turn = dict(head[0])
        for k, v in list(turn.items()):
            if isinstance(v, datetime):
                turn[k] = v.isoformat(sep=" ", timespec="seconds")
        return {"found": True, "turn": turn, "messages": out_msgs, "stored": len(out_msgs)}

    def touch_session(self, session_id: str, platform: str = "") -> None:
        session_id = str(session_id or "")[:96]
        if not session_id:
            return
        try:
            self.ensure_schema()
            self._exec(
                "INSERT INTO brain_sessions (session_id, platform, machine, started_at, last_active, turn_count) "
                "VALUES (%s, %s, %s, NOW(), NOW(), 0) "
                "ON DUPLICATE KEY UPDATE last_active = NOW(), platform = VALUES(platform), machine = VALUES(machine)",
                (session_id, str(platform or "")[:48], self.machine[:48]),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("brain: touch_session 失败: %s", exc)
        try:
            skey = self._rk("session", session_id)
            self.redis().hset(skey, "last_active", str(int(time.time())))
            self.redis().expire(skey, int(self.cfg["short_term_ttl_days"]) * 86400)
        except Exception:  # noqa: BLE001
            pass

    def archive(self, session_id: str, kind: str, payload: str, *, platform: str = "") -> int:
        payload = str(payload or "")
        if not payload:
            return 0
        if len(payload) > 400_000:
            payload = payload[:400_000] + "\n[truncated]"
        self.ensure_schema()
        _, row_id = self._exec(
            "INSERT INTO brain_archive (session_id, machine, platform, kind, payload) "
            "VALUES (%s, %s, %s, %s, %s)",
            (str(session_id or "")[:96], self.machine[:48], str(platform or "")[:48],
             str(kind or "precompress")[:24], payload),
        )
        return row_id

    # -------------------------------------------------- 读路径

    def _search_where(self, columns: tuple[str, ...], terms: list[str]) -> tuple[str, list]:
        clauses, params = [], []
        for t in terms:
            sub = " OR ".join(f"{c} LIKE %s" for c in columns)
            clauses.append(f"({sub})")
            params.extend([f"%{_esc_like(t)}%"] * len(columns))
        return " OR ".join(clauses), params

    def search_memories(self, query: str, *, limit: int = 5,
                        kind: Optional[str] = None) -> list[dict[str, Any]]:
        terms = search_terms(query)
        if not terms:
            return []
        where, params = self._search_where(("content", "tags"), terms)
        sql = (
            "SELECT id, kind, memory_type, content, tags, importance, source, session_id, machine, "
            "created_at, updated_at "
            f"FROM brain_memories WHERE active = 1 AND superseded_by IS NULL AND ({where}) "
        )
        if kind:
            sql += "AND kind = %s "
            params.append(kind)
        sql += "ORDER BY importance DESC, updated_at DESC, id DESC LIMIT %s"
        params.append(int(limit))
        self.ensure_schema()
        return list(self._exec(sql, tuple(params), fetch=True))

    def search_turns(self, query: str, *, limit: int = 3) -> list[dict[str, Any]]:
        terms = search_terms(query)
        if not terms:
            return []
        where, params = self._search_where(("user_content", "assistant_content"), terms)
        sql = (
            "SELECT id, session_id, platform, machine, user_content, assistant_content, ts "
            f"FROM brain_turns WHERE ({where}) "
        )
        window_days = int(self.cfg.get("search_turns_days") or 0)
        if window_days > 0:
            sql += "AND ts >= (NOW() - INTERVAL %s DAY) "
            params.append(window_days)
        sql += "ORDER BY ts DESC, id DESC LIMIT %s"
        params.append(int(limit))
        self.ensure_schema()
        return list(self._exec(sql, tuple(params), fetch=True))

    def recent_turns(self, limit: int = 10, *, source: str = "redis") -> list[dict[str, Any]]:
        if source == "redis":
            try:
                entries = self.redis().xrevrange(self._rk("recent"), count=int(limit))
                out = []
                for entry_id, f in entries:
                    out.append({
                        "stream_id": entry_id,
                        "session": f.get("sid", ""),
                        "platform": f.get("platform", ""),
                        "author": f.get("author", ""),
                        "machine": f.get("machine", ""),
                        "user": f.get("user", ""),
                        "assistant": f.get("assistant", ""),
                        "ts": float(f.get("ts") or 0),
                    })
                return out
            except Exception as exc:  # noqa: BLE001
                logger.debug("brain: Redis 最近对话读取失败，回退 MariaDB: %s", exc)
                source = "db"
        self.ensure_schema()
        rows = self._exec(
            "SELECT id, session_id, platform, author, machine, user_content, assistant_content, ts "
            "FROM brain_turns ORDER BY ts DESC LIMIT %s", (int(limit),), fetch=True)
        return [{
            "stream_id": str(r["id"]), "session": r["session_id"], "platform": r["platform"],
            "author": r.get("author", ""), "machine": r.get("machine", ""), "user": r["user_content"],
            # 库内 DATETIME 是 UTC 口径；naive .timestamp() 会按本机时区解释
            "assistant": r["assistant_content"], "ts": _utc_epoch(r["ts"]) or 0,
        } for r in rows]

    def hot_recent(self, limit: int = 5) -> list[dict[str, Any]]:
        try:
            r = self.redis()
            ids = r.zrevrange(self._rk("hot", "index"), 0, max(0, int(limit) - 1))
            out = []
            for mid in ids:
                raw = r.get(self._rk("hot", mid))
                if raw:
                    try:
                        out.append(json.loads(raw))
                    except ValueError:
                        continue
            return out
        except Exception:  # noqa: BLE001
            return []

    # -------------------------------------------------- 召回组装

    def prefetch_block(self, query: str, *, session_id: str = "",
                       use_cache: bool = True) -> tuple[str, int]:
        """组装本轮注入文本；结果按查询哈希缓存进 Redis。"""
        query = (query or "").strip()
        if not query:
            return "", 0
        if self._remote_on():
            # 远程召回：直接返回服务端注入文本，不再查本地 Redis 查询缓存
            try:
                payload: dict[str, Any] = {"query": query}
                if session_id:
                    payload["session_id"] = str(session_id)
                data = self._remote_call("/v1/memory/recall", payload,
                                         timeout_ms=int(self.cfg.get("remote_timeout_ms") or 800))
                return str(data.get("text") or ""), int(data.get("count") or 0)
            except Exception as exc:  # noqa: BLE001
                if not int(self.cfg.get("remote_fallback") or 0):
                    logger.warning("brain: 远程召回失败，未启用回落（返回空）: %s", str(exc)[:200])
                    return "", 0
                logger.warning("brain: 远程召回失败，回落本地: %s", str(exc)[:200])
        cache_key = self._rk("qcache", hashlib.sha1(query.encode("utf-8")).hexdigest())
        if use_cache:
            try:
                raw = self.redis().get(cache_key)
                if raw:
                    data = json.loads(raw)
                    return str(data.get("text", "")), int(data.get("count", 0))
            except Exception:  # noqa: BLE001
                pass
        mems = self.search_memories_hybrid(query, limit=int(self.cfg["prefetch_memories"]))
        turns = self.search_turns(query, limit=int(self.cfg["prefetch_turns"]))
        text = self._compose(mems, turns)
        count = len(mems) + len(turns)
        if text:
            try:
                self.redis().set(
                    cache_key,
                    json.dumps({"text": text, "count": count}, ensure_ascii=False),
                    ex=int(self.cfg["search_cache_ttl"]),
                )
            except Exception:  # noqa: BLE001
                pass
        return text, count

    def _compose(self, mems: list[dict[str, Any]], turns: list[dict[str, Any]]) -> str:
        budget = int(self.cfg["prefetch_max_chars"])
        blocks: list[str] = []
        if mems:
            lines = ["[brain 长期记忆 · MariaDB]"]
            for m in mems:
                line = f"- [{m['kind']}·重要度{m['importance']}] {_clip(str(m['content']), 240)}"
                if m.get("tags"):
                    line += f" (tags: {m['tags']})"
                if m.get("machine"):
                    line += f" [{m['machine']}]"
                lines.append(line)
            blocks.append("\n".join(lines))
        if turns:
            lines = ["[brain 相关历史对话 · 归档]"]
            for t in turns:
                ts = t.get("ts")
                ts_s = ts.strftime("%Y-%m-%d") if hasattr(ts, "strftime") else str(ts)[:16]
                snippet = _clip(str(t.get("user_content") or "").replace("\n", " "), 150)
                if snippet:
                    mch = str(t.get("machine") or "")
                    tag = f" [{mch}]" if mch else ""
                    lines.append(f"- {ts_s}{tag}: {snippet}")
            blocks.append("\n".join(lines))
        text = "\n".join(b for b in blocks if len(b.splitlines()) > 1)
        if len(text) > budget:
            text = text[:budget].rstrip() + " …"
        return text

    # -------------------------------------------------- 向量层（语义召回）
    #
    # 设计要点：
    #   * key 不落 Hermes 配置：从 qdrant 的配置文件读取（用户指定，唯一存放点）；
    #   * 嵌入走 SiliconFlow（Qwen/Qwen3-Embedding-4B，2560 维）；向量库存 Qdrant；
    #   * 写入异步（drainer 线程 + wake 事件）；召回同步（超时即降级关键词）；
    #   * 任一环节失败 → 自动回落纯关键词，绝不阻断记忆读写。

    def _vector_secrets(self) -> dict[str, str]:
        """向量层 key：从 qdrant 的配置文件读取（service.api_key；缓存 5 分钟，不落 Hermes 配置）。"""
        now = time.time()
        if self._vsec is not None and now - self._vsec_ts < 300:
            return self._vsec
        key = ""
        path = str(self.cfg.get("qdrant_config_file") or "/root/qdrant/qdrant-config/production.yaml")
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
            section = ""
            for raw in text.splitlines():
                line = raw.split("#", 1)[0].rstrip()
                if not line.strip():
                    continue
                if len(line) - len(line.lstrip()) == 0:
                    section = line.strip().rstrip(":")
                elif section == "service":
                    m = re.match(r"\s*api_key\s*:\s*(.+?)\s*$", line)
                    if m:
                        val = m.group(1).strip().strip('"').strip("'")
                        if val and val.lower() not in ("null", "~"):
                            key = val
                            break
        except Exception as exc:  # noqa: BLE001
            logger.debug("brain: 读取 qdrant 配置文件失败: %s", exc)
        out: dict[str, str] = {}
        if key:
            out["qdrant"] = key
            out["embed"] = key          # 首期两处同一把 key（用户指定）
        self._vsec, self._vsec_ts = out, now
        return out

    def _http_json(self, method: str, url: str, *, headers: Optional[dict] = None,
                   payload: Optional[dict] = None, timeout: float = 15) -> tuple[int, dict]:
        import urllib.error
        import urllib.request
        hdrs = {"Accept": "application/json", "User-Agent": "hermes-brain/1.1"}
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
                return int(resp.status), (json.loads(body) if body.strip() else {})
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:300]
            if int(exc.code) == 401:      # key 可能刚被轮换：让缓存失效，下次重读
                self._vsec, self._vsec_ts = None, 0.0
            raise BrainHTTPError(int(exc.code), url, body) from exc

    def embed_texts(self, texts: list[str], *, timeout: Optional[float] = None) -> list[list[float]]:
        key = self._vector_secrets().get("embed")
        if not key:
            raise BrainError("向量层未就绪：qdrant 容器中未找到可用 key")
        url = str(self.cfg["embed_base_url"]).rstrip("/") + "/embeddings"
        limit = int(self.cfg.get("vector_text_max") or 12000)
        payload = {"model": str(self.cfg["embed_model"]),
                   "input": [str(t)[:limit] for t in texts],
                   "encoding_format": "float"}
        _, data = self._http_json("POST", url, headers={"Authorization": f"Bearer {key}"},
                                  payload=payload,
                                  timeout=timeout or float(self.cfg.get("embed_timeout") or 60))
        items = sorted(data.get("data") or [], key=lambda d: int(d.get("index") or 0))
        vecs = [d.get("embedding") or [] for d in items]
        if len(vecs) != len(texts) or any(not v for v in vecs):
            raise BrainError(f"embedding 响应异常（期望 {len(texts)} 条向量）")
        return vecs

    def _qdrant_headers(self) -> dict:
        key = self._vector_secrets().get("qdrant")
        return {"api-key": key} if key else {}

    def _qdrant(self, method: str, path: str, payload: Optional[dict] = None,
                timeout: float = 15) -> dict:
        url = str(self.cfg["qdrant_url"]).rstrip("/") + path
        _, data = self._http_json(method, url, headers=self._qdrant_headers(),
                                  payload=payload, timeout=timeout)
        return data

    def qdrant_ensure_collection(self) -> None:
        name = str(self.cfg["qdrant_collection"])
        dim = int(self.cfg["embed_dim"])
        try:
            data = self._qdrant("GET", f"/collections/{name}")
        except BrainHTTPError as exc:
            if exc.code != 404:
                raise
            self._qdrant("PUT", f"/collections/{name}",
                         {"vectors": {"size": dim, "distance": "Cosine"}}, timeout=30)
            logger.info("brain: 已创建 Qdrant 集合 %s（%s 维, Cosine）", name, dim)
            return
        vectors = (((data.get("result") or {}).get("config") or {})
                   .get("params") or {}).get("vectors")
        size = vectors.get("size") if isinstance(vectors, dict) else None
        if size and int(size) != dim:
            raise BrainError(f"Qdrant 集合 {name} 维度为 {size}，与配置 {dim} 不一致："
                             "需换集合名或清库重建")

    def qdrant_upsert(self, points: list[dict]) -> None:
        name = str(self.cfg["qdrant_collection"])
        self._qdrant("PUT", f"/collections/{name}/points?wait=true", {"points": points}, timeout=30)

    def qdrant_delete_points(self, ids: list[int]) -> None:
        if not ids:
            return
        name = str(self.cfg["qdrant_collection"])
        self._qdrant("POST", f"/collections/{name}/points/delete?wait=true",
                     {"points": [int(i) for i in ids]}, timeout=30)

    def qdrant_query(self, vector: list[float], topk: int) -> list[dict]:
        name = str(self.cfg["qdrant_collection"])
        data = self._qdrant("POST", f"/collections/{name}/points/query",
                            {"query": vector, "limit": int(topk), "with_payload": False},
                            timeout=20)
        pts = ((data.get("result") or {}).get("points")) or []
        return [{"id": int(p["id"]), "score": float(p.get("score") or 0.0)} for p in pts]

    def qdrant_get_vectors(self, ids: list[int]) -> dict[int, list[float]]:
        """按 id 取 Qdrant 里已有的向量（不重新调 embed API）；缺失的 id 不在返回结果里。"""
        name = str(self.cfg["qdrant_collection"])
        out: dict[int, list[float]] = {}
        for i in range(0, len(ids), 256):
            chunk = [int(x) for x in ids[i:i + 256]]
            data = self._qdrant("POST", f"/collections/{name}/points",
                                {"ids": chunk, "with_vector": True, "with_payload": False},
                                timeout=30)
            for p in (data.get("result") or []):
                vec = p.get("vector")
                if isinstance(vec, dict):        # 命名向量兜底
                    vec = next((v for v in vec.values() if isinstance(v, list)), None)
                if isinstance(vec, list) and vec:
                    out[int(p["id"])] = [float(x) for x in vec]
        return out

    def _pending_memories(self, limit: int, *, force: bool = False,
                          cursor: int = 0) -> list[dict]:
        self.ensure_schema()
        base = ("SELECT m.id, m.content, m.kind, m.tags, m.importance, m.machine, m.content_hash "
                "FROM brain_memories m ")
        if force:
            sql = base + ("WHERE m.active = 1 AND m.id > %s "
                          "ORDER BY m.id ASC LIMIT %s")
            return list(self._exec(sql, (int(cursor), int(limit)), fetch=True))
        sql = (base +
               "LEFT JOIN brain_vector_index v ON v.mem_id = m.id "
               "WHERE m.active = 1 AND ("
               "v.mem_id IS NULL "
               "OR (v.status = 'ok' AND (v.model <> %s OR v.content_hash IS NULL "
               "OR v.content_hash <> m.content_hash)) "
               "OR (v.status = 'error' AND v.updated_at < (NOW() - INTERVAL 30 MINUTE))"
               ") ORDER BY m.updated_at DESC, m.id DESC LIMIT %s")
        return list(self._exec(sql, (str(self.cfg["embed_model"])[:64], int(limit)), fetch=True))

    def _mark_vector(self, mem_id: int, *, status: str, model: str = "", dim: int = 0,
                     content_hash: Optional[str] = None, error: str = "") -> None:
        # 库内时间统一 UTC；本机时区的 time.strftime 会让 vector-status 的 last_ok 错位
        embedded_at = (datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                       if status == "ok" else None)
        self._exec(
            "INSERT INTO brain_vector_index (mem_id, status, model, dim, content_hash, error, embedded_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE status = VALUES(status), model = VALUES(model), dim = VALUES(dim), "
            "content_hash = VALUES(content_hash), error = VALUES(error), embedded_at = VALUES(embedded_at)",
            (int(mem_id), status[:16], str(model)[:64], int(dim), content_hash, str(error)[:255],
             embedded_at))

    def drain_vectors(self, *, max_items: int = 0, force: bool = False) -> dict:
        """把未向量化的记忆推入 Qdrant（跨进程用 GET_LOCK 串行，失败带节流重试）。"""
        stats = {"enabled": bool(int(self.cfg.get("vector_enabled") or 0)),
                 "embedded": 0, "errors": 0, "deleted": 0, "remaining": 0}
        if not stats["enabled"]:
            return stats
        got = False
        try:
            rows = self._exec("SELECT GET_LOCK('brain_vector_drain', 0) AS got", fetch=True)
            got = bool(rows and rows[0].get("got"))
            if not got:
                # 锁被另一路 drain 占用（可能正卡在一次慢速 embed 上）：不再硬跳过，
                # 直接继续——本管线幂等（同 id 覆盖写），最坏只是重复一次嵌入。
                logger.info("brain: drain 锁被占用，仍然继续（幂等处理）")
            self.qdrant_ensure_collection()
            model = str(self.cfg["embed_model"])
            batch_n = max(1, int(self.cfg.get("vector_drain_batch") or 8))
            budget = int(max_items) if int(max_items) > 0 else 10 ** 9
            cursor = 0
            while budget > 0:
                pending = self._pending_memories(min(batch_n, budget), force=force, cursor=cursor)
                if not pending:
                    break
                if force:
                    cursor = max(int(r["id"]) for r in pending)
                texts = [str(r["content"]) for r in pending]
                try:
                    vecs: list = list(self.embed_texts(texts))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("brain: 批量向量化失败（%s），逐条重试", str(exc)[:160])
                    vecs = []
                    for t in texts:
                        try:
                            vecs.append(self.embed_texts([t])[0])
                        except Exception as exc2:  # noqa: BLE001
                            logger.warning("brain: 单条向量化失败: %s", str(exc2)[:160])
                            vecs.append(None)
                ok_pairs = []
                for row, vec in zip(pending, vecs):
                    if vec:
                        ok_pairs.append((row, vec))
                    else:
                        self._mark_vector(int(row["id"]), status="error", model=model,
                                          error="embed 失败")
                        stats["errors"] += 1
                if ok_pairs:
                    points = [{
                        "id": int(row["id"]),
                        "vector": vec,
                        "payload": {"kind": row["kind"], "importance": int(row["importance"]),
                                    "tags": row["tags"], "machine": row["machine"],
                                    "preview": str(row["content"])[:200]},
                    } for row, vec in ok_pairs]
                    try:
                        self.qdrant_upsert(points)
                        for row, vec in ok_pairs:
                            self._mark_vector(int(row["id"]), status="ok", model=model, dim=len(vec),
                                              content_hash=row["content_hash"])
                            stats["embedded"] += 1
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("brain: Qdrant 写入失败: %s", str(exc)[:160])
                        for row, _vec in ok_pairs:
                            self._mark_vector(int(row["id"]), status="error", model=model,
                                              error=f"qdrant: {str(exc)[:200]}")
                        stats["errors"] += len(ok_pairs)
                budget -= len(pending)
                logger.info("brain: 向量化批次 +%s（成功累计 %s / 失败累计 %s）",
                            len(pending), stats["embedded"], stats["errors"])
                if len(pending) < batch_n:
                    break
            # 清理已失活记忆的向量
            gone = self._exec(
                "SELECT v.mem_id FROM brain_vector_index v "
                "JOIN brain_memories m ON m.id = v.mem_id WHERE m.active = 0 LIMIT 50", fetch=True)
            if gone:
                ids = [int(g["mem_id"]) for g in gone]
                try:
                    self.qdrant_delete_points(ids)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("brain: 清理 Qdrant 失活向量失败: %s", exc)
                marks = ", ".join(["%s"] * len(ids))
                self._exec(f"DELETE FROM brain_vector_index WHERE mem_id IN ({marks})", tuple(ids))
                stats["deleted"] = len(ids)
            row = self._exec(
                "SELECT COUNT(*) AS c FROM brain_memories m LEFT JOIN brain_vector_index v "
                "ON v.mem_id = m.id WHERE m.active = 1 AND (v.mem_id IS NULL OR v.status <> 'ok' "
                "OR v.model <> %s OR (v.content_hash IS NULL OR v.content_hash <> m.content_hash))",
                (model[:64],), fetch=True)
            stats["remaining"] = int(row[0]["c"] or 0)
        finally:
            if got:
                try:
                    self._exec("SELECT RELEASE_LOCK('brain_vector_drain')")
                except Exception:  # noqa: BLE001
                    pass
        return stats

    def _query_embedding(self, query: str, *, timeout: Optional[float] = None) -> list[float]:
        """查询向量化：Redis 缓存（默认 1 小时），降低重复查询的延迟与波动。"""
        cache_key = self._rk("qemb", hashlib.sha1(
            (str(self.cfg.get("embed_model")) + "\x00" + query).encode("utf-8")).hexdigest())
        try:
            raw = self.redis().get(cache_key)
            if raw:
                val = json.loads(raw)
                if isinstance(val, list) and val:
                    return [float(x) for x in val]
        except Exception:  # noqa: BLE001
            pass
        vec = self.embed_texts(
            [query], timeout=timeout or float(self.cfg.get("vector_query_timeout") or 20))[0]
        try:
            self.redis().set(cache_key, json.dumps([round(float(x), 6) for x in vec]),
                             ex=int(self.cfg.get("vector_qemb_ttl") or 3600))
        except Exception:  # noqa: BLE001
            pass
        return vec

    def vector_search_memories(self, query: str, *, limit: int = 8,
                               kind: Optional[str] = None) -> list[dict[str, Any]]:
        if not int(self.cfg.get("vector_enabled") or 0) or not (query or "").strip():
            return []
        vec = self._query_embedding(query)
        hits = self.qdrant_query(vec, int(self.cfg.get("vector_topk") or 12))
        cutoff = float(self.cfg.get("vector_min_score") or 0.0)
        hits = [h for h in hits if h["score"] >= cutoff]
        if not hits:
            return []
        ids = [h["id"] for h in hits]
        marks = ", ".join(["%s"] * len(ids))
        sql = ("SELECT id, kind, memory_type, content, tags, importance, source, session_id, machine, "
               "created_at, updated_at "
               f"FROM brain_memories WHERE active = 1 AND superseded_by IS NULL AND id IN ({marks}) ")
        params: list = list(ids)
        if kind:
            sql += "AND kind = %s "
            params.append(kind)
        self.ensure_schema()
        rows = {int(r["id"]): r for r in self._exec(sql, tuple(params), fetch=True)}
        out = []
        for h in hits:
            r = rows.get(h["id"])
            if r:
                r = dict(r)
                r["vscore"] = round(h["score"], 4)
                r["via"] = "vec"
                out.append(r)
        return out[: int(limit)]

    def search_memories_hybrid(self, query: str, *, limit: int = 5,
                               kind: Optional[str] = None) -> list[dict[str, Any]]:
        """关键词 + 向量 RRF 融合；向量任一环节失败自动回落关键词。"""
        kw = self.search_memories(query, limit=limit, kind=kind)
        try:
            vec = self.vector_search_memories(
                query, limit=int(self.cfg.get("vector_topk") or 12), kind=kind)
        except Exception as exc:  # noqa: BLE001
            logger.warning("brain: 向量检索失败，回落纯关键词: %s", str(exc)[:200])
            vec = []
        if not vec:
            for m in kw:
                m.setdefault("via", "kw")
            return kw
        k = float(self.cfg.get("vector_rrf_k") or 60)
        scores: dict[int, float] = {}
        items: dict[int, dict] = {}
        kw_ids, vec_ids = set(), set()
        for rank, m in enumerate(kw):
            mid = int(m["id"])
            kw_ids.add(mid)
            scores[mid] = scores.get(mid, 0.0) + 1.0 / (k + rank + 1)
            items.setdefault(mid, m)
        for rank, m in enumerate(vec):
            mid = int(m["id"])
            vec_ids.add(mid)
            scores[mid] = scores.get(mid, 0.0) + 1.0 / (k + rank + 1)
            if mid in items:
                items[mid]["vscore"] = m.get("vscore")
            else:
                items[mid] = m
        # 时间衰减：RRF 打分完成后、排序之前，对白名单层（默认 episodic）乘衰减系数
        if int(self.cfg.get("decay_enabled") or 0):
            self._apply_decay(items, scores)
        ordered = sorted(items.items(),
                         key=lambda kv: (-scores[kv[0]], -int(kv[1].get("importance") or 0)))
        out = []
        for mid, m in ordered:
            m = dict(m)
            m["via"] = ("both" if (mid in kw_ids and mid in vec_ids)
                        else ("vec" if mid in vec_ids else "kw"))
            out.append(m)
        if out and int(self.cfg.get("rerank_enabled") or 0):
            try:
                out = self.rerank(query, out, limit=int(limit))
            except Exception as exc:  # noqa: BLE001
                logger.warning("brain: rerank 失败，保持 RRF 顺序: %s", str(exc)[:200])
        return out[: int(limit)]

    def _apply_decay(self, items: dict[int, dict], scores: dict[int, float]) -> None:
        """按记忆分层做时间衰减（原地改 scores）；年龄取不到时 factor=1，不丢结果。"""
        layers = {t.strip() for t in str(self.cfg.get("decay_types") or "").split(",") if t.strip()}
        try:
            half_life = float(self.cfg.get("decay_half_life_days") or 0)
        except (TypeError, ValueError):
            half_life = 0.0
        if not layers or half_life <= 0:
            return
        now_ts = time.time()
        for mid, score in list(scores.items()):
            item = items.get(mid) or {}
            layer = str(item.get("memory_type") or "").strip() or _memory_type(str(item.get("kind") or ""))
            if layer not in layers:
                continue
            age = _age_days(item.get("updated_at"), now_ts)
            if age is None:
                age = _age_days(item.get("created_at"), now_ts)
            if age is None:
                continue
            scores[mid] = score * (0.5 ** (age / half_life))

    # -------------------------------------------------- rerank（服务端可选重排）

    def rerank(self, query: str, candidates: list[dict[str, Any]], *,
               limit: int) -> list[dict[str, Any]]:
        """用硅基流动 rerank 重排候选（只送前 rerank_topn 条；key 复用向量层 embed key）。

        失败（无 key / 网络 / 响应异常）直接抛异常，由调用方兜底回落原顺序。
        """
        head = list(candidates)[: max(1, int(self.cfg.get("rerank_topn") or 20))]
        if not head:
            return []
        key = self._vector_secrets().get("embed")
        if not key:
            raise BrainError("rerank 不可用：qdrant 容器中未找到可用 key")
        payload = {
            "model": str(self.cfg.get("rerank_model")),
            "query": str(query or ""),
            "documents": [str(c.get("content") or "") for c in head],
            "top_n": len(head),
            "return_documents": False,
        }
        _, data = self._http_json(
            "POST", str(self.cfg.get("rerank_url")),
            headers={"Authorization": f"Bearer {key}"}, payload=payload,
            timeout=float(self.cfg.get("rerank_timeout") or 20))
        try:
            min_score = float(self.cfg.get("rerank_min_score") or 0.0)
        except (TypeError, ValueError):
            min_score = 0.0
        out: list[dict[str, Any]] = []
        for item in sorted(data.get("results") or [],
                           key=lambda r: -float(r.get("relevance_score") or 0.0)):
            idx = int(item.get("index") or 0)
            if not 0 <= idx < len(head):
                continue
            score = float(item.get("relevance_score") or 0.0)
            if score < min_score:
                continue
            m = dict(head[idx])
            m["rerank_score"] = round(score, 6)
            out.append(m)
        return out[: max(1, int(limit))]

    # -------------------------------------------------- 取代关系（可回滚）

    def detect_supersedes(self, *, dry_run: bool = True, threshold: float = 0.90,
                          limit: int = 200) -> dict[str, Any]:
        """同一 memory_type 内两两比对，把语义重叠的旧记忆标记为「已被新记忆取代」。

        只使用 Qdrant 里已存在的向量（不重新调 embed API），没有向量的行跳过；
        胜出方：created_at 更新者（相同则 id 更大者）；一条旧记忆只标记一次，不会成环。
        dry_run=True 只返回将要发生的变更，不写库。
        """
        self.ensure_schema()
        try:
            th = float(threshold)
        except (TypeError, ValueError):
            th = 0.90
        rows = [dict(r) for r in self._exec(
            "SELECT id, kind, memory_type, content, created_at, updated_at FROM brain_memories "
            "WHERE active = 1 AND superseded_by IS NULL ORDER BY created_at ASC, id ASC LIMIT %s",
            (max(1, int(limit)),), fetch=True)]
        out: dict[str, Any] = {"pairs": [], "applied": 0, "skipped": 0, "dry_run": bool(dry_run)}
        if len(rows) < 2:
            return out
        try:
            vectors = self.qdrant_get_vectors([int(r["id"]) for r in rows])
        except Exception as exc:  # noqa: BLE001
            logger.warning("brain: 读取 Qdrant 向量失败，本次不做取代检测: %s", str(exc)[:200])
            out["skipped"] = len(rows)
            out["error"] = str(exc)[:200]
            return out
        groups: dict[str, list[tuple[dict, list[float]]]] = {}
        for r in rows:
            vec = vectors.get(int(r["id"]))
            if not vec:
                out["skipped"] += 1     # 库里没有该 id 的向量：跳过
                continue
            layer = str(r.get("memory_type") or "").strip() or _memory_type(str(r.get("kind") or ""))
            groups.setdefault(layer, []).append((r, vec))
        cands: list[tuple[float, dict, dict, str]] = []
        for layer, group in groups.items():
            for i in range(len(group) - 1):
                ri, vi = group[i]
                for j in range(i + 1, len(group)):
                    rj, vj = group[j]
                    sim = _cosine(vi, vj)
                    if sim < th:
                        continue
                    # cands 元组语义 = (sim, lose, win, layer)：胜出方是时间更新者
                    # （created_at 相同则 id 更大者），旧的那条才是 lose。
                    if (ri["created_at"], int(ri["id"])) > (rj["created_at"], int(rj["id"])):
                        cands.append((sim, rj, ri, layer))
                    else:
                        cands.append((sim, ri, rj, layer))
        cands.sort(key=lambda c: (-c[0], int(c[1]["id"])))
        marked: set[int] = set()
        for sim, lose, win, layer in cands:
            lid, wid = int(lose["id"]), int(win["id"])
            if lid in marked:
                continue                # 一条旧记忆只标记一次（同时保证无环）
            marked.add(lid)
            out["pairs"].append({
                "id": lid, "superseded_by": wid, "similarity": round(float(sim), 4),
                "memory_type": layer, "kind": str(lose.get("kind") or ""),
                "content": _clip(str(lose.get("content") or ""), 160),
                "winner_kind": str(win.get("kind") or ""),
                "winner_content": _clip(str(win.get("content") or ""), 160),
            })
            if not dry_run:
                # 显式 updated_at = updated_at：标记取代关系不该刷新记忆的“最后更新/年龄”信号
                rowcount, _ = self._exec(
                    "UPDATE brain_memories SET superseded_by = %s, superseded_at = NOW(), "
                    "updated_at = updated_at "
                    "WHERE id = %s AND active = 1 AND superseded_by IS NULL", (wid, lid))
                if rowcount:
                    out["applied"] += 1
        if out["applied"]:
            logger.info("brain: 取代检测标记 %s 条旧记忆（阈值 %s）", out["applied"], th)
        return out

    def unsupersede(self, mem_id: int) -> dict[str, Any]:
        """撤销取代标记（可回滚）：把 superseded_by / superseded_at 置回 NULL。"""
        self.ensure_schema()
        rowcount, _ = self._exec(
            "UPDATE brain_memories SET superseded_by = NULL, superseded_at = NULL, "
            "updated_at = updated_at "
            "WHERE id = %s AND superseded_by IS NOT NULL", (int(mem_id),))
        return {"id": int(mem_id), "cleared": rowcount}

    def vector_status(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "enabled": bool(int(self.cfg.get("vector_enabled") or 0)),
            "embed_model": str(self.cfg.get("embed_model")),
            "embed_dim": int(self.cfg.get("embed_dim") or 0),
            "qdrant": f"{self.cfg.get('qdrant_url')} / {self.cfg.get('qdrant_collection')}",
            "key_source": f"qdrant 配置文件 {self.cfg.get('qdrant_config_file')}（挂载进容器；不落 Hermes 配置）",
        }
        sec = self._vector_secrets()
        out["keys_found"] = {"embed": bool(sec.get("embed")), "qdrant": bool(sec.get("qdrant"))}
        self.ensure_schema()
        row = self._exec(
            "SELECT "
            "(SELECT COUNT(*) FROM brain_memories WHERE active = 1) AS active, "
            "(SELECT COUNT(*) FROM brain_vector_index v JOIN brain_memories m ON m.id = v.mem_id "
            " WHERE m.active = 1 AND v.status = 'ok') AS ok, "
            "(SELECT COUNT(*) FROM brain_vector_index v JOIN brain_memories m ON m.id = v.mem_id "
            " WHERE m.active = 1 AND v.status = 'error') AS err, "
            "(SELECT COUNT(*) FROM brain_memories m LEFT JOIN brain_vector_index v "
            " ON v.mem_id = m.id WHERE m.active = 1 AND v.mem_id IS NULL) AS missing, "
            "(SELECT COUNT(*) FROM brain_vector_index v JOIN brain_memories m ON m.id = v.mem_id "
            " WHERE m.active = 1 AND v.status = 'ok' AND (v.model <> %s OR v.content_hash IS NULL "
            " OR v.content_hash <> m.content_hash)) AS stale, "
            "(SELECT MAX(embedded_at) FROM brain_vector_index WHERE status = 'ok') AS last_ok",
            (str(self.cfg.get("embed_model"))[:64],), fetch=True)[0]
        out["memories"] = {k: (v if k == "last_ok" else int(v or 0)) for k, v in row.items()}
        try:
            data = self._qdrant("GET", f"/collections/{self.cfg['qdrant_collection']}", timeout=8)
            res = data.get("result") or {}
            out["qdrant_state"] = {"status": res.get("status"), "points": res.get("points_count")}
        except Exception as exc:  # noqa: BLE001
            out["qdrant_state"] = {"error": str(exc)[:200]}
        return out

    # -------------------------------------------------- 运维

    # 维护循环（不依赖 systemd）：
    #   * 每 maintenance_interval_min 分钟巡检一轮；首轮最多等 60 秒（短命进程早已退出）；
    #   * 只有距 brain_meta.last_prune 超过 prune_every_days 天才真正清理；
    #   * 清理前取 GET_LOCK('brain_prune', 0)，多进程互斥，拿不到就跳过；
    #   * 线程是 daemon，且任何异常都只记 warning，绝不影响主路径/进程退出。

    def _meta_get(self, key: str) -> Optional[str]:
        """读 brain_meta 单键（缺失返回 None，视为“从未发生”）。"""
        self.ensure_schema()
        rows = self._exec(
            "SELECT meta_value FROM brain_meta WHERE meta_key = %s", (key,), fetch=True)
        return str(rows[0]["meta_value"]) if rows else None

    def _meta_set(self, key: str, value: str) -> None:
        self.ensure_schema()
        self._exec(
            "INSERT INTO brain_meta (meta_key, meta_value) VALUES (%s, %s) "
            "ON DUPLICATE KEY UPDATE meta_value = VALUES(meta_value)",
            (key, str(value)))

    def start_maintenance(self) -> None:
        """惰性启动维护线程（每进程一个；daemon，绝不阻塞进程退出）。"""
        if not int(self.cfg.get("maintenance_enabled") or 0):
            return
        with self._maint_lock:
            if self._maint_started:
                return
            self._maint_started = True
        try:
            threading.Thread(target=self._maintenance_loop,
                             name="brain-maintenance", daemon=True).start()
        except Exception as exc:  # noqa: BLE001
            with self._maint_lock:
                self._maint_started = False
            logger.warning("brain: 启动维护线程失败: %s", exc)

    def _maintenance_loop(self) -> None:
        interval = max(0.01, float(self.cfg.get("maintenance_interval_min") or 60)) * 60.0
        delay = min(interval, 60.0)   # 首轮稍后开始，不抢占启动路径
        while True:
            try:
                time.sleep(delay)
                self._maintenance_tick()
            except Exception as exc:  # noqa: BLE001
                # 线程绝不因单轮失败退出，更不向上抛。
                logger.warning("brain: 维护循环异常（继续下一轮）: %s", exc)
            delay = interval

    def _maintenance_tick(self) -> None:
        if not int(self.cfg.get("maintenance_enabled") or 0):
            return
        every = float(self.cfg.get("prune_every_days") or 0)
        if every > 0:
            last = self._meta_get("last_prune")
            if last:
                try:
                    prev = datetime.strptime(last, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                except ValueError:
                    prev = None
                if prev is not None:
                    age_days = (datetime.now(timezone.utc) - prev).total_seconds() / 86400.0
                    if age_days < every:
                        return
        rows = self._exec("SELECT GET_LOCK('brain_prune', 0) AS got", fetch=True)
        if not (rows and rows[0].get("got")):
            logger.debug("brain: 维护清理跳过（另一进程正在清理）")
            return
        try:
            result = self.prune(dry_run=False)
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            self._meta_set("last_prune", now)
            next_txt = f"{int(every)} 天后" if every > 0 else "每轮巡检"
            logger.info("brain: 维护清理完成 turns=%s sessions=%s archive=%s 下次 %s",
                        result.get("turns", 0), result.get("sessions", 0),
                        result.get("archives", 0), next_txt)
        finally:
            try:
                self._exec("SELECT RELEASE_LOCK('brain_prune')")
            except Exception:  # noqa: BLE001
                pass

    # -------------------------------------------------- 结论蒸馏（distill）
    #
    # 进程内第 3 个后台线程（与向量 drainer、维护循环同款）：
    #   * 每 distill_interval_min 分钟巡检；首轮最多等 60 秒（短命进程早已退出）；
    #   * 把水位之后的新对话轮次交给 LLM 蒸馏成“跨周月仍成立”的结论；
    #   * 写前与既有记忆做向量对账：高相似跳过、灰区一次 LLM 裁决、低相似新增；
    #   * 取 GET_LOCK('brain_distill', 0) 多进程互斥，brain_meta 记账，零 systemd；
    #   * distill_enabled 默认 0：不显式打开时零行为变化、零 API 花费；
    #   * 任何异常只 warning + 计数，绝不向上抛、绝不影响记忆读写主路径。
    #
    # key 只从环境变量/.env 解析（SECRET_ENV_VARS），日志/CLI 只报告“已配置/缺失”。

    _DISTILL_SYSTEM_PROMPT = (
        "你是长期记忆蒸馏器。输入是一批对话轮次（含 id、时间、来源）与当前 UTC 时间。\n"
        "提取“跨周、跨月仍然成立”的结论：事实、偏好、决策、目标、稳定的行为模式"
        "（归纳类 kind=insight）、进行中项目的稳定状态。\n"
        "排除：一次性任务细节、工具输出、寒暄、临时状态；"
        "凭证/密钥/口令/token 一律禁止出现在输出里；宁少而准。\n"
        "每条结论必须自足可读（脱离上下文也能看懂）。\n"
        "只输出严格 JSON 数组，不要任何多余文字：\n"
        "[{\"content\":\"...\",\"kind\":\"fact|preference|decision|insight\","
        "\"importance\":1-10,\"tags\":[\"≤3个\"],\"evidence_turn_ids\":[...]}]\n"
        "最多 {max_items} 条；没有可提炼的结论就输出 []。"
    )

    _DISTILL_RECONCILE_PROMPT = (
        "你是记忆去重裁决器。输入是一批候选结论，每条带 index、content 与相似邻居列表"
        "（邻居含 id、余弦相似度 sim、内容）。逐条裁决：\n"
        "  * skip：邻居已表达同一结论且没有新增信息；\n"
        "  * update：同一条事实/偏好出现了更新版本，需由新结论取代旧记忆"
        "（target_id 必须取自该候选的邻居 id）；\n"
        "  * new：与所有邻居都不是同一结论。\n"
        "target_id 只能来自该候选自己的邻居列表，否则按 new。\n"
        "只输出严格 JSON，无多余文字："
        "{\"decisions\":[{\"index\":0,\"decision\":\"skip|new|update\",\"target_id\":0}]}"
    )

    # 写入双保险：LLM 已按提示词排除凭证，落库前再兜底过滤一次。
    _DISTILL_SENSITIVE_RE = re.compile(
        r'(sk-[A-Za-z0-9_\-]{8,}|Bearer\s+[A-Za-z0-9._\-]{16,}|'
        r'(?i:(password|passwd|secret|token|密码|密钥|口令)\s*[:=：]\s*\S{6,}))')

    def _meta_incr(self, key: str, amount: int = 1) -> None:
        """brain_meta 计数自增（缺失即从 amount 开始；并发安全）。"""
        self.ensure_schema()
        self._exec(
            "INSERT INTO brain_meta (meta_key, meta_value) VALUES (%s, %s) "
            "ON DUPLICATE KEY UPDATE meta_value = CAST(meta_value AS UNSIGNED) + %s",
            (key, str(int(amount)), int(amount)))

    def start_distill(self) -> None:
        """惰性启动蒸馏线程（每进程一个；daemon，绝不阻塞进程退出）。"""
        if not int(self.cfg.get("distill_enabled") or 0):
            return
        with self._distill_lock:
            if self._distill_started:
                return
            self._distill_started = True
        try:
            threading.Thread(target=self._distill_loop,
                             name="brain-distill", daemon=True).start()
        except Exception as exc:  # noqa: BLE001
            with self._distill_lock:
                self._distill_started = False
            logger.warning("brain: 启动蒸馏线程失败: %s", exc)

    def _distill_loop(self) -> None:
        interval = max(0.01, float(self.cfg.get("distill_interval_min") or 60)) * 60.0
        delay = min(interval, 60.0)   # 首轮稍后开始，不抢占启动路径
        while True:
            try:
                time.sleep(delay)
                self._distill_tick()
            except Exception as exc:  # noqa: BLE001
                logger.warning("brain: 蒸馏循环异常（继续下一轮）: %s", exc)
            delay = interval

    def distill_status(self) -> dict[str, Any]:
        """蒸馏状态：开关 / 水位 / 待处理轮次 / 上次时间 / 五项计数 / API 配置（key 只报存在与否）。"""
        wm_txt = self._meta_get("distill_watermark")
        try:
            wm = int(wm_txt) if wm_txt not in (None, "") else None
        except (TypeError, ValueError):
            wm = None
        pending = 0
        if wm is not None:
            row = self._exec(
                "SELECT COUNT(*) AS c FROM brain_turns WHERE id > %s", (wm,), fetch=True)[0]
            pending = int(row["c"] or 0)
        counters: dict[str, int] = {}
        for key in ("distill_runs", "distill_added", "distill_updated",
                    "distill_skipped", "distill_errors"):
            try:
                counters[key] = int(self._meta_get(key) or 0)
            except (TypeError, ValueError):
                counters[key] = 0
        return {
            "enabled": bool(int(self.cfg.get("distill_enabled") or 0)),
            "interval_min": int(self.cfg.get("distill_interval_min") or 60),
            "watermark": wm,
            "pending": pending,
            "last_distill": self._meta_get("last_distill"),
            "counters": counters,
            "api_base_url": str(self.cfg.get("distill_api_base_url") or ""),
            "api_model": str(self.cfg.get("distill_api_model") or ""),
            "api_key_present": bool(str(self.cfg.get("distill_api_key") or "").strip()),
        }

    def _distill_tick(self, force: bool = False, turns_override: int = 0,
                      dry_run: bool = False) -> dict[str, Any]:
        """一轮蒸馏。force 无视开关（手动命令用）；dry_run 不取锁、不写库、水位不动。"""
        result: dict[str, Any] = {
            "ok": True, "reason": "", "dry_run": bool(dry_run),
            "turns": [], "items": [], "added": [], "updated": [],
            "skipped": 0, "errors": 0, "watermark": None,
        }
        if not force and not int(self.cfg.get("distill_enabled") or 0):
            result["reason"] = "disabled"
            logger.debug("brain: 蒸馏未启用，跳过")
            return result
        self.ensure_schema()
        cfg = self.cfg
        max_turns = max(1, int(cfg.get("distill_max_turns") or 16))
        turn_chars = max(100, int(cfg.get("distill_turn_chars") or 1000))
        override = max(0, int(turns_override or 0))

        # 1) 水位：首次启用只记当前最大 id（不回灌历史），绝不拿历史对话突然烧 API。
        wm_txt = self._meta_get("distill_watermark")
        try:
            wm = int(wm_txt) if wm_txt not in (None, "") else 0
        except (TypeError, ValueError):
            wm = 0
        if not override and wm_txt in (None, ""):
            if dry_run:
                logger.info("brain: 蒸馏水位未初始化，dry-run 按 0 计（不写库）")
            else:
                row = self._exec(
                    "SELECT COALESCE(MAX(id), 0) AS m FROM brain_turns", fetch=True)[0]
                wm = int(row["m"] or 0)
                self._meta_set("distill_watermark", str(wm))
                logger.info("brain: 蒸馏水位初始化 id=%s（不回灌历史）", wm)
                result["reason"] = "initialized"
                result["watermark"] = wm
                return result
        result["watermark"] = wm
        now = datetime.now(timezone.utc)

        # 2) 取批：override 取最近 N 轮（升序）；否则按水位 + 静默窗口/积压门槛。
        capacity = max_turns
        if override:
            raw = self._exec(
                "SELECT id, session_id, machine, platform, user_content, assistant_content, ts "
                "FROM brain_turns ORDER BY id DESC LIMIT %s", (override,), fetch=True)
            raw = list(reversed(raw))
            capacity = override
        else:
            cutoff = (now - timedelta(seconds=120)).strftime("%Y-%m-%d %H:%M:%S")
            stat = self._exec(
                "SELECT COUNT(*) AS c, MIN(ts) AS oldest FROM brain_turns "
                "WHERE id > %s AND ts < %s", (wm, cutoff), fetch=True)[0]
            count = int(stat["c"] or 0)
            if count <= 0:
                result["reason"] = "no_turns"
                return result
            min_turns = max(1, int(cfg.get("distill_min_turns") or 8))
            if count < min_turns:
                oldest = stat.get("oldest")
                stalled = False
                if oldest is not None:
                    try:
                        age_h = (now - oldest.replace(tzinfo=timezone.utc)).total_seconds() / 3600.0
                        stalled = age_h >= float(cfg.get("distill_stagnation_hours") or 24)
                    except (TypeError, ValueError):
                        stalled = False
                if not stalled:
                    result["reason"] = "below_threshold"
                    return result
            limit = max_turns * 2 + 10
            raw = self._exec(
                "SELECT id, session_id, machine, platform, user_content, assistant_content, ts "
                "FROM brain_turns WHERE id > %s AND ts < %s ORDER BY id LIMIT %s",
                (wm, cutoff, limit), fetch=True)

        rows: list[dict[str, Any]] = []
        for r in raw:
            user = str(r.get("user_content") or "")
            if not user.strip() or user.startswith("[IMPORTANT"):
                continue
            rows.append({
                "id": int(r["id"]),
                "session_id": str(r.get("session_id") or ""),
                "machine": str(r.get("machine") or ""),
                "platform": str(r.get("platform") or ""),
                "ts": r.get("ts"),
                "user_content": _clip(user, turn_chars),
                "assistant_content": _clip(str(r.get("assistant_content") or ""), turn_chars),
            })
            if len(rows) >= capacity:
                break
        result["turns"] = [r["id"] for r in rows]
        if not rows:
            result["reason"] = "no_turns"
            return result

        # 3) dry-run：不取锁、不写库、水位不动，但仍然跑真实提取 + 对账。
        lock_held = False
        if not dry_run:
            got = self._exec("SELECT GET_LOCK('brain_distill', 0) AS got", fetch=True)
            if not (got and got[0].get("got")):
                logger.debug("brain: 蒸馏跳过（另一进程正在蒸馏）")
                result["reason"] = "locked"
                return result
            lock_held = True
        try:
            try:
                candidates = self._distill_extract(rows)
            except Exception as exc:  # noqa: BLE001
                result["ok"] = False
                result["reason"] = "extract_failed"
                result["errors"] = 1
                result["error"] = str(exc)[:300]
                if not dry_run:
                    self._meta_incr("distill_errors", 1)
                logger.warning("brain: 蒸馏提取失败（不推水位）: %s", str(exc)[:200])
                return result

            items = self._distill_candidates(candidates, dry_run=dry_run)
            result["items"] = items
            result["skipped"] = sum(1 for it in items if it["decision"] == "skip")
            if dry_run:
                result["reason"] = "dry_run"
                return result

            added_ids = [int(it["id"]) for it in items
                         if it["decision"] in ("new", "update") and it.get("id")]
            updated_ids = [int(it["id"]) for it in items
                           if it["decision"] == "update" and it.get("id")]
            batch_max = max(r["id"] for r in rows)
            # 水位推进：无论本轮写入 0 条还是 N 条都推进，避免同一批反复烧 API。
            # 例外：手动 --now 取的是「最近 N 轮」，如果它与旧水位之间还有积压轮次，
            # 直接推到 batch_max 会让这些轮次永远不再被自动蒸馏；此时保持原水位。
            # 另外绝不把水位往回推（其它进程可能已经推到更后面）。
            advance = batch_max > wm
            if advance and override:
                first_processed = int(raw[0]["id"]) if raw else batch_max
                gap = self._exec(
                    "SELECT COUNT(*) AS c FROM brain_turns WHERE id > %s AND id < %s",
                    (wm, first_processed), fetch=True)[0]
                advance = not int(gap["c"] or 0)
            if advance:
                self._meta_set("distill_watermark", str(batch_max))
            else:
                logger.info("brain: 手动蒸馏批次之前仍有积压轮次，水位保持 %s（不跳到 %s）",
                            wm, batch_max)
            self._meta_set("last_distill", now.strftime("%Y-%m-%d %H:%M:%S"))
            self._meta_incr("distill_runs", 1)
            if added_ids:
                self._meta_incr("distill_added", len(added_ids))
            if updated_ids:
                self._meta_incr("distill_updated", len(updated_ids))
            if result["skipped"]:
                self._meta_incr("distill_skipped", result["skipped"])
            result["watermark"] = batch_max if advance else wm
            result["added"] = added_ids
            result["updated"] = updated_ids
            logger.info("brain: 蒸馏完成 新增=%s 更新=%s 跳过=%s (批 %s 轮, id %s-%s)",
                        len(added_ids), len(updated_ids), result["skipped"],
                        len(rows), rows[0]["id"], rows[-1]["id"])
            return result
        finally:
            if lock_held:
                try:
                    self._exec("SELECT RELEASE_LOCK('brain_distill')")
                except Exception:  # noqa: BLE001
                    pass

    def _distill_extract(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """调用 LLM 从一批轮次中提取结论；任何解析失败都抛 BrainError（调用方只计数不推水位）。"""
        max_items = max(1, int(self.cfg.get("distill_max_items") or 6))
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        lines = [f"当前 UTC 时间: {now}", ""]
        for r in rows:
            ts = r.get("ts")
            ts_txt = ts.strftime("%Y-%m-%d %H:%M:%S") if hasattr(ts, "strftime") else str(ts or "?")
            src = "/".join(x for x in (r.get("machine"), r.get("platform")) if x) or "?"
            lines.append(f"--- 轮次 id={r['id']} 时间={ts_txt} 来源={src}")
            lines.append(f"用户: {r.get('user_content') or ''}")
            lines.append(f"助手: {r.get('assistant_content') or ''}")
        text = self._distill_llm_chat([
            {"role": "system",
             "content": self._DISTILL_SYSTEM_PROMPT.replace("{max_items}", str(max_items))},
            {"role": "user", "content": "\n".join(lines)},
        ])
        raw = text.strip()
        if raw.startswith("```"):                     # 剥 ```json 围栏
            first_nl = raw.find("\n")
            raw = raw[first_nl + 1:] if first_nl >= 0 else ""
            if raw.rstrip().endswith("```"):
                raw = raw.rstrip()[:-3]
        start, end = raw.find("["), raw.rfind("]")
        if start < 0 or end <= start:
            raise BrainError("蒸馏输出未找到 JSON 数组")
        try:
            data = json.loads(raw[start:end + 1])
        except Exception as exc:  # noqa: BLE001
            raise BrainError(f"蒸馏输出 JSON 解析失败: {str(exc)[:160]}") from exc
        if not isinstance(data, list):
            raise BrainError("蒸馏输出不是 JSON 数组")
        out: list[dict[str, Any]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            kind = str(item.get("kind") or "fact").strip()
            if kind not in ("fact", "preference", "decision", "insight"):
                kind = "fact"
            try:
                imp = int(item.get("importance") or 5)
            except (TypeError, ValueError):
                imp = 5
            tags_raw = item.get("tags")
            tags: list[str] = []
            if isinstance(tags_raw, list):
                tags = [str(t).strip() for t in tags_raw if str(t).strip()]
            elif isinstance(tags_raw, str):
                tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
            out.append({
                "content": content, "kind": kind, "importance": imp,
                "tags": ",".join(tags[:3])[:255],
                "evidence_turn_ids": item.get("evidence_turn_ids") or [],
            })
            if len(out) >= max_items:
                break
        return out

    def _distill_llm_chat(self, messages: list[dict[str, str]]) -> str:
        """POST {base}/chat/completions；必须复用 _http_json（默认 UA 是 CommandCode 前置的刚需）。"""
        cfg = self.cfg
        key = str(cfg.get("distill_api_key") or "").strip()
        if not key:
            raise BrainError("蒸馏 API key 缺失（HERMES_CUSTOM_API_COMMANDCODE_AI_API_KEY）")
        url = str(cfg.get("distill_api_base_url") or "").rstrip("/") + "/chat/completions"
        payload = {
            "model": str(cfg.get("distill_api_model") or ""),
            "messages": messages,
            "temperature": 0.2,
            # 该模型是推理模型：reasoning_tokens 也计入 max_tokens，1500 会把 JSON 截断，
            # 因此留足预算（输出仍受 distill_max_items 约束，成本可控）。
            "max_tokens": 4000,
        }
        try:
            status, data = self._http_json(
                "POST", url, headers={"Authorization": f"Bearer {key}"}, payload=payload,
                timeout=float(cfg.get("distill_api_timeout") or 120))
        except Exception as exc:  # noqa: BLE001
            raise BrainError(f"蒸馏 API 请求失败: {str(exc)[:200]}") from exc
        if int(status) != 200:
            raise BrainError(f"蒸馏 API 返回 HTTP {status}")
        choice = (data.get("choices") or [{}])[0] if isinstance(data, dict) else {}
        if str(choice.get("finish_reason") or "") == "length":
            raise BrainError("蒸馏 API 输出被 max_tokens 截断")
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise BrainError("蒸馏 API 响应缺少 choices[0].message.content") from exc
        if not isinstance(content, str) or not content.strip():
            raise BrainError("蒸馏 API 返回空内容")
        return content

    def _distill_reconcile(self, candidates: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
        """灰区统一裁决：一次 LLM 调用返回 index → {decision,target_id}。"""
        text = self._distill_llm_chat([
            {"role": "system", "content": self._DISTILL_RECONCILE_PROMPT},
            {"role": "user", "content": json.dumps(candidates, ensure_ascii=False)},
        ])
        raw = text.strip()
        if raw.startswith("```"):
            first_nl = raw.find("\n")
            raw = raw[first_nl + 1:] if first_nl >= 0 else ""
            if raw.rstrip().endswith("```"):
                raw = raw.rstrip()[:-3]
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            raise BrainError("对账输出未找到 JSON 对象")
        try:
            data = json.loads(raw[start:end + 1])
        except Exception as exc:  # noqa: BLE001
            raise BrainError(f"对账输出 JSON 解析失败: {str(exc)[:160]}") from exc
        decisions = data.get("decisions") if isinstance(data, dict) else None
        if not isinstance(decisions, list):
            raise BrainError("对账输出缺少 decisions 数组")
        out: dict[int, dict[str, Any]] = {}
        for d in decisions:
            if not isinstance(d, dict):
                continue
            try:
                idx = int(d.get("index"))
            except (TypeError, ValueError):
                continue
            out[idx] = d
        return out

    def _distill_candidates(self, candidates: list[dict[str, Any]], *,
                            dry_run: bool) -> list[dict[str, Any]]:
        """候选逐条裁剪 → 向量对账 → （真实模式下）写入；返回带决策的明细。"""
        cfg = self.cfg
        min_imp = int(cfg.get("distill_min_importance") or 4)
        max_imp = int(cfg.get("distill_max_importance") or 7)
        if min_imp > max_imp:
            min_imp, max_imp = max_imp, min_imp
        low = float(cfg.get("distill_low_sim") or 0.85)
        high = float(cfg.get("distill_high_sim") or 0.94)
        now_txt = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        items: list[dict[str, Any]] = []
        gray: list[dict[str, Any]] = []

        for cand in candidates:
            content = str(cand.get("content") or "").strip()
            raw_tags = [t.strip() for t in str(cand.get("tags") or "").split(",") if t.strip()]
            tags = ",".join(["distill"] + [t for t in raw_tags if t != "distill"][:3])[:255]
            try:
                imp = int(cand.get("importance") or 5)
            except (TypeError, ValueError):
                imp = 5
            imp = max(min_imp, min(max_imp, imp))
            item: dict[str, Any] = {
                "content": content,
                "kind": str(cand.get("kind") or "fact"),
                "importance": imp,
                "tags": tags,
                "sim": None, "decision": "", "target_id": None,
                "id": None, "deduped": False, "reason": "",
                "evidence_turn_ids": cand.get("evidence_turn_ids") or [],
            }
            items.append(item)
            if not content:
                item["decision"], item["reason"] = "skip", "empty"
                continue
            if self._DISTILL_SENSITIVE_RE.search(content):
                item["decision"], item["reason"] = "skip", "sensitive"
                logger.warning("brain: 蒸馏候选命中敏感信息，已丢弃")
                continue
            hits: list[dict[str, Any]] = []
            try:
                hits = self.vector_search_memories(content, limit=3)
            except Exception as exc:  # noqa: BLE001
                logger.debug("brain: 蒸馏对账向量检索失败（按新增）: %s", str(exc)[:160])
            if hits:
                item["sim"] = float(hits[0].get("vscore") or 0.0)
            if item["sim"] is not None and item["sim"] >= high:
                item["decision"], item["reason"] = "skip", "duplicate"
                continue
            if item["sim"] is None or item["sim"] < low:
                item["decision"] = "new"
                continue
            item["_hits"] = hits                 # 灰区：留到统一裁决
            gray.append(item)

        if gray:
            decisions: dict[int, dict[str, Any]] = {}
            try:
                decisions = self._distill_reconcile([
                    {"index": i, "content": it["content"],
                     "neighbors": [{"id": int(h["id"]), "sim": h.get("vscore"),
                                    "content": str(h.get("content") or "")[:400]}
                                   for h in it.get("_hits") or []]}
                    for i, it in enumerate(gray)
                ])
            except Exception as exc:  # noqa: BLE001
                logger.warning("brain: 蒸馏对账失败，灰区全部按新增: %s", str(exc)[:200])
                decisions = {}
            for i, it in enumerate(gray):
                dec = decisions.get(i) or {}
                choice = str(dec.get("decision") or "new").strip().lower()
                valid = {int(h["id"]) for h in it.get("_hits") or []}
                target: Optional[int] = None
                try:
                    target = int(dec.get("target_id")) if dec.get("target_id") is not None else None
                except (TypeError, ValueError):
                    target = None
                if choice not in ("skip", "new", "update"):
                    choice = "new"
                if choice == "update" and target not in valid:
                    choice = "new"
                it["decision"] = choice
                it["target_id"] = target if choice == "update" else None
                if choice == "skip":
                    it["reason"] = "reconciled"

        for it in items:
            it.pop("_hits", None)
            if dry_run or it["decision"] not in ("new", "update"):
                continue
            try:
                saved = self.remember(it["content"], kind=it["kind"], tags=it["tags"],
                                      importance=it["importance"], source="distill")
            except Exception as exc:  # noqa: BLE001
                it["decision"], it["reason"] = "skip", "write_failed"
                logger.warning("brain: 蒸馏写入失败: %s", str(exc)[:200])
                continue
            new_id = saved.get("id")
            it["id"] = new_id
            it["deduped"] = bool(saved.get("deduped"))
            if it["deduped"]:
                it["decision"], it["reason"] = "skip", "deduped"
                it["target_id"] = None
                continue
            if it["decision"] == "update" and it["target_id"] and new_id:
                try:
                    # updated_at = updated_at 必须显式保留：否则 ON UPDATE 会把旧记忆的
                    # “最后更新/衰减年龄”冲成当下。
                    self._exec(
                        "UPDATE brain_memories SET superseded_by = %s, superseded_at = %s, "
                        "updated_at = updated_at WHERE id = %s AND active = 1",
                        (int(new_id), now_txt, int(it["target_id"])))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("brain: 标记旧记忆取代失败（新记忆已写入）: %s", str(exc)[:200])
        return items

    def ping(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        try:
            out["redis"] = bool(self.redis().ping())
        except Exception as exc:  # noqa: BLE001
            out["redis"] = False
            out["redis_error"] = str(exc)[:200]
        try:
            self._exec("SELECT 1", fetch=True)
            out["mariadb"] = True
        except Exception as exc:  # noqa: BLE001
            out["mariadb"] = False
            out["mariadb_error"] = str(exc)[:200]
        return out

    def stats(self) -> dict[str, Any]:
        out: dict[str, Any] = {"mysql": {}, "redis": {}}
        self.ensure_schema()
        row = self._exec(
            "SELECT (SELECT COUNT(*) FROM brain_memories) AS memories_total, "
            "(SELECT COUNT(*) FROM brain_memories WHERE active = 1) AS memories_active, "
            "(SELECT COUNT(*) FROM brain_turns) AS turns, "
            "(SELECT COUNT(*) FROM brain_turn_messages) AS turn_messages, "
            "(SELECT COALESCE(SUM(raw_bytes), 0) FROM brain_turn_messages) AS turn_messages_raw_bytes, "
            "(SELECT COUNT(*) FROM brain_sessions) AS sessions, "
            "(SELECT COUNT(*) FROM brain_archive) AS archives",
            fetch=True)[0]
        size = self._exec(
            "SELECT COALESCE(SUM(data_length + index_length), 0) AS bytes "
            "FROM information_schema.TABLES WHERE table_schema = %s",
            (self.cfg["mariadb_db"],), fetch=True)[0]
        out["mysql"] = {k: int(v or 0) for k, v in row.items()}
        out["mysql"]["size_bytes"] = int(size["bytes"] or 0)
        try:
            r = self.redis()
            out["redis"] = {
                "keys": int(r.dbsize()),
                "recent_stream": int(r.xlen(self._rk("recent"))),
                "hot_index": int(r.zcard(self._rk("hot", "index"))),
                "used_memory": str(r.info("memory").get("used_memory_human", "")),
            }
        except Exception as exc:  # noqa: BLE001
            out["redis"] = {"error": str(exc)[:200]}
        return out

    def prune(self, *, turns_keep_days: Optional[int] = None,
              archive_keep_days: Optional[int] = None,
              inactive_keep_days: Optional[int] = None,
              sessions_keep_days: Optional[int] = None,
              dry_run: bool = False) -> dict[str, Any]:
        def _days(value: Optional[int], key: str) -> int:
            return int(value if value is not None else (self.cfg.get(key) or 0))

        t_days = _days(turns_keep_days, "turns_keep_days")
        a_days = _days(archive_keep_days, "archive_keep_days")
        i_days = _days(inactive_keep_days, "inactive_keep_days")
        s_days = _days(sessions_keep_days, "sessions_keep_days")
        self.ensure_schema()
        targets: list[tuple[str, str, str, tuple]] = []
        if t_days > 0:
            targets.append((
                "turns",
                "DELETE FROM brain_turns WHERE ts < (NOW() - INTERVAL %s DAY)",
                "SELECT COUNT(*) AS c FROM brain_turns WHERE ts < (NOW() - INTERVAL %s DAY)",
                (t_days,)))
        if a_days > 0:
            targets.append((
                "archives",
                "DELETE FROM brain_archive WHERE ts < (NOW() - INTERVAL %s DAY)",
                "SELECT COUNT(*) AS c FROM brain_archive WHERE ts < (NOW() - INTERVAL %s DAY)",
                (a_days,)))
        if i_days > 0:
            targets.append((
                "inactive_memories",
                "DELETE FROM brain_memories WHERE active = 0 "
                "AND COALESCE(deactivated_at, updated_at) < (NOW() - INTERVAL %s DAY)",
                "SELECT COUNT(*) AS c FROM brain_memories WHERE active = 0 "
                "AND COALESCE(deactivated_at, updated_at) < (NOW() - INTERVAL %s DAY)",
                (i_days,)))
        if s_days > 0:
            # 会话索引：只删“旧、且其轮次已不在库中（或同样过期）”的行
            cond = ("last_active < (NOW() - INTERVAL %s DAY) AND NOT EXISTS "
                    "(SELECT 1 FROM brain_turns t WHERE t.session_id = brain_sessions.session_id "
                    "AND t.ts >= (NOW() - INTERVAL %s DAY))")
            targets.append((
                "sessions",
                f"DELETE FROM brain_sessions WHERE {cond}",
                f"SELECT COUNT(*) AS c FROM brain_sessions WHERE {cond}",
                (s_days, s_days)))
        out: dict[str, Any] = {"dry_run": bool(dry_run)}
        if t_days > 0:
            # 先清转录层（按所属轮次的时间），再删轮次本体
            msg_cond = ("FROM brain_turn_messages m JOIN brain_turns t ON t.id = m.turn_id "
                        "WHERE t.ts < (NOW() - INTERVAL %s DAY)")
            if dry_run:
                out["turn_messages"] = int(self._exec(
                    "SELECT COUNT(*) AS c " + msg_cond, (t_days,), fetch=True)[0]["c"] or 0)
            else:
                rowcount, _ = self._exec("DELETE m " + msg_cond, (t_days,))
                out["turn_messages"] = int(rowcount)
        for name, delete_sql, count_sql, params in targets:
            if dry_run:
                out[name] = int(self._exec(count_sql, params, fetch=True)[0]["c"] or 0)
            else:
                rowcount, _ = self._exec(delete_sql, params)
                out[name] = int(rowcount)
        out["skipped"] = [name for name, days in (("turns", t_days), ("archives", a_days),
                                                  ("inactive_memories", i_days), ("sessions", s_days))
                          if days <= 0]
        return out
