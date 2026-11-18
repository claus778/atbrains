"""atbrains 客户端配置解析（纯标准库，支持通用配置与旧版兼容）。"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


def normalize_base_url(raw: str) -> str:
    s = str(raw or "").strip()
    if not s:
        return ""
    if not re.match(r"^https?://", s, re.I):
        s = f"http://{s}"
    s = s.rstrip("/")
    s = re.sub(r"/(mcp|v1)$", "", s, flags=re.I)
    return s


def _dotenv_value(raw: str) -> str:
    value = raw.strip()
    if value[:1] in ('"', "'"):
        q = value[0]
        out: list[str] = []
        i = 1
        while i < len(value):
            ch = value[i]
            if q == '"' and ch == "\\" and value[i + 1:i + 2] in ('"', "\\"):
                out.append(value[i + 1])
                i += 2
                continue
            if ch == q:
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


def _dotenv_get(env_path: Path, key: str) -> Optional[str]:
    try:
        text = env_path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("export "):
            s = s[7:].lstrip()
        name, sep, val = s.partition("=")
        if sep and name.strip() == key:
            return _dotenv_value(val)
    return None


def _first_env_or_dotenv(env_paths: list[Path], keys: tuple[str, ...]) -> str:
    for k in keys:
        val = os.environ.get(k)
        if val is not None and val.strip():
            return val.strip()
    for p in env_paths:
        for k in keys:
            val = _dotenv_get(p, k)
            if val is not None and val.strip():
                return val.strip()
    return ""


@dataclass
class ClientConfig:
    base_url: str = ""
    token: str = ""
    timeout_ms: int = 1200
    write_timeout_ms: int = 4000
    spool_dir: str = ""
    spool_max_bytes: int = 10 * 1024 * 1024
    recall_cache_ttl: int = 60
    recall_cache_max: int = 128
    expected_protocol_major: int = 2
    config_home: str = ""


def _candidate_homes(config_home: Optional[str] = None) -> list[Path]:
    homes: list[Path] = []
    if config_home:
        homes.append(Path(config_home).expanduser())
    if os.environ.get("ATBRAINS_HOME"):
        homes.append(Path(os.environ["ATBRAINS_HOME"]).expanduser())
    xdg = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")).expanduser() / "atbrains"
    homes.append(xdg)
    if os.environ.get("HERMES_HOME"):
        homes.append(Path(os.environ["HERMES_HOME"]).expanduser())
    homes.append(Path.home() / ".hermes")
    # 去重保序
    seen: set[str] = set()
    uniq: list[Path] = []
    for h in homes:
        key = str(h)
        if key not in seen:
            seen.add(key)
            uniq.append(h)
    return uniq


def resolve_client_config(
    config_home: Optional[str] = None,
    *,
    base_url: Optional[str] = None,
    token: Optional[str] = None,
    timeout_ms: Optional[int] = None,
    write_timeout_ms: Optional[int] = None,
    spool_dir: Optional[str] = None,
) -> ClientConfig:
    """按优先级解析客户端连接配置：显式参数 > 环境变量 > client.json / atbrains.json + .env。"""
    homes = _candidate_homes(config_home)
    raw: dict[str, Any] = {}
    chosen_home = homes[0] if homes else (Path.home() / ".config" / "atbrains")
    env_paths: list[Path] = []

    for h in homes:
        if (h / ".env").exists():
            env_paths.append(h / ".env")
        if raw:
            continue
        for fname in ("client.json", "atbrains.json", "brain.json"):
            p = h / fname
            if not p.exists():
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8-sig"))
                if isinstance(data, dict):
                    raw = data
                    chosen_home = h
                    break
            except Exception:  # noqa: BLE001
                continue

    url = base_url or _first_env_or_dotenv(
        env_paths, ("ATBRAINS_URL", "ATBRAINS_REMOTE_URL", "ATBRAINS_SERVICE_URL"))
    if not url:
        for k in ("url", "base_url", "service_url", "remote_url"):
            if str(raw.get(k) or "").strip():
                url = str(raw[k]).strip()
                break
    if not url:
        # 兼容从旧配置 mariadb_host 推断（仅非回环地址）
        host = str(raw.get("mariadb_host") or "").strip()
        if host and host not in ("127.0.0.1", "localhost", "::1"):
            url = f"http://{host}:8787"
    url = normalize_base_url(url)

    tok = token or _first_env_or_dotenv(
        env_paths,
        ("ATBRAINS_TOKEN", "ATBRAINS_SERVICE_TOKEN", "ATBRAINS_REMOTE_TOKEN",
         "ATBRAINS_MCP_TOKEN", "BRAIN_REMOTE_TOKEN"),
    )
    if not tok:
        for k in ("token", "service_token", "remote_token"):
            if str(raw.get(k) or "").strip():
                tok = str(raw[k]).strip()
                break

    def _int_val(explicit: Optional[int], env_keys: tuple[str, ...], raw_keys: tuple[str, ...], default: int) -> int:
        if explicit is not None:
            return int(explicit)
        v = _first_env_or_dotenv(env_paths, env_keys)
        if v:
            try:
                return int(v)
            except ValueError:
                pass
        for rk in raw_keys:
            if raw.get(rk) is not None:
                try:
                    val = int(raw[rk])
                    # remote_recall_timeout 旧单位是秒
                    if rk == "remote_recall_timeout" and val <= 60:
                        return val * 1000
                    return val
                except (TypeError, ValueError):
                    pass
        return default

    t_ms = _int_val(timeout_ms, ("ATBRAINS_TIMEOUT_MS",),
                    ("timeout_ms", "recall_timeout_ms", "remote_timeout_ms", "remote_recall_timeout"), 1200)
    wt_ms = _int_val(write_timeout_ms, ("ATBRAINS_WRITE_TIMEOUT_MS",),
                     ("write_timeout_ms",), max(t_ms, 4000))

    sp_dir = spool_dir if spool_dir is not None else _first_env_or_dotenv(env_paths, ("ATBRAINS_SPOOL_DIR",))
    if not sp_dir and raw.get("spool_dir"):
        sp_dir = str(raw["spool_dir"])

    return ClientConfig(
        base_url=url,
        token=tok,
        timeout_ms=max(200, t_ms),
        write_timeout_ms=max(500, wt_ms),
        spool_dir=sp_dir,
        config_home=str(chosen_home),
    )
