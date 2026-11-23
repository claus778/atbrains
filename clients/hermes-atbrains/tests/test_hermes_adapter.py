"""Hermes 薄适配器单元测试（零数据库驱动，验证全生命周期 + 12 工具 + 离线队列）。"""
from __future__ import annotations

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

spec = importlib.util.spec_from_file_location("hermes_atbrains", ROOT / "__init__.py")
assert spec and spec.loader
hermes_atbrains = importlib.util.module_from_spec(spec)
sys.modules["hermes_atbrains"] = hermes_atbrains
spec.loader.exec_module(hermes_atbrains)


class HermesAdapterTest(unittest.TestCase):
    def test_no_db_imports(self) -> None:
        src = (ROOT / "__init__.py").read_text(encoding="utf-8")
        cli_src = (ROOT / "cli.py").read_text(encoding="utf-8")
        for forbidden in ("pymysql", "import redis", "ensure_schema", "SELECT "):
            self.assertNotIn(forbidden, src)
            self.assertNotIn(forbidden, cli_src)

    def test_lifecycle_and_tools(self) -> None:
        seen: list[tuple[str, dict]] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path == "/v1/health":
                    self._send(200, {"ok": True, "version": "2.1.0"})
                    return
                self._send(404, {"ok": False})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n).decode() or "{}")
                seen.append((self.path, body, self.headers.get("X-Atbrains-Client") or "", self.headers.get("Authorization") or ""))
                if self.path == "/v1/memory/status":
                    if self.headers.get("Authorization") == "Bearer bad-tok":
                        self._send(401, {"ok": False, "error": "unauthorized"})
                        return
                    self._send(200, {"ok": True, "name": "dev|hermes", "machine": "dev", "agent": "hermes", "client": "hermes", "role": "agent", "ping": {"redis": True, "mariadb": True}})
                    return
                if self.path == "/v1/memory/recall":
                    self._send(200, {"ok": True, "text": "[mem] 端口是 7622", "count": 1,
                                     "items": [{"id": 9, "kind": "fact"}], "claims": [], "turns": []})
                    return
                if self.path == "/v1/memory/remember":
                    self._send(200, {"ok": True, "saved": {"id": 99, "kind": body.get("kind")}})
                    return
                if self.path == "/v1/memory/turn":
                    self._send(200, {"ok": True, "id": 12, "archived": 2})
                    return
                if self.path == "/v1/context":
                    self._send(200, {"ok": True, "agent": "hermes", "memories": []})
                    return
                if self.path == "/v1/handoff":
                    self._send(200, {"ok": True, "saved": {"id": 100}})
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
                home = Path(tmp)
                (home / "atbrains.json").write_text(
                    json.dumps({"service_url": f"http://127.0.0.1:{srv.server_port}"}),
                    encoding="utf-8",
                )
                (home / ".env").write_text("ATBRAINS_REMOTE_TOKEN=tok-h\n", encoding="utf-8")

                prov = hermes_atbrains.AtbrainsMemoryProvider()
                prov.initialize("sess-1", hermes_home=str(home), platform="weixin")

                self.assertEqual(len(prov.get_tool_schemas()), 12)
                text = prov.prefetch("SSH 端口")
                self.assertIn("7622", text)
                self.assertEqual(prov.recall_status().count, 1)

                prov.sync_turn("你好", "在的", messages=[{"role": "user", "content": "你好"}])
                prov.on_memory_write("replace", "MEMORY.md", "新事实", {"previous_content": "旧事实"})
                prov.on_pre_compress([{"role": "user", "content": "上下文"}])
                prov.on_session_end([])

                r_save = json.loads(prov.handle_tool_call("atbrains_remember", {"content": "记住", "kind": "fact"}))
                self.assertTrue(r_save["ok"])
                r_ctx = json.loads(prov.handle_tool_call("atbrains_context", {"focus": "端口"}))
                self.assertTrue(r_ctx["ok"])
                r_ho = json.loads(prov.handle_tool_call("atbrains_handoff", {"note": "接力"}))
                self.assertTrue(r_ho["ok"])

                paths = [p for p, _, _, _ in seen]
                self.assertIn("/v1/memory/recall", paths)
                self.assertIn("/v1/memory/turn", paths)
                self.assertIn("/v1/memory/deactivate", paths)
                self.assertIn("/v1/memory/archive", paths)
                self.assertIn("/v1/memory/session/touch", paths)
                self.assertIn("/v1/context", paths)
                self.assertIn("/v1/handoff", paths)
                # Hermes 适配器必须上报 X-Atbrains-Client: hermes
                self.assertTrue(all(hdr == "hermes" for _, _, hdr, _ in seen))

                # 验证 cli.py tokset / tokshow 命令
                cli_spec = importlib.util.spec_from_file_location("hermes_atbrains.cli", ROOT / "cli.py")
                assert cli_spec and cli_spec.loader
                cli_mod = importlib.util.module_from_spec(cli_spec)
                cli_mod.__package__ = "hermes_atbrains"
                sys.modules["hermes_atbrains.cli"] = cli_mod
                cli_spec.loader.exec_module(cli_mod)

                import argparse
                parser = argparse.ArgumentParser()
                cli_mod.register_cli(parser)
                home2 = Path(tmp) / "h2"
                args_ok = parser.parse_args(["tokset", "tok-h-new", "--url", f"http://127.0.0.1:{srv.server_port}", "--home", str(home2)])
                self.assertEqual(cli_mod._cmd_tokset(args_ok), 0)
                self.assertIn("ATBRAINS_TOKEN=tok-h-new", (home2 / ".env").read_text(encoding="utf-8"))
                self.assertIn(f"http://127.0.0.1:{srv.server_port}", (home2 / "atbrains.json").read_text(encoding="utf-8"))
                args_bad = parser.parse_args(["tokset", "bad-tok", "--url", f"http://127.0.0.1:{srv.server_port}", "--home", str(home2)])
                self.assertEqual(cli_mod._cmd_tokset(args_bad), 1)
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
