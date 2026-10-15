#!/usr/bin/env python3
"""atbrains 网页控制台 —— HTTP 层。

* 数据一律走父目录的 probe.py(与 `hermes atbrains` CLI、agent 工具同一份逻辑)。
* 凭据只在服务端解析,绝不下发浏览器。
* 所有 /api/* 需要 token(存在本目录 .token,权限 600,首次运行自动生成)。
* /v1/* 是 **Memory Service**(service.py):与 /api/* 共用这一个端口,但令牌体系独立
  (Authorization: Bearer <service-tokens.json 里的令牌>),走的是另一套路由与鉴权。

启动方式(不是系统服务):
    hermes atbrains serve              前台
    hermes atbrains serve --background 后台(pidfile)
    hermes atbrains stop               停止
"""
from __future__ import annotations

import json
import os
import secrets
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # 让 `import probe` / `import service` 命中插件根目录
import probe  # noqa: E402
import service  # noqa: E402

SERVICE = service.MemoryService()   # /v1 Memory Service(每 machine 一个 BrainStore,内部加锁)
TOKEN_PATH = HERE / ".token"
CONF_PATH = HERE / "config.json"
DEFAULT_PORT = 8787


def load_conf() -> dict:
    try:
        c = json.loads(CONF_PATH.read_text(encoding="utf-8"))
    except Exception:
        c = {}
    c.setdefault("host", "0.0.0.0")
    c.setdefault("port", DEFAULT_PORT)
    if os.environ.get("ATBRAINS_PORT"):
        try:
            c["port"] = int(os.environ["ATBRAINS_PORT"])
        except ValueError:
            pass
    return c


def token() -> str:
    if not TOKEN_PATH.exists():
        TOKEN_PATH.write_text(secrets.token_urlsafe(24), encoding="utf-8")
        TOKEN_PATH.chmod(0o600)
    return TOKEN_PATH.read_text(encoding="utf-8").strip()


def jdump(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "atbrains-console/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, body: bytes, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _authed(self, q) -> bool:
        got = self.headers.get("X-Auth-Token") or (q.get("token", [""])[0])
        return got == token()

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n).decode() or "{}")
        except Exception:
            return {}

    def _v1(self, method: str, query: dict, body: dict):
        """/v1/* 交给 MemoryService(原始 headers + 解析后的 body;鉴权由 service 自己判)。"""
        headers = dict(self.headers.items())
        flat = {k: v[0] for k, v in query.items()}
        code, payload = SERVICE.handle(method, urlparse(self.path).path, flat, body, headers)
        return self._send(code, jdump(payload))

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        path = u.path
        if path.startswith("/v1/"):
            return self._v1("GET", q, {})
        if path in ("/", "/index.html"):
            p = HERE / "index.html"
            if not p.exists():
                return self._send(404, b"index.html missing", "text/plain")
            return self._send(200, p.read_bytes(), "text/html; charset=utf-8")
        if path == "/api/whoami":
            return self._send(200, jdump({"ok": True}))
        if not path.startswith("/api/"):
            return self._send(404, jdump({"error": "not found"}))
        if not self._authed(q):
            return self._send(401, jdump({"error": "unauthorized", "hint": "在页面右上角填入 token"}))
        try:
            if path == "/api/overview":
                return self._send(200, jdump(probe.overview()))
            if path == "/api/memories":
                return self._send(200, jdump(probe.memories(
                    limit=min(int(q.get("limit", ["100"])[0]), 500),
                    offset=int(q.get("offset", ["0"])[0]),
                    active=q.get("active", ["1"])[0],
                    q=q.get("q", [""])[0],
                    kind=q.get("kind", [""])[0],
                    machine=q.get("machine", [""])[0])))
            if path == "/api/memory":
                return self._send(200, jdump(probe.memory_detail(int(q.get("id", ["0"])[0]))))
            if path == "/api/turns":
                return self._send(200, jdump(probe.turns(
                    limit=min(int(q.get("limit", ["40"])[0]), 300),
                    offset=int(q.get("offset", ["0"])[0]),
                    machine=q.get("machine", [""])[0],
                    session_id=q.get("session_id", [""])[0],
                    q=q.get("q", [""])[0])))
            if path == "/api/turn":
                return self._send(200, jdump(probe.turn_detail(int(q.get("id", ["0"])[0]))))
            if path == "/api/sessions":
                return self._send(200, jdump(probe.sessions(
                    limit=min(int(q.get("limit", ["100"])[0]), 500),
                    offset=int(q.get("offset", ["0"])[0]),
                    machine=q.get("machine", [""])[0],
                    q=q.get("q", [""])[0])))
            if path == "/api/search":
                return self._send(200, jdump(probe.search(
                    q.get("q", [""])[0], limit=min(int(q.get("limit", ["10"])[0]), 50))))
            if path == "/api/recent":
                return self._send(200, jdump(probe.recent(limit=min(int(q.get("limit", ["20"])[0]), 100))))
            if path == "/api/qdrant":
                return self._send(200, jdump(probe.qdrant_info()))
            return self._send(404, jdump({"error": "unknown endpoint"}))
        except Exception as e:
            return self._send(500, jdump({"error": f"{type(e).__name__}: {e}"}))

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path.startswith("/v1/"):
            return self._v1("POST", q, self._body())
        if not self._authed(q):
            return self._send(401, jdump({"error": "unauthorized"}))
        body = self._body()
        try:
            if u.path == "/api/remember":
                return self._send(200, jdump({"ok": True, "saved": probe.remember(
                    (body.get("content") or "").strip(), kind=body.get("kind") or "note",
                    tags=body.get("tags") or "", importance=int(body.get("importance") or 5))}))
            if u.path == "/api/forget":
                return self._send(200, jdump({"ok": True, "result": probe.forget(int(body.get("id")))}))
            if u.path == "/api/restore":
                try:
                    return self._send(200, jdump({"ok": True,
                                                  "result": probe.restore(int(body.get("id") or 0))}))
                except Exception as e:  # noqa: BLE001 —— 业务性拒绝(不存在/同内容已有活跃条目)
                    return self._send(200, jdump({"ok": False, "error": str(e)}))
            if u.path == "/api/drain":
                return self._send(200, jdump(probe.drain()))
            if u.path == "/api/doctor":
                return self._send(200, jdump(probe.doctor()))
            if u.path == "/api/prune":
                return self._send(200, jdump({"ok": True,
                                              "result": probe.prune(dry_run=bool(body.get("dry_run", True)))}))
            return self._send(404, jdump({"error": "unknown endpoint"}))
        except Exception as e:
            return self._send(500, jdump({"error": f"{type(e).__name__}: {e}"}))


def main() -> None:
    conf = load_conf()
    host, port = conf["host"], int(conf["port"])
    token()
    print(f"atbrains 控制台 → http://{host}:{port}/", flush=True)
    print(f"atbrains Memory Service → http://{host}:{port}/v1/health", flush=True)
    print(f"口令文件 {TOKEN_PATH}(权限 600)", flush=True)
    print(f"服务令牌 {service.TOKENS_PATH}({len(service.load_tokens())} 个 machine,权限 600)", flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
