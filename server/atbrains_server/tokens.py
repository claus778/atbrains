"""atbrains 令牌管理：Bearer 令牌与多 Agent 身份解析（machine | agent + role）。"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hmac
import json
import os
import secrets
import threading
from pathlib import Path
from typing import Any, Iterator, Optional

from .config import resolve_data_dir


@dataclass(frozen=True)
class TokenIdentity:
    name: str
    machine: str
    agent: str
    role: str = "agent"
    client: str = ""


BUILTIN_ADMIN_NAMES = frozenset({"server"})

_CLIENT_ALIASES = {
    "admin": "admin",
    "hermes": "hermes",
    "astrbot": "astrbot",
    "dsh": "dsh",
    "claude": "claude-code",
    "claude-code": "claude-code",
    "codex": "codex",
    "cursor": "cursor",
    "python": "python-sdk",
    "python-sdk": "python-sdk",
    "mcp": "mcp",
    "custom": "custom",
}


def infer_client(agent: str, raw_client: str = "", *, role: str = "") -> str:
    c = str(raw_client or "").strip().lower()[:32]
    if c:
        return _CLIENT_ALIASES.get(c, c)
    a = str(agent or "").strip().lower()
    if a in _CLIENT_ALIASES:
        return _CLIENT_ALIASES[a]
    for k, v in _CLIENT_ALIASES.items():
        if k not in ("custom", "admin") and (a.startswith(k + "-") or a.startswith(k + "_")):
            return v
    if str(role or "").strip().lower() == "admin":
        return "admin"
    return ""


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
        self._cache: Optional[tuple[Path, Optional[int], dict[str, dict[str, Any]]]] = None

    @property
    def path(self) -> Path:
        return self._explicit_path or default_tokens_path()

    @staticmethod
    def _normalize_attrs(raw: Any) -> dict[str, str]:
        if not isinstance(raw, dict):
            return {}
        out: dict[str, str] = {}
        for k, v in raw.items():
            key = str(k or "").strip()[:64]
            if not key:
                continue
            val = "" if v is None else str(v).strip()[:200]
            out[key] = val
            if len(out) >= 32:
                break
        return out

    @classmethod
    def _clone_entry(cls, entry: dict[str, Any]) -> dict[str, Any]:
        cp = dict(entry)
        if isinstance(entry.get("attrs"), dict):
            cp["attrs"] = dict(entry["attrs"])
        return cp

    @classmethod
    def _normalize_entry(cls, name: str, raw: Any) -> Optional[dict[str, Any]]:
        is_builtin = name in BUILTIN_ADMIN_NAMES
        if isinstance(raw, str):
            tok = raw.strip()
            if not tok:
                return None
            default_role = "admin" if (is_builtin or name == "admin") else "agent"
            out_s: dict[str, Any] = {"token": tok, "role": default_role}
            if is_builtin:
                out_s["protected"] = True
            return out_s
        if isinstance(raw, dict):
            tok = str(raw.get("token") or "").strip()
            if not tok:
                return None
            out: dict[str, Any] = {"token": tok}
            if raw.get("machine"):
                out["machine"] = str(raw["machine"]).strip()[:48]
            if raw.get("agent"):
                out["agent"] = str(raw["agent"]).strip()[:32]
            if raw.get("client"):
                out["client"] = str(raw["client"]).strip().lower()[:32]
            if is_builtin:
                out["role"] = "admin"
            else:
                role = str(raw.get("role") or ("admin" if name == "admin" else "agent")).strip().lower()
                out["role"] = "admin" if role == "admin" else "agent"
            if raw.get("note"):
                out["note"] = str(raw["note"]).strip()[:200]
            attrs = cls._normalize_attrs(raw.get("attrs"))
            if attrs:
                out["attrs"] = attrs
            if is_builtin or bool(raw.get("protected")):
                out["protected"] = True
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

    def entries(self) -> dict[str, dict[str, Any]]:
        p = self.path
        with self._lock:
            try:
                mtime: Optional[int] = p.stat().st_mtime_ns
            except OSError:
                mtime = None
            if self._cache is not None and self._cache[0] == p and self._cache[1] == mtime:
                return {k: self._clone_entry(v) for k, v in self._cache[2].items()}
            raw = self._read_raw()
            out: dict[str, dict[str, Any]] = {}
            for k, v in raw.items():
                entry = self._normalize_entry(str(k), v)
                if entry is not None:
                    out[str(k)] = entry
            try:
                mtime = p.stat().st_mtime_ns
            except OSError:
                pass
            self._cache = (p, mtime, out)
            return {k: self._clone_entry(v) for k, v in out.items()}

    def load(self) -> dict[str, str]:
        """兼容视图：{name: token_str}。"""
        return {k: str(v["token"]) for k, v in self.entries().items()}

    @contextmanager
    def _file_lock(self) -> Iterator[None]:
        p = self.path
        p.parent.mkdir(parents=True, exist_ok=True)
        lock_path = p.with_name(p.name + ".lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def add(
        self,
        name: str,
        *,
        role: str = "",
        machine: str = "",
        agent: str = "",
        client: str = "",
        note: Optional[str] = None,
        attrs: Optional[dict[str, Any]] = None,
        protected: Optional[bool] = None,
    ) -> str:
        key = str(name or "").strip()
        if not key:
            raise ValueError("令牌名称不能为空")
        tok = secrets.token_urlsafe(32)
        is_builtin = key in BUILTIN_ADMIN_NAMES
        with self._lock, self._file_lock():
            raw = self._read_raw()
            prev = self._normalize_entry(key, raw[key]) if key in raw else None
            eff_note = str(note).strip()[:200] if note is not None else (str(prev.get("note") or "") if prev else "")
            eff_attrs = (
                self._normalize_attrs(attrs)
                if attrs is not None
                else (dict(prev.get("attrs") or {}) if prev and isinstance(prev.get("attrs"), dict) else {})
            )
            eff_protected = True if is_builtin else (
                bool(protected) if protected is not None else bool(prev and prev.get("protected"))
            )
            eff_role = "admin" if (is_builtin or role == "admin" or (not role and key == "admin")) else "agent"
            if role or machine or agent or client or eff_note or eff_attrs or eff_protected:
                entry: dict[str, Any] = {"token": tok}
                if machine:
                    entry["machine"] = str(machine).strip()[:48]
                if agent:
                    entry["agent"] = str(agent).strip()[:32]
                if client:
                    entry["client"] = str(client).strip().lower()[:32]
                entry["role"] = eff_role
                if eff_note:
                    entry["note"] = eff_note
                if eff_attrs:
                    entry["attrs"] = eff_attrs
                if eff_protected:
                    entry["protected"] = True
                raw[key] = entry
            else:
                raw[key] = tok
            self._write_raw(raw)
            self._cache = None
        return tok

    def update(
        self,
        name: str,
        *,
        new_name: Optional[str] = None,
        role: Optional[str] = None,
        machine: Optional[str] = None,
        agent: Optional[str] = None,
        client: Optional[str] = None,
        note: Optional[str] = None,
        attrs: Optional[dict[str, Any]] = None,
        protected: Optional[bool] = None,
    ) -> dict[str, Any]:
        """编辑已有令牌的客户端属性（保留原 Bearer Token 不变，支持可选重命名）。"""
        key = str(name or "").strip()
        if not key:
            raise ValueError("令牌名称不能为空")
        with self._lock, self._file_lock():
            raw = self._read_raw()
            if key not in raw:
                raise KeyError(f"令牌不存在: {key}")
            cur = self._normalize_entry(key, raw[key])
            if cur is None:
                raise KeyError(f"令牌不存在: {key}")
            is_builtin = key in BUILTIN_ADMIN_NAMES
            target = str(new_name).strip() if new_name is not None and str(new_name).strip() else key
            if is_builtin and target != key:
                raise ValueError(f"系统内置管理员令牌 {key} 不允许重命名")
            if target != key and target in raw:
                raise ValueError(f"目标令牌名称已存在: {target}")

            eff_machine = (
                str(machine).strip()[:48]
                if machine is not None
                else str(cur.get("machine") or "")
            )
            eff_agent = (
                str(agent).strip()[:32]
                if agent is not None
                else str(cur.get("agent") or "")
            )
            eff_client = (
                str(client).strip().lower()[:32]
                if client is not None
                else str(cur.get("client") or "")
            )
            if role is not None and str(role).strip():
                eff_role = "admin" if str(role).strip().lower() == "admin" else "agent"
            else:
                eff_role = str(cur.get("role") or "agent")
            if is_builtin and eff_role != "admin":
                raise ValueError(f"系统内置管理员令牌 {key} 必须保持 admin 权限")
            if cur.get("role") == "admin" and eff_role != "admin":
                other_admins = [
                    k for k, v in raw.items()
                    if str(k) != key
                    and (ne := self._normalize_entry(str(k), v)) is not None
                    and ne.get("role") == "admin"
                ]
                if not other_admins:
                    raise ValueError("至少需要保留一个 admin 权限令牌，不能降级最后一个管理员")
            if is_builtin and protected is False:
                raise ValueError(f"系统内置管理员令牌 {key} 始终受保护，不可关闭防误删保护")
            eff_protected = True if is_builtin else (
                bool(protected) if protected is not None else bool(cur.get("protected"))
            )
            eff_note = (
                str(note).strip()[:200]
                if note is not None
                else str(cur.get("note") or "")
            )
            if attrs is not None:
                eff_attrs = self._normalize_attrs(attrs)
            else:
                eff_attrs = dict(cur.get("attrs") or {}) if isinstance(cur.get("attrs"), dict) else {}

            entry: dict[str, Any] = {"token": str(cur["token"])}
            if eff_machine:
                entry["machine"] = eff_machine
            if eff_agent:
                entry["agent"] = eff_agent
            if eff_client:
                entry["client"] = eff_client
            entry["role"] = eff_role
            if eff_note:
                entry["note"] = eff_note
            if eff_attrs:
                entry["attrs"] = eff_attrs
            if eff_protected:
                entry["protected"] = True

            if target != key:
                del raw[key]
            raw[target] = entry
            self._write_raw(raw)
            self._cache = None
            return self.resolve_entry(target, entry)

    def remove(self, name: str, *, allow_protected: bool = False) -> bool:
        key = str(name or "").strip()
        with self._lock, self._file_lock():
            raw = self._read_raw()
            if key not in raw:
                return False
            cur = self._normalize_entry(key, raw[key])
            if not allow_protected:
                if key in BUILTIN_ADMIN_NAMES:
                    raise ValueError(f"系统内置管理员令牌 {key} 受保护，禁止删除；如需更换密钥请使用轮换")
                if cur and cur.get("protected"):
                    raise ValueError(f"令牌「{key}」已开启防误删保护，请先在编辑属性中关闭保护后再删除")
                if cur and cur.get("role") == "admin":
                    other_admins = [
                        k for k, v in raw.items()
                        if str(k) != key
                        and (ne := self._normalize_entry(str(k), v)) is not None
                        and ne.get("role") == "admin"
                    ]
                    if not other_admins:
                        raise ValueError("至少需要保留一个 admin 权限令牌，禁止删除最后一个管理员")
            del raw[key]
            self._write_raw(raw)
            self._cache = None
            return True

    @staticmethod
    def resolve_entry(name: str, entry: dict[str, Any]) -> dict[str, Any]:
        builtin = name in BUILTIN_ADMIN_NAMES
        role = "admin" if builtin else str(entry.get("role") or "agent")
        protected = bool(builtin or entry.get("protected"))
        if entry.get("machine") or entry.get("agent"):
            mach = str(entry.get("machine") or (split_identity(name)[0] if "|" in name else name))
            ag = str(entry.get("agent") or (split_identity(name)[1] if "|" in name else name))
        elif "|" in name:
            mach, ag = split_identity(name)
        else:
            mach, ag = name, ("admin" if role == "admin" else name)
        return {
            "name": name,
            "machine": mach,
            "agent": ag,
            "client": infer_client(ag, str(entry.get("client") or ""), role=role),
            "role": role,
            "masked": mask(str(entry.get("token") or "")),
            "note": str(entry.get("note") or ""),
            "attrs": dict(entry.get("attrs") or {}) if isinstance(entry.get("attrs"), dict) else {},
            "builtin": builtin,
            "protected": protected,
        }

    def authorize(self, headers: Any, *, channel: str = "v1") -> Optional[TokenIdentity]:
        got = extract_bearer_token(headers)
        if not got:
            return None
        enc = got.encode("utf-8", "replace")
        for name, entry in self.entries().items():
            known = entry.get("token") or ""
            if known and hmac.compare_digest(enc, known.encode("utf-8", "replace")):
                info = self.resolve_entry(name, entry)
                return TokenIdentity(
                    name=info["name"],
                    machine=info["machine"],
                    agent=info["agent"],
                    role=info["role"],
                    client=info["client"],
                )
        return None
