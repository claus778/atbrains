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
    client_name: str = ""
    timeout_ms: int = 1200
    write_timeout_ms: int = 4000
    spool_dir: str = ""
    spool_max_bytes: int = 10 * 1024 * 1024
    recall_cache_ttl: int = 60
    recall_cache_max: int = 128
    expected_protocol_major: int = 2
    config_home: str = ""


def mask_token(token: str) -> str:
    t = str(token or "").strip()
    if not t:
        return "(未设置)"
    return f"{t[:4]}…{t[-4:]}" if len(t) > 12 else "…"


def _upsert_dotenv(env_path: Path, key: str, value: str) -> None:
    env_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if env_path.exists():
        try:
            lines = env_path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        except OSError:
            lines = []
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    needs_quote = any(c in value for c in (' ', '\t', '#', '"', "'"))
    new_line = f'{key}="{escaped}"' if needs_quote else f"{key}={value}"
    replaced = False
    out_lines: list[str] = []
    for line in lines:
        s = line.strip()
        check = s[7:].lstrip() if s.startswith("export ") else s
        if not s.startswith("#") and "=" in check:
            k, _, _ = check.partition("=")
            if k.strip() == key:
                if not replaced:
                    out_lines.append(new_line)
                    replaced = True
                continue
        out_lines.append(line)
    if not replaced:
        out_lines.append(new_line)
    content = "\n".join(out_lines).rstrip("\n") + "\n"
    tmp = env_path.with_name(env_path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, env_path)
    os.chmod(env_path, 0o600)


def save_client_token(
    token: str,
    *,
    config_home: Optional[str] = None,
    base_url: Optional[str] = None,
    json_filename: str = "client.json",
    env_key: str = "ATBRAINS_TOKEN",
) -> dict[str, str]:
    """安全持久化客户端 Bearer Token 到 `.env`（权限 600），可选同步写入 `service_url`。"""
    tok = str(token or "").strip()
    if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in ('"', "'"):
        tok = tok[1:-1].strip()
    if not tok:
        raise ValueError("token 不能为空")
    if "\n" in tok or "\r" in tok:
        raise ValueError("token 不能包含换行符")
    home = Path(config_home).expanduser() if config_home else (
        Path(os.environ.get("ATBRAINS_HOME")).expanduser()
        if os.environ.get("ATBRAINS_HOME")
        else Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")).expanduser() / "atbrains"
    )
    home.mkdir(parents=True, exist_ok=True)
    env_path = home / ".env"
    _upsert_dotenv(env_path, env_key, tok)

    json_path = home / json_filename
    eff_url = normalize_base_url(base_url or "")
    if eff_url:
        existing: dict[str, Any] = {}
        if json_path.exists():
            try:
                parsed = json.loads(json_path.read_text(encoding="utf-8-sig"))
                if isinstance(parsed, dict):
                    existing = parsed
            except Exception:  # noqa: BLE001
                existing = {}
        existing["service_url"] = eff_url
        tmp = json_path.with_name(json_path.name + ".tmp")
        tmp.write_text(json.dumps(existing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, json_path)
    return {
        "config_home": str(home),
        "env_path": str(env_path),
        "json_path": str(json_path),
        "base_url": eff_url,
        "masked": mask_token(tok),
    }


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
    client_name: Optional[str] = None,
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

    cname = (
        str(client_name or "").strip().lower()
        or _first_env_or_dotenv(env_paths, ("ATBRAINS_CLIENT",)).lower()
        or str(raw.get("client") or raw.get("client_name") or "").strip().lower()
    )[:32]

    return ClientConfig(
        base_url=url,
        token=tok,
        client_name=cname,
        timeout_ms=max(200, t_ms),
        write_timeout_ms=max(500, wt_ms),
        spool_dir=sp_dir,
        config_home=str(chosen_home),
    )
