"""atbrains-client SDK 单元测试（离线队列 + HTTP 客户端 + 协议校验）。"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atbrains_client import (  # noqa: E402
    AtbrainsClient,
    AtbrainsClientError,
    OfflineSpool,
    normalize_base_url,
    resolve_client_config,
)


class ConfigTest(unittest.TestCase):
    def test_normalize_url(self) -> None:
        self.assertEqual(normalize_base_url("http://127.0.0.1:8787/mcp"), "http://127.0.0.1:8787")
        self.assertEqual(normalize_base_url("10.0.0.2:8787/v1/"), "http://10.0.0.2:8787")
        self.assertEqual(normalize_base_url(""), "")

    def test_resolve_from_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            (p / "client.json").write_text(json.dumps({
                "url": "http://mem.local:8787",
                "timeout_ms": 900,
                "write_timeout_ms": 5000,
            }), encoding="utf-8")
            (p / ".env").write_text("ATBRAINS_TOKEN=tok-123\n", encoding="utf-8")
            cfg = resolve_client_config(str(p))
            self.assertEqual(cfg.base_url, "http://mem.local:8787")
            self.assertEqual(cfg.token, "tok-123")
            self.assertEqual(cfg.timeout_ms, 900)
            self.assertEqual(cfg.write_timeout_ms, 5000)


class SpoolTest(unittest.TestCase):
    def test_spool_fifo_transient_and_deadletter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sp = OfflineSpool(tmp, max_bytes=8192)
            self.assertTrue(sp.enqueue("/v1/memory/remember", {"content": "m1"}))
            self.assertTrue(sp.enqueue("/v1/memory/remember", {"content": "bad"}))
            self.assertTrue(sp.enqueue("/v1/memory/turn", {"session_id": "s1", "user": "hi"}))
            self.assertEqual(sp.count(), 3)

            seen: list[str] = []

            def sender(path: str, payload: dict) -> dict:
                if payload.get("content") == "bad":
                    raise AtbrainsClientError("bad request", status=400, transient=False)
                if path == "/v1/memory/turn" and "retry_done" not in seen:
                    seen.append("retry_done")
                    raise AtbrainsClientError("server down", status=503, transient=True)
                seen.append(path)
                return {"ok": True}

            res1 = sp.drain(sender, is_transient=lambda e: isinstance(e, AtbrainsClientError) and e.transient)
            self.assertEqual(res1, {"sent": 1, "dropped": 1, "remaining": 1})
            self.assertEqual(sp.count(), 1)
            self.assertTrue(sp.dead_path.exists())

            res2 = sp.drain(sender, is_transient=lambda e: isinstance(e, AtbrainsClientError) and e.transient)
            self.assertEqual(res2, {"sent": 1, "dropped": 0, "remaining": 0})
            self.assertEqual(sp.count(), 0)
            self.assertIn("/v1/memory/turn", seen)


class HttpClientTest(unittest.TestCase):
    def test_client_roundtrip_cache_and_spool(self) -> None:
        calls: list[tuple[str, str, dict]] = []
        fail_writes = {"on": False}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path == "/v1/health":
                    self._json(200, {"ok": True, "version": "2.1.0"})
                    return
                self._json(404, {"ok": False})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n).decode() or "{}")
                calls.append(("POST", self.path, body))
                if self.path == "/v1/memory/recall":
                    self._json(200, {"ok": True, "text": "[mem] ok", "count": 1, "items": [{"id": 1}]})
                    return
                if fail_writes["on"]:
                    self._json(503, {"ok": False, "error": "temporarily unavailable"})
                    return
                if self.path == "/v1/memory/remember":
                    self._json(200, {"ok": True, "saved": {"id": 42}})
                    return
                self._json(200, {"ok": True})

            def _json(self, code: int, obj: dict):
                raw = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                cli = AtbrainsClient(
                    f"http://127.0.0.1:{srv.server_port}",
                    "tok-abc",
                    spool_dir=tmp,
                    recall_cache_ttl=60,
                )
                self.assertTrue(cli.protocol_ok())
                r1 = cli.recall("test query")
                r2 = cli.recall("test query")
                self.assertEqual(r1["count"], 1)
                self.assertEqual(r2["count"], 1)
                # 第二次命中客户端缓存
                self.assertEqual(sum(1 for _, p, _ in calls if p == "/v1/memory/recall"), 1)

                # 瞬时 503 写入自动进离线队列
                fail_writes["on"] = True
                queued = cli.remember("offline fact", kind="fact")
                self.assertTrue(queued.get("queued"))
                self.assertEqual(cli.spool_status()["pending"], 1)

                # 服务端恢复后下次写入自动重放队列
                fail_writes["on"] = False
                res = cli.remember("online fact", kind="fact")
                self.assertEqual(res["saved"]["id"], 42)
                self.assertEqual(cli.spool_status()["pending"], 0)
                remembered = [b["content"] for _, p, b in calls if p == "/v1/memory/remember"]
                self.assertIn("offline fact", remembered)
                self.assertIn("online fact", remembered)
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
