"""atbrains 令牌管理：Bearer 令牌与多 Agent 身份解析（machine | agent + role）。"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .config import resolve_data_dir


@dataclass(frozen=True)
class TokenIdentity:
    name: str
    machine: str
    agent: str
    role: str = "agent"   # "agent" | "admin"


def split_identity(name: str) -> tuple[str, str]:
    """令牌名 → (主机, agent)。规范写法 `<主机>|<agent>`。"""
    n = str(name or "")
    if "|" in n:
        left, _, right = n.partition("|")
        return left.strip(), right.strip()
    return n, n


def mask(token: str) -> str:
    """令牌掩码(abcd…wxyz)—— 只给管理端展示，响应体/日志从不出现明文。"""
    t = str(token or "")
    return f"{t[:4]}…{t[-4:]}" if len(t) > 12 else "…"


def header_value(headers: Any, name: str) -> str:
    want = name.lower()
    for k, v in (headers or {}).items():
        if str(k).lower() == want:
            return str(v or "")
    return ""


def extract_bearer_token(headers: Any) -> str:
    auth = header_value(headers, "Authorization")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    return header_value(headers, "X-Memory-Token").strip()


def default_tokens_path(data_dir: Optional[Path] = None) -> Path:
    env = os.environ.get("ATBRAINS_TOKENS_FILE")
    if env:
        return Path(env).expanduser()
    base = Path(data_dir) if data_dir is not None else resolve_data_dir()
    return base / "service-tokens.json"


class TokenStore:
    """线程安全的令牌表存储（按 mtime 缓存，支持多进程 CLI 更新）。"""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._explicit_path = path
        self._lock = threading.Lock()
        self._cache: Optional[tuple[Path, Optional[int], dict[str, dict[str, str]]]] = None

    @property
    def path(self) -> Path:
        return self._explicit_path or default_tokens_path()

    @staticmethod
    def _normalize_entry(name: str, raw: Any) -> Optional[dict[str, str]]:
        if isinstance(raw, str):
            tok = raw.strip()
            if not tok:
                return None
            default_role = "admin" if name in ("server", "admin") else "agent"
            return {"token": tok, "role": default_role}
        if isinstance(raw, dict):
            tok = str(raw.get("token") or "").strip()
            if not tok:
                return None
            out: dict[str, str] = {"token": tok}
            if raw.get("machine"):
                out["machine"] = str(raw["machine"]).strip()
            if raw.get("agent"):
                out["agent"] = str(raw["agent"]).strip()
            role = str(raw.get("role") or ("admin" if name in ("server", "admin") else "agent")).strip().lower()
            out["role"] = "admin" if role == "admin" else "agent"
            return out
        return None

    def _read_raw(self) -> dict[str, Any]:
        p = self.path
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data: dict[str, Any] = {"server": secrets.token_urlsafe(32)}
            self._write_raw(data)
            return data
        except Exception:  # noqa: BLE001
            return {}
        return raw if isinstance(raw, dict) else {}

    def _write_raw(self, data: dict[str, Any]) -> None:
        p = self.path
        p.parent.mkdir(parents=True, exist_ok=True)
        blob = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        tmp = p.with_name(p.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(blob)
        os.replace(tmp, p)
        os.chmod(p, 0o600)

    def entries(self) -> dict[str, dict[str, str]]:
        p = self.path
        with self._lock:
            try:
                mtime: Optional[int] = p.stat().st_mtime_ns
            except OSError:
                mtime = None
            if self._cache is not None and self._cache[0] == p and self._cache[1] == mtime:
                return {k: dict(v) for k, v in self._cache[2].items()}
            raw = self._read_raw()
            out: dict[str, dict[str, str]] = {}
            for k, v in raw.items():
                entry = self._normalize_entry(str(k), v)
                if entry is not None:
                    out[str(k)] = entry
            try:
                mtime = p.stat().st_mtime_ns
            except OSError:
                pass
            self._cache = (p, mtime, out)
            return {k: dict(v) for k, v in out.items()}

    def load(self) -> dict[str, str]:
        """兼容视图：{name: token_str}。"""
        return {k: v["token"] for k, v in self.entries().items()}

    def add(self, name: str, *, role: str = "", machine: str = "", agent: str = "") -> str:
        key = str(name or "").strip()
        if not key:
            raise ValueError("令牌名称不能为空")
        tok = secrets.token_urlsafe(32)
        with self._lock:
            raw = self._read_raw()
            if role or machine or agent:
                entry: dict[str, Any] = {"token": tok}
                if machine:
                    entry["machine"] = str(machine).strip()
                if agent:
                    entry["agent"] = str(agent).strip()
                entry["role"] = "admin" if role == "admin" else ("admin" if key in ("server", "admin") else "agent")
                raw[key] = entry
            else:
                raw[key] = tok
            self._write_raw(raw)
            self._cache = None
        return tok

    def remove(self, name: str) -> bool:
        key = str(name or "").strip()
        with self._lock:
            raw = self._read_raw()
            if key not in raw:
                return False
            del raw[key]
            self._write_raw(raw)
            self._cache = None
            return True

    def authorize(self, headers: Any, *, channel: str = "v1") -> Optional[TokenIdentity]:
        """验证请求头并解析身份；失败返回 None。"""
        got = extract_bearer_token(headers)
        if not got:
            return None
        enc = got.encode("utf-8", "replace")
        for name, entry in self.entries().items():
            known = entry.get("token") or ""
            if known and hmac.compare_digest(enc, known.encode("utf-8", "replace")):
                if entry.get("machine") or entry.get("agent"):
                    mach = entry.get("machine") or ("" if channel == "mcp" else split_identity(name)[0])
                    ag = entry.get("agent") or split_identity(name)[1]
                elif "|" in name:
                    mach, ag = split_identity(name)
                    if channel == "mcp":
                        mach = ""
                else:
                    if channel == "mcp":
                        mach, ag = "", name
                    else:
                        mach, ag = name, "hermes"
                return TokenIdentity(
                    name=name,
                    machine=mach,
                    agent=ag,
                    role=entry.get("role") or "agent",
                )
        return None
