"""atbrains-memory — AstrBot 通用记忆客户端插件（纯 HTTP `/v1`，零数据库依赖）。

把 AstrBot 接入 atbrains 共享记忆服务端（`atbrains-server`）：
  1) 自动召回：每轮 LLM 请求前调用 `POST /v1/memory/recall`（语义向量 + 关键词 + 图谱 RRF），
     在 `recall_timeout_ms` 预算内注入 system prompt；
  2) 记忆工具：`memory_save` / `memory_search` / `memory_forget` 函数工具；
  3) 自动归档：每轮问答结束后异步调用 `POST /v1/memory/turn`，服务端按游标幂等归档；
     若服务端暂时不可达，写入进入本地有界 JSONL 离线队列，恢复后自动重放。

本插件不直连 MariaDB / Redis / Qdrant，不携带任何数据库密码或第三方 `vendor/`。
配置来源（优先级从高到低）：
  1. AstrBot 插件配置 / 环境变量 `ATBRAINS_URL` + `ATBRAINS_TOKEN`
  2. `~/.config/atbrains/client.json` + `.env`
  3. 兼容旧配置：`$ATBRAINS_HERMES_HOME`（默认 `~/.hermes`）下的 `atbrains.json` + `.env`
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any, Optional


def _ensure_sdk() -> None:
    try:
        import atbrains_client  # noqa: F401
        return
    except ImportError:
        pass
    here = Path(__file__).resolve().parent
    for cand in (here / "_sdk", here.parents[1] / "sdk" / "python"):
        if (cand / "atbrains_client" / "__init__.py").is_file() and str(cand) not in sys.path:
            sys.path.insert(0, str(cand))
            return


_ensure_sdk()

from atbrains_client import (  # noqa: E402
    AtbrainsClient,
    AtbrainsClientError,
    resolve_client_config,
    save_client_token,
)

try:
    from astrbot.api import logger  # noqa: E402
    from astrbot.api.event import AstrMessageEvent, filter  # noqa: E402
    from astrbot.api.provider import ProviderRequest  # noqa: E402
    from astrbot.api.star import Context, Star  # noqa: E402
except ImportError:
    class _FallbackLogger:
        def _emit(self, msg: Any) -> None:
            text = str(msg)
            print(text if text.startswith("[atbrains]") else f"[atbrains] {text}", flush=True)

        debug = info = warning = error = _emit

    class _FallbackFilter:
        @staticmethod
        def _identity(*args: Any, **kwargs: Any) -> Any:
            return lambda fn: fn

        on_llm_request = _identity
        on_agent_done = _identity
        llm_tool = _identity

    class _Placeholder:
        pass

    class _FallbackStar:
        def __init__(self, context: Any = None, config: Optional[dict[str, Any]] = None) -> None:
            self.context = context
            self.config = config or {}

    logger = _FallbackLogger()
    filter = _FallbackFilter()
    AstrMessageEvent = ProviderRequest = Context = _Placeholder
    Star = _FallbackStar

AGENT_NAME = "astrbot"
_BG_TASKS: "set[asyncio.Task]" = set()


def _normalize_platform(name: str) -> str:
    n = (name or "").strip().lower()
    if n == "aiocqhttp":
        return "qq"
    return n or "unknown"


def _build_client(plugin_config: Optional[dict[str, Any]] = None) -> AtbrainsClient:
    pc = plugin_config if isinstance(plugin_config, dict) else {}
    config_home = os.environ.get("ATBRAINS_HERMES_HOME") or None
    cfg = resolve_client_config(
        config_home,
        base_url=str(pc.get("base_url") or pc.get("service_url") or "") or None,
        token=str(pc.get("token") or pc.get("service_token") or "") or None,
        client_name=AGENT_NAME,
        timeout_ms=int(pc["timeout_ms"]) if pc.get("timeout_ms") else None,
        write_timeout_ms=int(pc["write_timeout_ms"]) if pc.get("write_timeout_ms") else None,
        spool_dir=str(pc.get("spool_dir") or "") or None,
    )
    if not cfg.spool_dir:
        cfg.spool_dir = str(Path(__file__).resolve().parent / "data" / "spool")
    return AtbrainsClient.from_config(cfg)


class AtbrainsMemory(Star):
    """atbrains 通用长期记忆 AstrBot 适配器（纯 HTTP `/v1`）。"""

    def __init__(self, context: Context, config: Optional[dict[str, Any]] = None) -> None:
        super().__init__(context)
        self.plugin_cfg = config if isinstance(config, dict) else {}
        self.client = _build_client(self.plugin_cfg)
        self.recall_timeout_ms = int(self.plugin_cfg.get("recall_timeout_ms") or self.client.timeout_ms or 1600)
        self.archive_async = bool(self.plugin_cfg.get("archive_async", True))
        self._warned_recall = False
        self._warned_write = False
        if self.client.configured:
            logger.info(f"[atbrains] HTTP 客户端已就绪 → {self.client.base_url} (agent={AGENT_NAME})")
            threading.Thread(target=self._startup_checkin, name="atbrains-astrbot-checkin", daemon=True).start()
        else:
            logger.warning("[atbrains] 未配置 service_url 或 token，记忆功能处于待机状态")

    def _startup_checkin(self) -> None:
        try:
            self.client.status(light=True)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[atbrains] 启动在线上报跳过：{exc}")

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        if not self.client.configured:
            return
        try:
            if event.get_extra("atbrains_injected"):
                return
            event.set_extra("atbrains_injected", True)
            query = (event.message_str or "").strip()
            if len(query) < 2:
                return
            if not self.client.protocol_ok():
                return
            sid = str(event.unified_msg_origin or "")
            if self.recall_timeout_ms > 0:
                try:
                    block, count = await asyncio.wait_for(
                        asyncio.to_thread(self.client.prefetch, query, session_id=sid),
                        timeout=self.recall_timeout_ms / 1000.0,
                    )
                except asyncio.TimeoutError:
                    logger.warning(f"[atbrains] 召回超过 {self.recall_timeout_ms} ms 预算，跳过注入")
                    return
            else:
                block, count = await asyncio.to_thread(self.client.prefetch, query, session_id=sid)
            self._warned_recall = False
            if not block:
                return
            header = (
                "\n\n[atbrains 长期记忆 · 自动召回：以下内容提取自共享记忆库，"
                "回答时可自然参考，不必逐字复述，也不要主动声称“我查了数据库”]\n"
            )
            req.system_prompt = f"{req.system_prompt or ''}{header}{block}"
            logger.info(f"[atbrains] 已注入召回 {count} 条（{sid}）")
        except Exception as exc:  # noqa: BLE001
            if not self._warned_recall:
                self._warned_recall = True
                logger.warning(f"[atbrains] 召回注入失败（已跳过）：{exc}")

    @filter.on_agent_done()
    async def on_agent_done(self, event: AstrMessageEvent, run_context, response) -> None:
        if not self.client.configured:
            return
        try:
            user_text = (event.message_str or "").strip()
            reply_text = str(getattr(response, "completion_text", "") or "").strip()
            if not user_text and not reply_text:
                return
            platform = _normalize_platform(event.get_platform_name())
            author = (event.get_sender_name() or event.get_sender_id() or "")[:64]
            session_id = str(event.unified_msg_origin or "")
            if self.archive_async:
                task = asyncio.create_task(
                    self._archive_bg(user_text, reply_text, session_id, platform, author)
                )
                _BG_TASKS.add(task)
                task.add_done_callback(_BG_TASKS.discard)
                return
            res = await asyncio.to_thread(
                self.client.turn, session_id, user_text, reply_text, platform=platform, author=author
            )
            self._log_archive_res(res, platform, session_id)
        except Exception as exc:  # noqa: BLE001
            if not self._warned_write:
                self._warned_write = True
                logger.warning(f"[atbrains] 回合归档失败（已跳过）：{exc}")

    def _log_archive_res(self, res: dict[str, Any], platform: str, session_id: str) -> None:
        self._warned_write = False
        if res.get("queued"):
            logger.info(f"[atbrains] 服务端不可达，回合已入离线队列（待重放 {res.get('spool_pending')}）")
        else:
            logger.info(f"[atbrains] 已归档回合 #{res.get('id')}（{platform} · {session_id}）")

    async def _archive_bg(self, user_text: str, reply_text: str, session_id: str,
                          platform: str, author: str) -> None:
        try:
            res = await asyncio.to_thread(
                self.client.turn, session_id, user_text, reply_text, platform=platform, author=author
            )
            self._log_archive_res(res, platform, session_id)
        except Exception as exc:  # noqa: BLE001
            if not self._warned_write:
                self._warned_write = True
                logger.warning(f"[atbrains] 回合归档失败（已跳过）：{exc}")

    @filter.llm_tool(name="memory_save")
    async def memory_save(self, event: AstrMessageEvent, content: str,
                          kind: str = "note", tags: str = "", importance: int = 5) -> str:
        """保存一条长期记忆到 atbrains 共享记忆库（跨会话、跨 Agent 共享）。

        Args:
            content(string): 记忆内容，一句自足的话，脱离当前对话也能读懂
            kind(string): 类别，可选 fact / preference / decision / note / todo，默认 note
            tags(string): 逗号分隔的标签，可为空
            importance(number): 重要度 1-10，默认 5
        """
        if not self.client.configured:
            return json.dumps({"ok": False, "error": "atbrains 客户端未配置"}, ensure_ascii=False)
        try:
            res = await asyncio.to_thread(
                self.client.remember,
                content,
                kind=kind,
                tags=tags,
                importance=int(importance),
                source=AGENT_NAME,
                session_id=str(event.unified_msg_origin or ""),
            )
            if res.get("queued"):
                return json.dumps({"ok": True, "queued": True, "note": "服务端暂不可达，已暂存本地队列"}, ensure_ascii=False)
            saved = res.get("saved") or {}
            note = "（该内容已存在，已刷新）" if saved.get("deduped") else ""
            return json.dumps(
                {"ok": True, "id": saved.get("id"), "kind": saved.get("kind", kind),
                 "importance": saved.get("importance", importance), "note": note},
                ensure_ascii=False,
            )
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"ok": False, "error": str(exc)[:200]}, ensure_ascii=False)

    @filter.llm_tool(name="memory_search")
    async def memory_search(self, event: AstrMessageEvent, query: str, limit: int = 5) -> str:
        """检索 atbrains 共享长期记忆与历史对话片段。

        Args:
            query(string): 检索关键词或问题
            limit(number): 返回条数，默认 5
        """
        if not self.client.configured:
            return "记忆检索暂不可用（客户端未配置）。"
        try:
            res = await asyncio.to_thread(
                self.client.recall,
                query,
                session_id=str(event.unified_msg_origin or ""),
                limit=int(limit),
            )
            text = str(res.get("text") or "").strip()
            count = int(res.get("count") or 0)
            if not text:
                return "未找到相关记忆。"
            return f"{text}\n共 {count} 条" if count else text
        except Exception as exc:  # noqa: BLE001
            return f"检索失败：{exc}"

    @filter.llm_tool(name="memory_forget")
    async def memory_forget(self, event: AstrMessageEvent, memory_id: int) -> str:
        """停用一条不再有效的记忆（按 id）。

        Args:
            memory_id(number): 要停用的记忆 ID
        """
        if not self.client.configured:
            return json.dumps({"ok": False, "error": "atbrains 客户端未配置"}, ensure_ascii=False)
        try:
            res = await asyncio.to_thread(self.client.forget, int(memory_id))
            return json.dumps(res, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"ok": False, "error": str(exc)[:200]}, ensure_ascii=False)


def _selftest() -> int:
    cli = _build_client()
    print(f"configured: {cli.configured} url={cli.base_url or '<unset>'}")
    if not cli.configured:
        print("远程探测：跳过（未配置 base_url / token）")
        return 0
    h = cli.health()
    print(f"health: ok={h.get('ok')} version={h.get('version')}")
    st = cli.status(light=True)
    print(f"identity: machine={st.get('machine')} agent={st.get('agent')}")
    rec = cli.recall("atbrains 记忆库", limit=3)
    print(f"recall: count={rec.get('count')}")
    return 0


def _cli_tokset(argv: list[str]) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="astrbot-atbrains")
    p.add_argument("--tokset", required=True, help="Bearer 令牌")
    p.add_argument("--url", default="", help="可选：服务端地址")
    p.add_argument("--home", default=None, help="可选：配置目录（默认 ~/.config/atbrains）")
    p.add_argument("--no-verify", action="store_true")
    args = p.parse_args(argv)
    saved = save_client_token(
        args.tokset,
        config_home=args.home,
        base_url=args.url or None,
        json_filename="client.json",
        env_key="ATBRAINS_TOKEN",
    )
    print(f"✅ 已保存 Token ({saved['masked']}) → {saved['env_path']} (权限 600)")
    if saved.get("base_url"):
        print(f"✅ 已保存服务地址 {saved['base_url']} → {saved['json_path']}")
    if args.no_verify:
        return 0
    cfg = resolve_client_config(args.home, client_name=AGENT_NAME)
    if not cfg.base_url:
        return 0
    try:
        info = AtbrainsClient(base_url=cfg.base_url, token=args.tokset, client_name=AGENT_NAME).verify()
    except AtbrainsClientError as exc:
        print(f"❌ 令牌校验失败：{exc}", file=sys.stderr)
        return 1
    print(f"✅ 校验通过 ({info['latency_ms']}ms) · 身份 {info.get('machine') or '-'} | {info.get('agent') or '-'} · 已上报在线")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    if "--tokset" in sys.argv:
        raise SystemExit(_cli_tokset(sys.argv[1:]))
