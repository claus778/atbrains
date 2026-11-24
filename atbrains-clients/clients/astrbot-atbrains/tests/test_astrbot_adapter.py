"""AstrBot 薄适配器单元测试（零数据库驱动 + 离线队列）。"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SDK_DIR = ROOT.parents[1] / "sdk" / "python"
if str(SDK_DIR) not in sys.path:
    sys.path.insert(0, str(SDK_DIR))

spec = importlib.util.spec_from_file_location("astrbot_atbrains", ROOT / "main.py")
assert spec and spec.loader
astrbot_atbrains = importlib.util.module_from_spec(spec)
sys.modules["astrbot_atbrains"] = astrbot_atbrains
spec.loader.exec_module(astrbot_atbrains)


class FakeEvent:
    def __init__(self, text: str) -> None:
        self.message_str = text
        self.unified_msg_origin = "qq:group:12345"
        self._extra: dict[str, object] = {}

    def get_extra(self, k: str):
        return self._extra.get(k)

    def set_extra(self, k: str, v: object):
        self._extra[k] = v

    def get_platform_name(self) -> str:
        return "aiocqhttp"

    def get_sender_name(self) -> str:
        return "Alice"

    def get_sender_id(self) -> str:
        return "10001"


class FakeReq:
    system_prompt = "BASE"


class FakeResp:
    completion_text = "记住了，端口是 7622。"


class AstrBotAdapterTest(unittest.TestCase):
    def test_no_db_drivers(self) -> None:
        src = (ROOT / "main.py").read_text(encoding="utf-8")
        for forbidden in ("pymysql", "import redis", "SELECT ", "INSERT INTO"):
            self.assertNotIn(forbidden, src)

    def test_recall_archive_and_tools(self) -> None:
        seen: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self._send(200, {"ok": True, "version": "2.1.0"})

            def do_POST(self):
                seen.append((self.path, self.headers.get("X-Atbrains-Client") or ""))
                n = int(self.headers.get("Content-Length") or 0)
                _ = json.loads(self.rfile.read(n).decode() or "{}")
                if self.path == "/v1/memory/recall":
                    self._send(200, {"ok": True, "text": "[mem] SSH=7622", "count": 1, "items": [{"id": 1}]})
                    return
                if self.path == "/v1/memory/remember":
                    self._send(200, {"ok": True, "saved": {"id": 88, "kind": "fact", "importance": 8}})
                    return
                if self.path == "/v1/memory/turn":
                    self._send(200, {"ok": True, "id": 55, "archived": 0})
                    return
                self._send(200, {"ok": True})

            def _send(self, code: int, obj: dict):
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
                plugin = astrbot_atbrains.AtbrainsMemory(
                    None,
                    config={
                        "base_url": f"http://127.0.0.1:{srv.server_port}",
                        "token": "tok-astr",
                        "spool_dir": tmp,
                        "archive_async": False,
                    },
                )
                ev = FakeEvent("SSH 端口是多少")
                req = FakeReq()

                async def run():
                    await plugin.on_llm_request(ev, req)
                    self.assertIn("SSH=7622", req.system_prompt)
                    await plugin.on_agent_done(ev, None, FakeResp())
                    save_out = json.loads(await plugin.memory_save(ev, "SSH 端口是 7622", kind="fact", importance=8))
                    self.assertEqual(save_out["id"], 88)
                    search_out = await plugin.memory_search(ev, "SSH")
                    self.assertIn("SSH=7622", search_out)

                asyncio.run(run())
                paths = [p for p, _ in seen]
                self.assertIn("/v1/memory/recall", paths)
                self.assertIn("/v1/memory/turn", paths)
                self.assertIn("/v1/memory/remember", paths)
                self.assertTrue(all(hdr == "astrbot" for _, hdr in seen))
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
