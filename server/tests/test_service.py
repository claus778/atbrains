"""atbrains-server HTTP /v1、/mcp、/api、鉴权角色与契约一致性测试（不连外部 DB）。"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
SDK_DIR = REPO / "sdk" / "python"
for p in (str(ROOT), str(SDK_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from atbrains_client.schemas import TOOLS as SDK_TOOLS  # noqa: E402
from atbrains_server.context import RequestContext, owns_record  # noqa: E402
from atbrains_server.http import ServerRuntime, make_handler  # noqa: E402
from atbrains_server.mcp import AtbrainsMCP  # noqa: E402
from atbrains_server.service import MemoryService  # noqa: E402
from atbrains_server.tokens import TokenStore  # noqa: E402
from atbrains_server.tools_registry import TOOLS as SERVER_TOOLS  # noqa: E402
from atbrains_server.util import BrainForbidden  # noqa: E402


class FakeStore:
    """内存模拟存储引擎，用于验证 service / mcp / http 路由与鉴权语义。"""

    def __init__(self, cfg: dict) -> None:
        self.cfg = dict(cfg)
        self.machine = str(cfg.get("machine") or "srv")
        self.agent = str(cfg.get("agent") or "default")
        self.memories: list[dict] = []
        self.turns: list[dict] = []
        self.claims: list[dict] = []
        self.episodes: dict[int, dict] = {}
        self.archives: list[dict] = []

    def ping(self) -> dict:
        return {"redis": True, "mariadb": True}

    def stats(self, *, force: bool = False) -> dict:
        return {
            "mysql": {
                "memories_total": len(self.memories),
                "memories_active": sum(1 for m in self.memories if m.get("active", 1)),
                "turns": len(self.turns),
                "claims_active": len(self.claims),
                "episodes": len(self.episodes),
                "sessions": 1,
                "size_bytes": 4096,
            },
            "redis": {"keys": 3, "recent_stream": len(self.turns), "hot_index": len(self.memories)},
        }

    def remember(self, content: str, *, kind: str = "note", tags: str = "",
                 importance: int = 5, source: str = "tool",
                 session_id: str = "", memory_type: str = "",
                 ctx: RequestContext | None = None) -> dict:
        mid = len(self.memories) + 1
        rec = {
            "id": mid, "kind": kind, "memory_type": memory_type or "episodic",
            "content": content, "tags": tags, "importance": importance,
            "source": source,
            "machine": (ctx.machine if ctx and ctx.machine else self.machine),
            "agent": (ctx.agent if ctx else self.agent),
            "active": 1,
        }
        self.memories.append(rec)
        return {"id": mid, "kind": kind, "importance": importance,
                "memory_type": rec["memory_type"], "hot_cache": True, "deduped": False}

    def forget(self, mem_id: int, *, ctx: RequestContext | None = None) -> dict:
        for m in self.memories:
            if m["id"] == mem_id:
                if ctx is not None and not owns_record(ctx, m):
                    raise BrainForbidden("无权停用其他身份的记忆")
                m["active"] = 0
                return {"id": mem_id, "deactivated": 1}
        return {"id": mem_id, "deactivated": 0}

    def restore(self, mem_id: int, *, ctx: RequestContext | None = None) -> dict:
        for m in self.memories:
            if m["id"] == mem_id:
                if ctx is not None and not owns_record(ctx, m):
                    raise BrainForbidden("无权恢复其他身份的记忆")
                m["active"] = 1
                return {"id": mem_id, "restored": 1}
        return {"id": mem_id, "restored": 0}

    def deactivate_by_content(self, content: str, *, ctx: RequestContext | None = None) -> int:
        n = 0
        for m in self.memories:
            if m["content"] == content and m["active"] == 1 and owns_record(ctx, m):
                m["active"] = 0
                n += 1
        return n

    def search_memories_hybrid(self, query: str, *, limit: int = 5, kind=None, agent=None) -> list[dict]:
        return [m for m in self.memories if m.get("active", 1)][:limit]

    def recall_memories(self, query: str, *, limit: int = 5, kind=None, agent=None) -> list[dict]:
        return self.search_memories_hybrid(query, limit=limit, kind=kind, agent=agent)

    def top_memories(self, limit: int = 8, *, kind=None, agent=None) -> list[dict]:
        return [m for m in self.memories if m.get("active", 1)][:limit]

    def search_turns(self, query: str, *, limit: int = 3, ctx: RequestContext | None = None) -> list[dict]:
        return [t for t in self.turns if owns_record(ctx, t)][:limit]

    def prefetch_block(self, query: str, *, session_id: str = "", with_hits: bool = False,
                       with_turns: bool = False, ctx: RequestContext | None = None, **kw):
        hits = [{"id": m["id"], "kind": m["kind"], "preview": m["content"]} for m in self.memories[:3]]
        txt = "[mem]\n" + "\n".join(h["preview"] for h in hits) if hits else ""
        if with_turns:
            return txt, len(hits), hits, self.search_turns(query, limit=3, ctx=ctx)
        if with_hits:
            return txt, len(hits), hits
        return txt, len(hits)

    def format_hits(self, mems: list[dict], limit: int = 20) -> list[dict]:
        return mems[:limit]

    def hot_recent(self, limit: int = 5) -> list[dict]:
        return self.memories[-limit:]

    def recent_turns(self, limit: int = 10, *, ctx: RequestContext | None = None) -> list[dict]:
        return [t for t in self.turns if owns_record(ctx, t)][-limit:]

    def record_turn(self, session_id: str, user_content: str, assistant_content: str,
                    *, ctx: RequestContext | None = None, **kw) -> int:
        tid = len(self.turns) + 1
        self.turns.append({
            "id": tid, "session_id": session_id, "user": user_content,
            "assistant": assistant_content,
            "machine": (ctx.machine if ctx and ctx.machine else self.machine),
            "agent": (ctx.agent if ctx else self.agent),
        })
        return tid

    def get_session_cursor(self, session_id: str) -> int:
        return len(self.turns)

    def turn_detail(self, turn_id: int = 0, *, session_id: str = "", max_chars: int = 1500,
                    ctx: RequestContext | None = None) -> dict:
        tid = turn_id or 1
        for t in self.turns:
            if t["id"] == tid and not owns_record(ctx, t):
                raise BrainForbidden("无权读取其他身份的对话转录")
        return {"found": True, "turn": {"id": tid, "session_id": session_id}, "messages": [], "stored": 0}

    def archive(self, session_id: str, kind: str, payload: str, *, platform: str = "",
                ctx: RequestContext | None = None) -> int:
        self.archives.append({"session_id": session_id, "kind": kind, "payload": payload})
        return len(self.archives)

    def touch_session(self, session_id: str, platform: str = "", ctx: RequestContext | None = None) -> None:
        pass

    def claim_remember(self, subject: str, predicate: str, value: str,
                       *, ctx: RequestContext | None = None, **kw) -> dict:
        self.claims.append({
            "claim_id": len(self.claims) + 1, "subject": subject,
            "predicate": predicate, "value_text": value, "revision": 1,
            "machine": (ctx.machine if ctx and ctx.machine else self.machine),
            "agent": (ctx.agent if ctx else self.agent),
        })
        return self.claims[-1]

    def claim_recall(self, **kw) -> list[dict]:
        return list(self.claims)

    def claim_retire(self, claim_id: int, reason: str = "", ctx: RequestContext | None = None) -> dict:
        for c in self.claims:
            if c["claim_id"] == claim_id and not owns_record(ctx, c, allow_shared=True):
                raise BrainForbidden("无权撤回其他身份的主张")
        return {"claim_id": claim_id, "retracted": 1, "reason": reason}

    def episode_set(self, turn_id: int, *, ctx: RequestContext | None = None, **kw) -> dict:
        self.episodes[turn_id] = {"turn_id": turn_id, **{k: v for k, v in kw.items() if v is not None}}
        return {"turn_id": turn_id, "updated": True}

    def episode_get(self, turn_id: int = 0, *, session_id: str = "",
                    ctx: RequestContext | None = None) -> dict:
        ep = self.episodes.get(turn_id)
        return {"found": bool(ep), **(ep or {})}

    def open_episodes(self, limit: int = 5, *, ctx: RequestContext | None = None) -> list[dict]:
        return list(self.episodes.values())[:limit]

    def overview(self, *, fresh: bool = False) -> dict:
        return {"ts": "2026-01-01 00:00:00", "version": "2.1.0", "stats": self.stats(), "ping": self.ping()}

    def recent(self, limit: int = 20, *, ctx: RequestContext | None = None) -> dict:
        return {"hot": self.hot_recent(limit), "turns": self.recent_turns(limit, ctx=ctx)}


class ParityTest(unittest.TestCase):
    def test_openapi_covers_all_service_routes(self) -> None:
        text = (REPO / "contracts" / "openapi.yaml").read_text(encoding="utf-8")
        # 抓取 paths: 下所有缩进 2 格的路径键
        spec_paths = set(re.findall(r"^  (/v1/[^\s:]+):", text, flags=re.M))
        route_paths = set(MemoryService._ROUTES.keys())
        self.assertEqual(spec_paths, route_paths)

    def test_tool_schemas_parity(self) -> None:
        self.assertEqual(SERVER_TOOLS, SDK_TOOLS)


class TokenStoreTest(unittest.TestCase):
    def test_legacy_and_structured_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "tokens.json"
            ts = TokenStore(p)
            srv_tok = ts.load()["server"]
            codex_tok = ts.add("laptop|codex")
            admin_tok = ts.add("ops-bot", role="admin", machine="ops", agent="cli")

            ident_srv = ts.authorize({"Authorization": f"Bearer {srv_tok}"}, channel="v1")
            self.assertIsNotNone(ident_srv)
            assert ident_srv
            self.assertEqual((ident_srv.machine, ident_srv.agent, ident_srv.role, ident_srv.client), ("server", "admin", "admin", "admin"))
            srv_info = ts.resolve_entry("server", ts.entries()["server"])
            self.assertTrue(srv_info["builtin"])
            self.assertTrue(srv_info["protected"])
            # 系统内置 server 令牌禁止删除、重命名、降权或关闭防误删保护
            with self.assertRaises(ValueError):
                ts.remove("server")
            with self.assertRaises(ValueError):
                ts.update("server", new_name="other-server")
            with self.assertRaises(ValueError):
                ts.update("server", role="agent")
            with self.assertRaises(ValueError):
                ts.update("server", protected=False)
            # 但允许更新内置 server 令牌的备注与自定义属性
            srv_upd = ts.update("server", note="核心管理通道", attrs={"env": "prod"})
            self.assertEqual(srv_upd["note"], "核心管理通道")
            self.assertEqual(srv_upd["attrs"], {"env": "prod"})
            self.assertTrue(srv_upd["protected"])

            ident_cdx = ts.authorize({"Authorization": f"Bearer {codex_tok}"}, channel="v1")
            assert ident_cdx
            self.assertEqual((ident_cdx.machine, ident_cdx.agent, ident_cdx.role), ("laptop", "codex", "agent"))

            ident_mcp = ts.authorize({"Authorization": f"Bearer {codex_tok}"}, channel="mcp")
            assert ident_mcp
            self.assertEqual((ident_mcp.machine, ident_mcp.agent), ("laptop", "codex"))

            ident_adm = ts.authorize({"Authorization": f"Bearer {admin_tok}"}, channel="v1")
            assert ident_adm
            self.assertEqual((ident_adm.machine, ident_adm.agent, ident_adm.role), ("ops", "cli", "admin"))

            hermes_tok = ts.add("x260|hermes", machine="x260", agent="hermes", client="hermes", note="主力机", attrs={"env": "prod"})
            ident_h = ts.authorize({"Authorization": f"Bearer {hermes_tok}"}, channel="v1")
            assert ident_h
            self.assertEqual((ident_h.machine, ident_h.agent, ident_h.client), ("x260", "hermes", "hermes"))
            self.assertEqual(ident_cdx.client, "codex")
            # 令牌解析容错：多空格、引号、X-Memory-Token 均能通过 O(1)+恒定时间校验
            ident_q = ts.authorize({"Authorization": f'  Bearer   "{hermes_tok}"  '}, channel="v1")
            assert ident_q
            self.assertEqual(ident_q.name, "x260|hermes")
            ident_hdr = ts.authorize({"X-Memory-Token": hermes_tok}, channel="v1")
            assert ident_hdr
            self.assertEqual(ident_hdr.name, "x260|hermes")
            self.assertIsNone(ts.validate_token("invalid-token"))

            # 编辑客户端属性：保留原 token 不变，支持修改 client/machine/agent/role/note/attrs 与重命名
            updated = ts.update(
                "x260|hermes",
                new_name="studio|cursor-pro",
                machine="studio",
                agent="cursor-pro",
                client="cursor",
                role="admin",
                note="工作室主力",
                attrs={"env": "dev", "model": "claude-4"},
            )
            self.assertEqual(updated["name"], "studio|cursor-pro")
            self.assertEqual(updated["machine"], "studio")
            self.assertEqual(updated["agent"], "cursor-pro")
            self.assertEqual(updated["client"], "cursor")
            self.assertEqual(updated["role"], "admin")
            self.assertEqual(updated["note"], "工作室主力")
            self.assertEqual(updated["attrs"], {"env": "dev", "model": "claude-4"})
            # 旧名称已移除，但原 Bearer Token 依然有效并映射至新身份
            self.assertNotIn("x260|hermes", ts.entries())
            ident_upd = ts.authorize({"Authorization": f"Bearer {hermes_tok}"}, channel="v1")
            assert ident_upd
            self.assertEqual(
                (ident_upd.name, ident_upd.machine, ident_upd.agent, ident_upd.client, ident_upd.role),
                ("studio|cursor-pro", "studio", "cursor-pro", "cursor", "admin"),
            )
            # 重名冲突与不存在目标应报错
            with self.assertRaises(ValueError):
                ts.update("studio|cursor-pro", new_name="laptop|codex")
            with self.assertRaises(KeyError):
                ts.update("nonexistent|token", note="x")


class HttpAndMcpServiceTest(unittest.TestCase):
    def _req(self, port: int, method: str, path: str, body=None, headers=None) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        hdrs = dict(headers or {})
        if data is not None:
            hdrs.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers=hdrs, method=method)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")

    def test_full_v1_mcp_and_rbac(self) -> None:
        from http.server import ThreadingHTTPServer

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            svc = MemoryService(
                config_home=tmp,
                tokens_path=data_dir / "service-tokens.json",
                start_workers=False,
                store_factory=lambda cfg: FakeStore(cfg),
            )
            rt = ServerRuntime(
                config_home=tmp,
                data_dir=data_dir,
                start_workers=False,
                access_log="-",
                service=svc,
            )
            svc._console_token_fn = rt.console_token.get
            srv_tok = svc.tokens.load()["server"]
            agent_tok = svc.tokens.add("macbook|codex")
            console_tok = rt.console_token.get()

            httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(rt))
            th = threading.Thread(target=httpd.serve_forever, daemon=True)
            th.start()
            try:
                p = httpd.server_port
                # 1. health
                code, h = self._req(p, "GET", "/v1/health")
                self.assertEqual(code, 200)
                self.assertTrue(h["ok"])
                self.assertTrue(h["version"].startswith("2."))

                # 2. agent token 读写 /v1 全路径
                hdr_agent = {"Authorization": f"Bearer {agent_tok}"}
                code, st = self._req(p, "POST", "/v1/memory/status", {"light": True}, hdr_agent)
                self.assertEqual(code, 200)
                self.assertEqual((st["machine"], st["agent"], st["role"]), ("macbook", "codex", "agent"))

                code, rem = self._req(p, "POST", "/v1/memory/remember", {"content": "通用记忆库测试", "kind": "fact"}, hdr_agent)
                self.assertEqual(code, 200)
                self.assertEqual(rem["saved"]["id"], 1)

                code, rec = self._req(p, "POST", "/v1/memory/recall", {"query": "通用", "include_claims": True}, hdr_agent)
                self.assertEqual(code, 200)
                self.assertEqual(rec["count"], 1)

                code, cl = self._req(p, "POST", "/v1/claim/remember",
                                     {"subject": "项目", "predicate": "协议", "value": "v2.1"}, hdr_agent)
                self.assertEqual(code, 200)
                self.assertEqual(cl["claim"]["revision"], 1)

                code, ctx = self._req(p, "POST", "/v1/context", {"format": "markdown"}, hdr_agent)
                self.assertEqual(code, 200)
                self.assertIn("markdown", ctx)

                code, ho = self._req(p, "POST", "/v1/handoff", {"note": "交接给下一个 agent"}, hdr_agent)
                self.assertEqual(code, 200)

                # 3. 权限隔离：普通 agent token 调 /v1/admin/* 必须 403；admin/console 才允许
                code, _ = self._req(p, "GET", "/v1/admin/tokens", headers=hdr_agent)
                self.assertEqual(code, 403)
                code, toks = self._req(p, "GET", "/v1/admin/tokens", headers={"Authorization": f"Bearer {srv_tok}"})
                self.assertEqual(code, 200)
                self.assertGreaterEqual(len(toks["tokens"]), 2)
                code, _ = self._req(p, "GET", "/v1/admin/tokens", headers={"X-Auth-Token": console_tok})
                self.assertEqual(code, 200)
                # 控制台令牌不能冒充 Bearer 访问 /v1/memory/*，且 401 统一带 ok=False
                code, unauth = self._req(p, "POST", "/v1/memory/status", {}, {"Authorization": f"Bearer {console_tok}"})
                self.assertEqual(code, 401)
                self.assertIs(unauth.get("ok"), False)
                # 携带有效 X-Auth-Token 时，即使附带非 admin Authorization 也能通过管理鉴权
                code, _ = self._req(
                    p, "GET", "/v1/admin/tokens",
                    headers={"X-Auth-Token": console_tok, "Authorization": f"Bearer {agent_tok}"},
                )
                self.assertEqual(code, 200)

                # 4. /mcp JSON-RPC
                code, mcp_list = self._req(p, "POST", "/mcp",
                                           {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, hdr_agent)
                self.assertEqual(code, 200)
                self.assertEqual(len(mcp_list["result"]["tools"]), 12)
                code, mcp_ctx = self._req(
                    p, "POST", "/mcp",
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                     "params": {"name": "atbrains_context", "arguments": {"limit": 3}}},
                    hdr_agent,
                )
                self.assertEqual(code, 200)
                self.assertFalse(mcp_ctx["result"]["isError"])

                # 5. /mcp 保留 machine 身份 + 跨 agent 隔离与越权阻断 + HTTP 边界保护
                code, mcp_rem = self._req(
                    p, "POST", "/mcp",
                    {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                     "params": {"name": "atbrains_remember", "arguments": {"content": "from mcp", "kind": "fact"}}},
                    hdr_agent,
                )
                self.assertEqual(code, 200)
                self.assertEqual(svc.engine.memories[-1]["machine"], "macbook")

                other_tok = svc.tokens.add("other|astrbot")
                hdr_other = {"Authorization": f"Bearer {other_tok}"}
                # 非 admin agent 不能停用别人的记忆 (403)
                code, _ = self._req(p, "POST", "/v1/memory/forget", {"id": 1}, hdr_other)
                self.assertEqual(code, 403)
                # admin 可以停用 (200)
                code, _ = self._req(p, "POST", "/v1/memory/forget", {"id": 1}, {"Authorization": f"Bearer {srv_tok}"})
                self.assertEqual(code, 200)

                # 废弃 /api/* 路由已下线 + 禁止 ?token= URL 参数传口令
                code, _ = self._req(p, "GET", f"/api/overview?token={console_tok}")
                self.assertEqual(code, 404)
                code, _ = self._req(p, "GET", f"/v1/admin/overview?token={console_tok}")
                self.assertEqual(code, 401)
                code, ov = self._req(p, "GET", "/v1/admin/overview", headers={"X-Auth-Token": console_tok})
                self.assertEqual(code, 200)
                self.assertTrue(ov["ok"])

                # 按客户端签发独立 agent 令牌 + 实时活动追踪（区分 token / client / channel / IP）
                hdr_console = {"X-Auth-Token": console_tok}
                code, issued = self._req(
                    p, "POST", "/v1/admin/tokens",
                    {"machine": "x260", "agent": "hermes", "client": "hermes", "role": "agent", "note": "客厅开发机"},
                    hdr_console,
                )
                self.assertEqual(code, 200)
                self.assertEqual(issued["name"], "x260|hermes")
                self.assertEqual(issued["client"], "hermes")
                self.assertEqual(issued["note"], "客厅开发机")
                hdr_h = {
                    "Authorization": f"Bearer {issued['token']}",
                    "X-Real-IP": "10.0.0.42",
                    "User-Agent": "hermes-plugin/2.1",
                }
                code, _ = self._req(p, "POST", "/v1/memory/status", {"light": True}, hdr_h)
                self.assertEqual(code, 200)
                code, _ = self._req(
                    p, "POST", "/mcp",
                    {"jsonrpc": "2.0", "id": 9, "method": "tools/list"},
                    hdr_h,
                )
                self.assertEqual(code, 200)
                code, tlist = self._req(p, "GET", "/v1/admin/tokens", headers=hdr_console)
                self.assertEqual(code, 200)
                by_name = {t["name"]: t for t in tlist["tokens"]}
                rec_h = by_name["x260|hermes"]
                self.assertEqual(rec_h["client"], "hermes")
                self.assertEqual(rec_h["machine"], "x260")
                self.assertEqual(rec_h["agent"], "hermes")
                self.assertEqual(rec_h["note"], "客厅开发机")
                self.assertEqual(rec_h["last_channel"], "mcp")
                self.assertEqual(rec_h["last_ip"], "10.0.0.42")
                self.assertEqual(rec_h["requests"], 2)
                self.assertTrue(rec_h["online"])

                # PATCH /v1/admin/tokens 编辑客户端属性（不旋转原令牌，迁移活跃记录，立即生效）
                code, patched = self._req(
                    p, "PATCH", "/v1/admin/tokens",
                    {
                        "name": "x260|hermes",
                        "new_name": "desk|claude-code",
                        "machine": "desk",
                        "agent": "claude-code",
                        "client": "claude-code",
                        "role": "agent",
                        "note": "主力桌面端",
                        "attrs": {"env": "prod", "owner": "alice"},
                    },
                    hdr_console,
                )
                self.assertEqual(code, 200)
                self.assertEqual(patched["name"], "desk|claude-code")
                self.assertEqual(patched["client"], "claude-code")
                self.assertEqual(patched["note"], "主力桌面端")
                self.assertEqual(patched["attrs"], {"env": "prod", "owner": "alice"})
                self.assertNotIn("token", patched)
                # 原 Bearer Token 无需轮换即生效，且识别为新身份（/v1/memory/status 同时返回 name 与 client）
                code, st_after = self._req(p, "POST", "/v1/memory/status", {"light": True}, hdr_h)
                self.assertEqual(code, 200)
                self.assertEqual((st_after["machine"], st_after["agent"], st_after["client"], st_after["name"]),
                                 ("desk", "claude-code", "claude-code", "desk|claude-code"))
                code, tlist2 = self._req(p, "GET", "/v1/admin/tokens", headers=hdr_console)
                self.assertIn("server_ts", tlist2)
                self.assertGreaterEqual(int(tlist2.get("online_ttl_sec") or 0), 30)
                by_name2 = {t["name"]: t for t in tlist2["tokens"]}
                self.assertNotIn("x260|hermes", by_name2)
                rec_d = by_name2["desk|claude-code"]
                self.assertEqual(rec_d["client"], "claude-code")
                self.assertEqual(rec_d["note"], "主力桌面端")
                self.assertEqual(rec_d["attrs"], {"env": "prod", "owner": "alice"})
                self.assertEqual(rec_d["requests"], 3)
                self.assertEqual(rec_d["status"], "online")
                self.assertIsNotNone(rec_d["ago_sec"])
                # 重启恢复：新建 MemoryService 实例应从 token-activity.json 恢复在线状态与请求计数
                svc_restarted = MemoryService(
                    config_home=tmp,
                    tokens_path=data_dir / "service-tokens.json",
                    start_workers=False,
                    console_token_fn=rt.console_token.get,
                    store_factory=lambda cfg: FakeStore(cfg),
                )
                rc_code, rc_payload = svc_restarted.handle("GET", "/v1/admin/tokens", {}, {}, hdr_console)
                self.assertEqual(rc_code, 200)
                rec_after_restart = {t["name"]: t for t in rc_payload["tokens"]}["desk|claude-code"]
                self.assertTrue(rec_after_restart["online"])
                self.assertEqual(rec_after_restart["status"], "online")
                self.assertEqual(rec_after_restart["last_ip"], "10.0.0.42")
                self.assertGreaterEqual(rec_after_restart["requests"], 2)
                # 编辑不存在的令牌返回 404；非 admin 调用 PATCH 返回 403
                code, _ = self._req(p, "PATCH", "/v1/admin/tokens", {"name": "no-such-client", "note": "x"}, hdr_console)
                self.assertEqual(code, 404)
                code, _ = self._req(p, "PATCH", "/v1/admin/tokens", {"name": "desk|claude-code", "note": "x"}, hdr_agent)
                self.assertEqual(code, 403)
                # 系统内置 server 令牌：明确标记为 client=admin / builtin=True / protected=True，且禁止删除或降权
                rec_srv = by_name2["server"]
                self.assertEqual(rec_srv["client"], "admin")
                self.assertEqual(rec_srv["role"], "admin")
                self.assertTrue(rec_srv["builtin"])
                self.assertTrue(rec_srv["protected"])
                code, _ = self._req(p, "DELETE", "/v1/admin/tokens", {"name": "server"}, hdr_console)
                self.assertEqual(code, 400)
                code, _ = self._req(p, "PATCH", "/v1/admin/tokens", {"name": "server", "role": "agent"}, hdr_console)
                self.assertEqual(code, 400)
                code, _ = self._req(p, "POST", "/v1/admin/tokens", {"name": "server", "role": "agent"}, hdr_console)
                self.assertEqual(code, 400)
                # 开启防误删保护的客户端令牌也禁止删除，关闭保护后才可删除
                code, prot_on = self._req(p, "PATCH", "/v1/admin/tokens", {"name": "desk|claude-code", "protected": True}, hdr_console)
                self.assertEqual(code, 200)
                self.assertTrue(prot_on["protected"])
                code, _ = self._req(p, "DELETE", "/v1/admin/tokens", {"name": "desk|claude-code"}, hdr_console)
                self.assertEqual(code, 400)
                code, prot_off = self._req(p, "PATCH", "/v1/admin/tokens", {"name": "desk|claude-code", "protected": False}, hdr_console)
                self.assertEqual(code, 200)
                self.assertFalse(prot_off["protected"])
                code, rm = self._req(p, "DELETE", "/v1/admin/tokens", {"name": "desk|claude-code"}, hdr_console)
                self.assertEqual(code, 200)
                self.assertTrue(rm["removed"])

                # 畸形 JSON 必须报 400 invalid json，而不是静默变空对象
                req = urllib.request.Request(
                    f"http://127.0.0.1:{p}/v1/memory/remember",
                    data=b"{bad json",
                    headers={**hdr_agent, "Content-Type": "application/json"},
                    method="POST",
                )
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                try:
                    opener.open(req, timeout=5)
                    self.fail("expected 400")
                except urllib.error.HTTPError as exc:
                    self.assertEqual(exc.code, 400)
                    err_body = json.loads(exc.read().decode())
                    self.assertIn("invalid json", err_body["error"])
                # 超大 Content-Length 必须被 413 拒之门外
                rt.max_body_bytes = 64
                req_big = urllib.request.Request(
                    f"http://127.0.0.1:{p}/v1/memory/remember",
                    data=b'{"content": "' + b"x" * 128 + b'"}',
                    headers={**hdr_agent, "Content-Type": "application/json"},
                    method="POST",
                )
                try:
                    opener.open(req_big, timeout=5)
                    self.fail("expected 413")
                except urllib.error.HTTPError as exc:
                    self.assertEqual(exc.code, 413)
            finally:
                httpd.shutdown()
                httpd.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
