"""atbrains 服务端配置：默认值 + 配置文件 + 环境变量（机密只走环境变量 / .env）。

配置发现顺序（通用服务端，不绑定任何单一 Agent 框架）：
  1. 显式传入的 config_home / config_path，或环境变量 ATBRAINS_CONFIG
  2. 环境变量 ATBRAINS_HOME
  3. ~/.config/atbrains（存在 server.json / atbrains.json / .env 时）
  4. /etc/atbrains（存在 server.json / atbrains.json / .env 时）
  5. 仅作兼容回退：HERMES_HOME 或 ~/.hermes（当上述通用目录均不存在且旧配置存在时）
  6. 最终环境变量覆盖（ATBRAINS_MARIADB_HOST / ATBRAINS_REDIS_HOST / ...）
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("atbrains")

DEFAULT_CONFIG: dict[str, Any] = {
    "host": "0.0.0.0",
    "port": 8787,
    "redis_host": "127.0.0.1",
    "redis_port": 9079,
    "redis_db": 0,
    # 多实例隔离：空串 = 保持 "brain:<后缀>"（兼容既有库）；非空 = "<prefix>:<后缀>"。
    "redis_key_prefix": "",
    "mariadb_host": "127.0.0.1",
    "mariadb_port": 57306,
    "mariadb_user": "agent",
    "mariadb_db": "agent_memory",
    "machine": "",
    # 写入方身份（brain_memories/brain_claims/brain_episodes.agent）：空由调用方/令牌决定。
    "agent": "",
    # 服务端行为控制：迁移与后台任务默认显式开启，避免短命工具/测试误起后台线程。
    "auto_migrate": 1,
    "background_workers": 0,
    "short_term_ttl_days": 14,
    "hot_ttl_days": 30,
    "stream_maxlen": 2000,
    "prefetch_max_chars": 1200,
    "prefetch_memories": 5,
    "prefetch_turns": 3,
    "search_cache_ttl": 600,
    "search_turns_days": 120,
    "search_regexp_enabled": 1,
    "turns_keep_days": 0,
    "turn_messages_enabled": 1,
    "turn_msg_max_bytes": 1048576,
    "turn_total_max_bytes": 16777216,
    "archive_keep_days": 365,
    "inactive_keep_days": 365,
    "sessions_keep_days": 365,
    "maintenance_enabled": 1,
    "maintenance_interval_min": 60,
    "prune_every_days": 7,
    # 向量层（语义召回）
    "vector_enabled": 1,
    "embed_base_url": "https://api.siliconflow.cn/v1",
    "embed_model": "Qwen/Qwen3-Embedding-4B",
    "embed_dim": 2560,
    "embed_timeout": 60,
    "vector_query_timeout": 20,
    "http_keepalive_enabled": 1,
    "http_keepalive_idle_s": 60,
    "http_user_agent": "atbrains-server/3.0",
    "qdrant_url": "http://127.0.0.1:6333",
    "qdrant_collection": "brain_memories",
    # 优先读取显式环境变量/配置；未设时才按候选路径探测 Qdrant yaml
    "qdrant_config_file": "",
    "vector_topk": 12,
    "vector_min_score": 0.25,
    "vector_rrf_k": 60,
    "vector_drain_batch": 8,
    "vector_drain_interval": 60,
    "vector_text_max": 12000,
    "vector_qemb_ttl": 3600,
    # 记忆分层与时间衰减
    "decay_enabled": 1,
    "decay_half_life_days": 45,
    "decay_types": "episodic",
    # 访问强化
    "access_boost_enabled": 1,
    "access_boost_factor": 0.35,
    # 按类型 TTL
    "ttl_enabled": 1,
    "ttl_days_episodic": 90,
    "ttl_days_todo": 30,
    # 图谱层（第三路召回）
    "graph_enabled": 1,
    "graph_route_weight": 0.35,
    "graph_auto_enabled": 1,
    "graph_auto_interval_min": 10,
    "graph_auto_limit": 12,
    # rerank（默认关闭）
    "rerank_enabled": 0,
    "rerank_url": "https://api.siliconflow.cn/v1/rerank",
    "rerank_model": "BAAI/bge-reranker-v2-m3",
    "rerank_topn": 20,
    "rerank_min_score": 0.0,
    "rerank_timeout": 20,
    # 结论蒸馏（默认关闭）
    "distill_enabled": 0,
    "distill_interval_min": 60,
    "distill_min_turns": 8,
    "distill_max_turns": 16,
    "distill_turn_chars": 1000,
    "distill_stagnation_hours": 24,
    "distill_max_items": 6,
    "distill_min_importance": 4,
    "distill_max_importance": 10,
    "distill_low_sim": 0.85,
    "distill_high_sim": 0.94,
    "distill_api_base_url": "https://api.commandcode.ai/provider/v1",
    "distill_api_model": "deepseek/deepseek-v4.1-flash",
    "distill_api_timeout": 120,
    "distill_api_max_tokens": 8000,
    # 投影层瘦身
    "detail_args_max_chars": 512,
    "detail_result_max_chars": 1024,
    "detail_reasoning_max_chars": 2000,
    # 运维探测（可选，默认空表示自动探测）
    "access_log": "",
    "data_dir": "",
}

# 机密：每个配置键按顺序尝试多个环境变量（通用 ATBRAINS_* 优先，兼容历史变量名）
SECRET_ENV_CANDIDATES: dict[str, tuple[str, ...]] = {
    "redis_password": ("ATBRAINS_REDIS_PASSWORD", "BRAIN_REDIS_PASSWORD"),
    "mariadb_password": ("ATBRAINS_MARIADB_PASSWORD", "BRAIN_MARIADB_PASSWORD"),
    "qdrant_api_key": ("ATBRAINS_QDRANT_API_KEY",),
    "embed_api_key": ("ATBRAINS_EMBED_API_KEY", "ATBRAINS_QDRANT_API_KEY"),
    "distill_api_key": (
        "ATBRAINS_DISTILL_API_KEY",
        "HERMES_CUSTOM_API_COMMANDCODE_AI_API_KEY",
    ),
    "distill_api_key_fallbacks": (
        "ATBRAINS_DISTILL_API_KEY_FALLBACKS",
        "COMMANDCODE_API_KEY",
    ),
}

# 向后兼容导出（单变量主名映射）
SECRET_ENV_VARS: dict[str, str] = {
    "redis_password": "ATBRAINS_REDIS_PASSWORD",
    "mariadb_password": "ATBRAINS_MARIADB_PASSWORD",
    "qdrant_api_key": "ATBRAINS_QDRANT_API_KEY",
    "embed_api_key": "ATBRAINS_EMBED_API_KEY",
    "distill_api_key": "HERMES_CUSTOM_API_COMMANDCODE_AI_API_KEY",
    "distill_api_key_fallbacks": "COMMANDCODE_API_KEY",
}

# 非密配置项的环境变量覆盖（12-factor / Docker / systemd 友好）
NON_SECRET_ENV_VARS: dict[str, tuple[str, ...]] = {
    "host": ("ATBRAINS_HOST",),
    "port": ("ATBRAINS_PORT",),
    "redis_host": ("ATBRAINS_REDIS_HOST",),
    "redis_port": ("ATBRAINS_REDIS_PORT",),
    "redis_db": ("ATBRAINS_REDIS_DB",),
    "redis_key_prefix": ("ATBRAINS_REDIS_KEY_PREFIX",),
    "mariadb_host": ("ATBRAINS_MARIADB_HOST",),
    "mariadb_port": ("ATBRAINS_MARIADB_PORT",),
    "mariadb_user": ("ATBRAINS_MARIADB_USER",),
    "mariadb_db": ("ATBRAINS_MARIADB_DB",),
    "machine": ("ATBRAINS_MACHINE",),
    "agent": ("ATBRAINS_AGENT",),
    "auto_migrate": ("ATBRAINS_AUTO_MIGRATE",),
    "background_workers": ("ATBRAINS_BACKGROUND_WORKERS",),
    "vector_enabled": ("ATBRAINS_VECTOR_ENABLED",),
    "embed_base_url": ("ATBRAINS_EMBED_BASE_URL",),
    "embed_model": ("ATBRAINS_EMBED_MODEL",),
    "embed_dim": ("ATBRAINS_EMBED_DIM",),
    "qdrant_url": ("ATBRAINS_QDRANT_URL",),
    "qdrant_collection": ("ATBRAINS_QDRANT_COLLECTION",),
    "qdrant_config_file": ("ATBRAINS_QDRANT_CONFIG_FILE",),
    "graph_enabled": ("ATBRAINS_GRAPH_ENABLED",),
    "maintenance_enabled": ("ATBRAINS_MAINTENANCE_ENABLED",),
    "rerank_enabled": ("ATBRAINS_RERANK_ENABLED",),
    "rerank_url": ("ATBRAINS_RERANK_URL",),
    "rerank_model": ("ATBRAINS_RERANK_MODEL",),
    "distill_enabled": ("ATBRAINS_DISTILL_ENABLED",),
    "distill_api_base_url": ("ATBRAINS_DISTILL_API_BASE_URL",),
    "distill_api_model": ("ATBRAINS_DISTILL_API_MODEL",),
    "access_log": ("ATBRAINS_ACCESS_LOG",),
    "data_dir": ("ATBRAINS_DATA_DIR",),
}

PROVIDER_NAME = "atbrains-memory"
KEY_PREFIX = "brain:"

_CONFIG_FILENAMES = ("server.json", "atbrains.json", "brain.json")


def _has_config_files(directory: Path) -> bool:
    return any((directory / name).exists() for name in (*_CONFIG_FILENAMES, ".env"))


def config_home_path(config_home: Optional[str] = None) -> Path:
    """解析配置目录：显式参数 > ATBRAINS_HOME > ~/.config/atbrains > /etc/atbrains > 兼容 ~/.hermes。"""
    if config_home:
        return Path(config_home).expanduser()
    env_cfg = os.environ.get("ATBRAINS_CONFIG")
    if env_cfg:
        p = Path(env_cfg).expanduser()
        return p.parent if p.is_file() or p.suffix == ".json" else p
    env_home = os.environ.get("ATBRAINS_HOME")
    if env_home:
        return Path(env_home).expanduser()
    xdg = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")).expanduser() / "atbrains"
    if _has_config_files(xdg):
        return xdg
    etc = Path("/etc/atbrains")
    if _has_config_files(etc):
        return etc
    hermes_env = os.environ.get("HERMES_HOME")
    if hermes_env:
        return Path(hermes_env).expanduser()
    legacy = Path.home() / ".hermes"
    if _has_config_files(legacy):
        return legacy
    return xdg


def hermes_home_path(hermes_home: Optional[str] = None) -> Path:
    """向后兼容别名；新代码请使用 `config_home_path()`。"""
    return config_home_path(hermes_home)


def resolve_data_dir(config_home: Optional[str] = None) -> Path:
    """服务端运行时数据目录（服务令牌、控制台口令、pid、访问日志）。"""
    env = os.environ.get("ATBRAINS_DATA_DIR")
    if env:
        return Path(env).expanduser()
    env_home = os.environ.get("ATBRAINS_HOME")
    if env_home:
        return Path(env_home).expanduser()
    if config_home:
        return Path(config_home).expanduser()
    xdg_data = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")).expanduser() / "atbrains"
    if (xdg_data / "service-tokens.json").exists():
        return xdg_data
    etc = Path("/etc/atbrains")
    if (etc / "service-tokens.json").exists():
        return etc
    var_lib = Path("/var/lib/atbrains")
    if (var_lib / "service-tokens.json").exists():
        return var_lib
    return xdg_data


def default_qdrant_config_candidates() -> tuple[Path, ...]:
    return (
        Path("/etc/qdrant/production.yaml"),
        Path("/etc/qdrant/config.yaml"),
        Path("/root/qdrant/qdrant-config/production.yaml"),
    )


def _dotenv_value(raw: str) -> str:
    """解析 .env 单值：引号内原样保留；未加引号时只有空白后的 `#` 才开始行内注释。"""
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
        return value
    if value.startswith("#"):
        return ""
    m = re.search(r"[ \t]#", value)
    if m:
        value = value[:m.start()].rstrip()
    return value


_DOTENV_CACHE: dict[tuple[str, int], dict[str, str]] = {}


def _dotenv_all(env_path: Path) -> dict[str, str]:
    """解析 .env 子集（bare / 'single' / "double"），按 mtime 缓存。"""
    try:
        st = env_path.stat()
    except OSError:
        return {}
    cache_key = (str(env_path), st.st_mtime_ns)
    cached = _DOTENV_CACHE.get(cache_key)
    if cached is not None:
        return cached
    try:
        text = env_path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return {}
    parsed: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, sep, value = line.partition("=")
        if not sep:
            continue
        parsed.setdefault(name.strip(), _dotenv_value(value))
    _DOTENV_CACHE.clear()
    _DOTENV_CACHE[cache_key] = parsed
    return parsed


def _dotenv_get(env_path: Path, key: str) -> Optional[str]:
    return _dotenv_all(env_path).get(key)


def _first_env_or_dotenv(env_file: Path, names: tuple[str, ...]) -> Optional[str]:
    for var in names:
        val = os.environ.get(var)
        if val is not None and val != "":
            return val
        val = _dotenv_get(env_file, var)
        if val is not None and val != "":
            return val
    return None


def resolve_config(config_home: Optional[str] = None) -> dict[str, Any]:
    """合并配置：默认值 < JSON 配置文件 < .env / 环境变量。"""
    explicit_file: Optional[Path] = None
    if config_home and Path(config_home).expanduser().suffix == ".json":
        explicit_file = Path(config_home).expanduser()
        home = explicit_file.parent
    elif not config_home and os.environ.get("ATBRAINS_CONFIG"):
        p = Path(os.environ["ATBRAINS_CONFIG"]).expanduser()
        if p.is_file() or p.suffix == ".json":
            explicit_file = p
            home = p.parent
        else:
            home = p
    else:
        home = config_home_path(config_home)

    cfg: dict[str, Any] = dict(DEFAULT_CONFIG)
    candidates = [explicit_file] if explicit_file else [home / name for name in _CONFIG_FILENAMES]
    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict):
                for k, v in data.items():
                    if k in DEFAULT_CONFIG and v is not None:
                        cfg[k] = v
            break
        except Exception as exc:  # noqa: BLE001
            logger.warning("atbrains: 忽略无法解析的 %s: %s", candidate, exc)

    env_file = home / ".env"

    # 非密环境变量覆盖（优先级高于 JSON）
    for key, vars_list in NON_SECRET_ENV_VARS.items():
        val = _first_env_or_dotenv(env_file, vars_list)
        if val is not None:
            cfg[key] = val

    # 机密环境变量 / .env
    for key, vars_list in SECRET_ENV_CANDIDATES.items():
        val = _first_env_or_dotenv(env_file, vars_list)
        if val is not None:
            cfg[key] = val

    # 整数配置归一化
    for k, default in DEFAULT_CONFIG.items():
        if isinstance(default, int):
            try:
                cfg[k] = int(cfg[k])
            except (TypeError, ValueError):
                cfg[k] = default

    cfg["config_home"] = str(home)
    cfg["hermes_home"] = str(home)  # 兼容旧测试与调用方
    if not cfg.get("data_dir"):
        cfg["data_dir"] = str(resolve_data_dir(config_home if config_home and not explicit_file else None))
    return cfg


def resolve_server_http_config(config_home: Optional[str] = None) -> dict[str, Any]:
    """解析 HTTP 服务端监听配置（host / port / data_dir / access_log）。"""
    cfg = resolve_config(config_home)
    data_dir = Path(str(cfg.get("data_dir") or resolve_data_dir()))
    access_log = str(cfg.get("access_log") or "").strip()
    if not access_log:
        access_log = str(data_dir / "access.log")
    return {
        "host": str(cfg.get("host") or "0.0.0.0"),
        "port": int(cfg.get("port") or 8787),
        "data_dir": data_dir,
        "access_log": access_log,
        "config_home": str(cfg.get("config_home") or ""),
    }
