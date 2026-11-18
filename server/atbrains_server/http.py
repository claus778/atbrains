#!/usr/bin/env python3
"""atbrains 统一 HTTP 服务（/v1 API + /mcp + 网页控制台 /api/* + 静态页面）。

* 所有凭据与存储读写只在服务端进程内进行，绝不下发浏览器或客户端。
* /v1/* 与 /mcp 使用 `service-tokens.json` 的 Bearer 令牌（按 `<machine>|<agent>` 识别身份）。
* 网页控制台 `/api/*` 使用 `.token`（`X-Auth-Token` / `?token=`），可访问 `/v1/admin/*`。
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from atbrains_server import __version__
from atbrains_server.config import resolve_server_http_config
from atbrains_server.mcp import AtbrainsMCP
from atbrains_server.service import MemoryService

HERE = Path(__file__).resolve().parent
CONSOLE_HTML = HERE / "console" / "index.html"


def jdump(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")


class ConsoleTokenStore:
    """控制台访问口令管理器（按 mtime 缓存，权限 600）。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._cache: Optional[tuple[int, str]] = None

    def get(self) -> str:
        try:
            st = self.path.stat()
        except OSError:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(secrets.token_urlsafe(24))
            os.chmod(self.path, 0o600)
            st = self.path.stat()
        if self._cache is not None and self._cache[0] == st.st_mtime_ns:
            return self._cache[1]
        value = self.path.read_text(encoding="utf-8").strip()
        self._cache = (st.st_mtime_ns, value)
        return value


class ServerRuntime:
    """把 MemoryService / AtbrainsMCP / AdminOps / ConsoleToken / AccessLog 绑定到一起。"""

    def __init__(
        self,
        *,
        config_home: Optional[str] = None,
        data_dir: Optional[Path] = None,
        start_workers: bool = True,
        access_log: Optional[str] = None,
        service: Optional[MemoryService] = None,
    ) -> None:
        http_cfg = resolve_server_http_config(config_home)
        self.data_dir = Path(data_dir) if data_dir is not None else Path(http_cfg["data_dir"])
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.console_token = ConsoleTokenStore(self.data_dir / ".token")
        self.service = service or MemoryService(
            config_home=config_home,
            tokens_path=self.data_dir / "service-tokens.json",
            start_workers=start_workers,
            console_token_fn=self.console_token.get,
        )
        self.mcp = AtbrainsMCP(self.service)
        self.access_log_path = access_log if access_log is not None else str(http_cfg["access_log"])
        self._log_fh = None

    def log_access(self, client_addr: str, fmt: str, args: tuple) -> None:
        if self.access_log_path == "-":
            return
        try:
            line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {client_addr} {fmt % args}\n"
            if self._log_fh is None:
                p = Path(self.access_log_path)
                p.parent.mkdir(parents=True, exist_ok=True)
                self._log_fh = open(p, "a", encoding="utf-8", buffering=1)
            self._log_fh.write(line)
        except Exception:  # noqa: BLE001
            self._log_fh = None


def make_handler(runtime: ServerRuntime):
    """生成绑定指定 ServerRuntime 的 HTTP Handler 类（便于多实例单元测试）。"""

    class Handler(BaseHTTPRequestHandler):
        server_version = f"atbrains-server/{__version__}"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            runtime.log_access(self.address_string(), fmt, args)

        def handle_one_request(self):
            try:
                super().handle_one_request()
            except (ConnectionResetError, BrokenPipeError):
                self.close_connection = True

        def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if body:
                try:
                    self.wfile.write(body)
                except BrokenPipeError:
                    pass

        def _authed_console(self, q: dict) -> bool:
            got = str(self.headers.get("X-Auth-Token") or (q.get("token", [""])[0]) or "")
            expected = runtime.console_token.get()
            return bool(got and expected and hmac.compare_digest(got.encode("utf-8"), expected.encode("utf-8")))

        def _body(self) -> dict:
            try:
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            except Exception:  # noqa: BLE001
                return {}

        def _v1(self, method: str, query: dict, body: dict):
            headers = dict(self.headers.items())
            flat = {k: v[0] for k, v in query.items()}
            code, payload = runtime.service.handle(method, urlparse(self.path).path, flat, body, headers)
            return self._send(code, jdump(payload))

        def _mcp(self):
            headers = dict(self.headers.items())
            ident = runtime.service.authorize_identity(headers, channel="mcp")
            if ident is None:
                return self._send(401, jdump({
                    "jsonrpc": "2.0", "id": None,
                    "error": {"code": -32001, "message": "unauthorized"},
                }))
            try:
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n).decode("utf-8") if n else ""
                payload = json.loads(raw) if raw.strip() else None
            except Exception:  # noqa: BLE001
                return self._send(400, jdump({
                    "jsonrpc": "2.0", "id": None,
                    "error": {"code": -32700, "message": "parse error"},
                }))
            code, obj, extra = runtime.mcp.handle(ident.agent, payload)
            body = b"" if obj is None else jdump(obj)
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if body:
                try:
                    self.wfile.write(body)
                except BrokenPipeError:
                    pass

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            path = u.path
            if path == "/mcp":
                return self._send(405, jdump({
                    "jsonrpc": "2.0", "id": None,
                    "error": {"code": -32000, "message": "use POST /mcp (streamable HTTP, JSON response)"},
                }))
            if path.startswith("/v1/"):
                return self._v1("GET", q, {})
            if path in ("/", "/index.html"):
                if not CONSOLE_HTML.exists():
                    return self._send(404, b"index.html missing", "text/plain")
                return self._send(200, CONSOLE_HTML.read_bytes(), "text/html; charset=utf-8")
            if path == "/api/whoami":
                return self._send(200, jdump({"ok": True}))
            if not path.startswith("/api/"):
                return self._send(404, jdump({"error": "not found"}))
            if not self._authed_console(q):
                return self._send(401, jdump({"error": "unauthorized", "hint": "在页面右上角填入 token"}))
            ops = runtime.service.admin_ops()
            try:
                if path == "/api/overview":
                    fresh = str(q.get("fresh", ["0"])[0]).strip().lower() in ("1", "true", "yes")
                    return self._send(200, jdump(ops.overview(fresh=fresh)))
                if path == "/api/memories":
                    return self._send(200, jdump(ops.memories(
                        limit=min(int(q.get("limit", ["100"])[0]), 500),
                        offset=int(q.get("offset", ["0"])[0]),
                        active=q.get("active", ["1"])[0],
                        q=q.get("q", [""])[0],
                        kind=q.get("kind", [""])[0],
                        machine=q.get("machine", [""])[0],
                        agent=q.get("agent", [""])[0],
                    )))
                if path == "/api/memory":
                    return self._send(200, jdump(ops.memory_detail(int(q.get("id", ["0"])[0]))))
                if path == "/api/turns":
                    return self._send(200, jdump(ops.turns(
                        limit=min(int(q.get("limit", ["40"])[0]), 300),
                        offset=int(q.get("offset", ["0"])[0]),
                        machine=q.get("machine", [""])[0],
                        session_id=q.get("session_id", [""])[0],
                        q=q.get("q", [""])[0],
                    )))
                if path == "/api/turn":
                    return self._send(200, jdump(ops.turn_detail(int(q.get("id", ["0"])[0]))))
                if path == "/api/sessions":
                    return self._send(200, jdump(ops.sessions(
                        limit=min(int(q.get("limit", ["100"])[0]), 500),
                        offset=int(q.get("offset", ["0"])[0]),
                        machine=q.get("machine", [""])[0],
                        q=q.get("q", [""])[0],
                    )))
                if path == "/api/search":
                    return self._send(200, jdump(ops.search(
                        q.get("q", [""])[0], limit=min(int(q.get("limit", ["10"])[0]), 50))))
                if path == "/api/recent":
                    return self._send(200, jdump(ops.recent(limit=min(int(q.get("limit", ["20"])[0]), 100))))
                if path == "/api/qdrant":
                    return self._send(200, jdump(ops.qdrant_info()))
                return self._send(404, jdump({"error": "unknown endpoint"}))
            except Exception as e:  # noqa: BLE001
                return self._send(500, jdump({"error": f"{type(e).__name__}: {e}"}))

        def do_POST(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path == "/mcp":
                return self._mcp()
            if u.path.startswith("/v1/"):
                return self._v1("POST", q, self._body())
            if not self._authed_console(q):
                return self._send(401, jdump({"error": "unauthorized"}))
            body = self._body()
            ops = runtime.service.admin_ops()
            try:
                if u.path == "/api/remember":
                    saved = ops.remember(
                        (body.get("content") or "").strip(),
                        kind=body.get("kind") or "note",
                        tags=body.get("tags") or "",
                        importance=int(body.get("importance") or 5),
                    )
                    return self._send(200, jdump({"ok": True, "saved": saved}))
                if u.path == "/api/forget":
                    return self._send(200, jdump({"ok": True, "result": ops.forget(int(body.get("id")))}))
                if u.path == "/api/restore":
                    try:
                        result = ops.restore(int(body.get("id") or 0))
                        return self._send(200, jdump({"ok": True, "result": result}))
                    except Exception as e:  # noqa: BLE001
                        return self._send(200, jdump({"ok": False, "error": str(e)}))
                if u.path == "/api/drain":
                    return self._send(200, jdump(ops.drain()))
                if u.path == "/api/doctor":
                    return self._send(200, jdump(ops.doctor()))
                if u.path == "/api/prune":
                    return self._send(200, jdump({"ok": True, "result": ops.prune(dry_run=bool(body.get("dry_run", True)))}))
                return self._send(404, jdump({"error": "unknown endpoint"}))
            except Exception as e:  # noqa: BLE001
                return self._send(500, jdump({"error": f"{type(e).__name__}: {e}"}))

        def do_DELETE(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path == "/mcp":
                return self._send(200, b"", "text/plain")
            if u.path.startswith("/v1/"):
                return self._v1("DELETE", q, self._body())
            return self._send(404, jdump({"error": "not found"}))

    return Handler


def create_http_server(
    host: str = "0.0.0.0",
    port: int = 8787,
    *,
    config_home: Optional[str] = None,
    data_dir: Optional[Path] = None,
    start_workers: bool = True,
    access_log: Optional[str] = None,
) -> tuple[ThreadingHTTPServer, ServerRuntime]:
    runtime = ServerRuntime(
        config_home=config_home,
        data_dir=data_dir,
        start_workers=start_workers,
        access_log=access_log,
    )
    runtime.console_token.get()
    runtime.service.tokens.load()
    server = ThreadingHTTPServer((host, int(port)), make_handler(runtime))
    return server, runtime


def main() -> None:
    cfg = resolve_server_http_config()
    host, port = cfg["host"], int(cfg["port"])
    server, runtime = create_http_server(host, port, start_workers=True)
    print(f"atbrains-server v{__version__} → http://{host}:{port}/", flush=True)
    print(f"atbrains /v1 API      → http://{host}:{port}/v1/health", flush=True)
    print(f"atbrains /mcp         → http://{host}:{port}/mcp", flush=True)
    print(f"控制台口令文件         {runtime.console_token.path} (权限 600)", flush=True)
    print(f"服务令牌表             {runtime.service.tokens.path} ({len(runtime.service.tokens.load())} 个身份, 权限 600)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
