"""atbrains 端到端集成测试（连接真实 MariaDB + Redis + Qdrant）。

两种运行方式：
  1. 指定已有测试库环境变量：ATBRAINS_IT_MARIADB_PORT=... ATBRAINS_IT_REDIS_PORT=... ATBRAINS_IT_QDRANT_PORT=...
  2. 自动启停临时 Docker 容器（ATBRAINS_DOCKER_IT=1，测试结束自动清理）。
未配置上述任一条件时自动跳过，确保纯单元测试环境可安全跑全套测试。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
SDK_DIR = REPO / "sdk" / "python"
for p in (str(ROOT), str(SDK_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from atbrains_client import AtbrainsClient  # noqa: E402
from atbrains_server import PROTOCOL_VERSION, AtbrainsStore  # noqa: E402
from atbrains_server.config import DEFAULT_CONFIG  # noqa: E402
from atbrains_server.http import create_http_server  # noqa: E402
from atbrains_server.schema import SCHEMA_VERSION  # noqa: E402


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_tcp(host: str, port: int, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return
        except OSError:
            time.sleep(0.5)
    raise TimeoutError(f"{host}:{port} not ready in {timeout}s")


def _wait_http(url: str, timeout: float = 45.0) -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with opener.open(url, timeout=2.0) as r:
                if r.status < 500:
                    return
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
    raise TimeoutError(f"{url} not ready in {timeout}s")


def _start_mock_embed(dim: int = 8) -> tuple[ThreadingHTTPServer, str]:
    """确定性本地假 embedding 服务，用来真测 Qdrant 建集合、向量入库与语义召回。"""
    class EmbedHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n).decode() or "{}")
            inputs = body.get("input") or []
            data = []
            for idx, text in enumerate(inputs):
                s = str(text)
                # 让包含 "端口"/"7622"/"ssh" 的向量高度接近
                v = [0.1] * dim
                if any(k in s.lower() for k in ("端口", "7622", "ssh")):
                    v[0] = 0.98
                    v[1] = 0.15
                elif "数据库" in s or "mariadb" in s.lower():
                    v[2] = 0.98
                    v[3] = 0.15
                else:
                    v[-1] = 0.9
                data.append({"index": idx, "embedding": v})
            raw = json.dumps({"data": data}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), EmbedHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}/v1"


def _enabled() -> bool:
    return bool(os.environ.get("ATBRAINS_DOCKER_IT") or os.environ.get("ATBRAINS_IT_MARIADB_PORT"))


@unittest.skipUnless(_enabled(), "设置 ATBRAINS_DOCKER_IT=1 或 ATBRAINS_IT_* 运行真库集成测试")
class EndToEndIntegrationTest(unittest.TestCase):
    containers: list[str] = []
    ports: dict[str, int] = {}
    embed_srv: ThreadingHTTPServer | None = None
    embed_url: str = ""

    @classmethod
    def setUpClass(cls) -> None:
        cls.embed_srv, cls.embed_url = _start_mock_embed(8)
        if os.environ.get("ATBRAINS_IT_MARIADB_PORT"):
            cls.ports = {
                "mariadb": int(os.environ["ATBRAINS_IT_MARIADB_PORT"]),
                "redis": int(os.environ["ATBRAINS_IT_REDIS_PORT"]),
                "qdrant": int(os.environ["ATBRAINS_IT_QDRANT_PORT"]),
            }
            return

        suffix = str(os.getpid())
        p_maria, p_redis, p_qdrant = _free_port(), _free_port(), _free_port()
        cls.ports = {"mariadb": p_maria, "redis": p_redis, "qdrant": p_qdrant}
        names = [f"atbrains-it-maria-{suffix}", f"atbrains-it-redis-{suffix}", f"atbrains-it-qdrant-{suffix}"]
        cls.containers = names
        try:
            subprocess.run([
                "docker", "run", "-d", "--rm", "--name", names[0],
                "-p", f"127.0.0.1:{p_maria}:3306",
                "-e", "MARIADB_ROOT_PASSWORD=rootpass",
                "-e", "MARIADB_DATABASE=agent_memory",
                "-e", "MARIADB_USER=agent",
                "-e", "MARIADB_PASSWORD=itpass",
                "mariadb:11", "--character-set-server=utf8mb4", "--collation-server=utf8mb4_unicode_ci",
            ], check=True, stdout=subprocess.DEVNULL)
            subprocess.run([
                "docker", "run", "-d", "--rm", "--name", names[1],
                "-p", f"127.0.0.1:{p_redis}:6379",
                "redis:7-alpine", "redis-server", "--requirepass", "itpass",
            ], check=True, stdout=subprocess.DEVNULL)
            subprocess.run([
                "docker", "run", "-d", "--rm", "--name", names[2],
                "-p", f"127.0.0.1:{p_qdrant}:6333",
                "qdrant/qdrant:latest",
            ], check=True, stdout=subprocess.DEVNULL)

            _wait_tcp("127.0.0.1", p_redis, 30)
            _wait_http(f"http://127.0.0.1:{p_qdrant}/collections", 45)
            # MariaDB 需要等建库完成且能真正 SELECT 1
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                r = subprocess.run(
                    ["docker", "exec", names[0], "mariadb", "-uagent", "-pitpass", "agent_memory", "-e", "SELECT 1"],
                    capture_output=True, text=True,
                )
                if r.returncode == 0:
                    break
                time.sleep(1.0)
            else:
                raise TimeoutError("MariaDB not ready in 90s")
        except Exception:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        for name in cls.containers:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        cls.containers = []
        if cls.embed_srv is not None:
            cls.embed_srv.shutdown()
            cls.embed_srv.server_close()
            cls.embed_srv = None

    def test_full_universal_multi_agent_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            cfg = {
                "mariadb_host": "127.0.0.1",
                "mariadb_port": self.ports["mariadb"],
                "mariadb_user": "agent",
                "mariadb_db": "agent_memory",
                "redis_host": "127.0.0.1",
                "redis_port": self.ports["redis"],
                "redis_db": 0,
                "qdrant_url": f"http://127.0.0.1:{self.ports['qdrant']}",
                "qdrant_collection": "brain_memories_it",
                "embed_base_url": self.embed_url,
                "embed_model": "mock-embed",
                "embed_dim": 8,
                "vector_enabled": 1,
                "graph_enabled": 0,
                "maintenance_enabled": 0,
                "distill_enabled": 0,
                "auto_migrate": 0,
                "machine": "it-srv",
            }
            (home / "server.json").write_text(json.dumps(cfg), encoding="utf-8")
            (home / ".env").write_text(
                "ATBRAINS_MARIADB_PASSWORD=itpass\n"
                "ATBRAINS_REDIS_PASSWORD=itpass\n"
                "ATBRAINS_QDRANT_API_KEY=\n"
                "ATBRAINS_EMBED_API_KEY=mock-key\n",
                encoding="utf-8",
            )

            # 1. 显式执行 migrate（从零库迁到最新 SCHEMA_VERSION）
            from atbrains_server.config import resolve_config
            st = AtbrainsStore(resolve_config(str(home)), start_workers=False)
            ver = st.migrate()
            self.assertEqual(ver, SCHEMA_VERSION)
            doc = st.doctor()
            self.assertTrue(doc["ok"], doc)
            self.assertIn("brain_author_defaults", doc["schema"]["tables"])
            st.close()

            # 2. 启动真实 HTTP 服务端 (/v1 + /mcp + console)
            port = _free_port()
            httpd, runtime = create_http_server(
                "127.0.0.1", port, config_home=str(home), data_dir=home, start_workers=False, access_log="-"
            )
            th = threading.Thread(target=httpd.serve_forever, daemon=True)
            th.start()
            try:
                admin_tok = runtime.service.tokens.load()["server"]
                hermes_tok = runtime.service.tokens.add("workstation|hermes")
                astrbot_tok = runtime.service.tokens.add("bot-host|astrbot")
                codex_tok = runtime.service.tokens.add("devbox|codex")

                admin_cli = AtbrainsClient(f"http://127.0.0.1:{port}", admin_tok)
                hermes_cli = AtbrainsClient(f"http://127.0.0.1:{port}", hermes_tok, spool_dir=str(home / "sp1"))
                astrbot_cli = AtbrainsClient(f"http://127.0.0.1:{port}", astrbot_tok, spool_dir=str(home / "sp2"))
                codex_cli = AtbrainsClient(f"http://127.0.0.1:{port}", codex_tok)

                # health
                h = codex_cli.health()
                self.assertEqual(h["version"], PROTOCOL_VERSION)

                # 多 agent 写入共享记忆 + 去重
                r1 = hermes_cli.remember("生产服务器 SSH 端口已改成 7622", kind="fact", tags="ssh,server", importance=9)
                self.assertGreater(r1["saved"]["id"], 0)
                r1_dup = astrbot_cli.remember("生产服务器 SSH 端口已改成 7622", kind="fact", importance=9)
                self.assertTrue(r1_dup["saved"]["deduped"])
                self.assertEqual(r1_dup["saved"]["id"], r1["saved"]["id"])

                r2 = codex_cli.remember("长期数据库后端是 MariaDB 11", kind="fact", tags="db", importance=8)
                self.assertGreater(r2["saved"]["id"], 0)

                # 触发管理员向量回填 → 真写入 Qdrant
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/v1/admin/drain",
                    data=b"{}",
                    headers={"Authorization": f"Bearer {admin_tok}", "Content-Type": "application/json"},
                    method="POST",
                )
                with opener.open(req, timeout=15) as resp:
                    drain_res = json.loads(resp.read().decode())
                self.assertEqual(drain_res["embedded"], 2, drain_res)

                # 跨 agent 语义+关键词混合召回（astrbot 搜 hermes 写入的记忆）
                rec = astrbot_cli.recall("ssh 端口是多少", include_claims=True)
                self.assertGreaterEqual(rec["count"], 1)
                self.assertIn("7622", rec["text"])
                self.assertTrue(any(it.get("via") in ("vec", "both") for it in rec["items"]))

                # 对话归档 + 游标幂等（全量转录 + 重复提交增量为 0）
                msgs = [
                    {"role": "user", "content": "查一下端口"},
                    {"role": "assistant", "content": "", "tool_calls": [
                        {"id": "c1", "type": "function", "function": {"name": "check_port", "arguments": "{}"}}]},
                    {"role": "tool", "tool_name": "check_port", "tool_call_id": "c1", "content": "7622"},
                    {"role": "assistant", "content": "端口是 7622"},
                ]
                t1 = codex_cli.turn("sess-it-1", "查一下端口", "端口是 7622", platform="codex", messages=msgs, total=4)
                self.assertGreater(t1["id"], 0)
                self.assertEqual(t1["archived"], 4)
                t2 = codex_cli.turn("sess-it-1", "查一下端口", "端口是 7622", platform="codex", messages=msgs, total=4)
                self.assertNotEqual(t2["id"], t1["id"])
                self.assertEqual(t2["archived"], 4)

                det = codex_cli.turn_detail(t1["id"])
                self.assertTrue(det["found"])
                self.assertEqual(det["stored"], 4)

                # 主张版本化 + 情节头 + 醒来包 + handoff（shared=True 跨机器/跨 agent 共用同一槽位）
                c1 = hermes_cli.claim_remember("server", "ssh_port", "22", turn_id=t1["id"], seq=2, quote="22", shared=True)
                c2 = codex_cli.claim_remember("server", "ssh_port", "7622", turn_id=t1["id"], seq=2, quote="7622", shared=True)
                self.assertEqual(c1["claim"]["claim_id"], c2["claim"]["claim_id"])
                self.assertEqual(c2["claim"]["revision"], 2)
                hist = astrbot_cli.claim_recall(subject="server", history=True)
                self.assertEqual(hist["count"], 2)

                codex_cli.episode_set(t1["id"], goal="验证通用架构", next_step="合并分支", open_items=["更新 README"])
                ep = hermes_cli.episode_get(t1["id"])
                self.assertTrue(ep["found"])
                self.assertEqual(ep["next_step"], "合并分支")

                ho = codex_cli.handoff("完成端到端集成测试", next_step="发布 v3.0", turn_id=t1["id"])
                self.assertTrue(ho["ok"])
                ctx = astrbot_cli.context(focus="端口", fmt="markdown")
                self.assertTrue(ctx["ok"])
                self.assertIn("醒来包", ctx["markdown"])

                # 单引擎共享状态：hermes 写入会直接把唯一 engine._vec_dirty 标脏
                self.assertTrue(getattr(runtime.service.engine, "_vec_dirty", False))

                # 跨 agent 隔离：astrbot 不能读取 codex 的敏感转录，也不能停用 codex 的记忆
                from atbrains_client import AtbrainsClientError
                with self.assertRaises(AtbrainsClientError) as ctx_err:
                    astrbot_cli.turn_detail(t1["id"])
                self.assertEqual(ctx_err.exception.status, 403)
                with self.assertRaises(AtbrainsClientError) as ctx_fg:
                    astrbot_cli.forget(r2["saved"]["id"], allow_spool=False)
                self.assertEqual(ctx_fg.exception.status, 403)

                # 写入后查询缓存 (qcache) 立即通过 qver 失效，跨端立刻能召回到新内容
                before = astrbot_cli.recall("缓存即时失效测试词9981", use_cache=False)
                self.assertNotIn("9981", before["text"])
                hermes_cli.remember("缓存即时失效测试词9981已写入", kind="fact", importance=8)
                hit_after = astrbot_cli.recall("缓存即时失效测试词9981", use_cache=False)
                self.assertIn("9981", hit_after["text"])

                # overview 走 vector_status 计数而不做全量 scroll
                req_ov = urllib.request.Request(
                    f"http://127.0.0.1:{port}/v1/admin/overview",
                    headers={"Authorization": f"Bearer {admin_tok}"},
                    method="GET",
                )
                with opener.open(req_ov, timeout=10) as resp:
                    ov = json.loads(resp.read().decode())
                self.assertTrue(ov["ok"])
                self.assertGreaterEqual(ov["qdrant"]["coverage"]["active_memories"], 3)

                # 停用 + 恢复（admin 权限放行）
                mid = r2["saved"]["id"]
                fg = admin_cli.forget(mid)
                self.assertEqual(fg["result"]["deactivated"], 1)
                rs = admin_cli.restore(mid)
                self.assertEqual(rs["result"]["restored"], 1)
            finally:
                for cli in (admin_cli, hermes_cli, astrbot_cli, codex_cli):
                    cli.close()
                httpd.shutdown()
                httpd.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
