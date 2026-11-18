"""运维：元数据、维护巡检、统计、清理。"""
from __future__ import annotations

import logging
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Optional
from typing import cast
import threading
import time


from .. import PROTOCOL_VERSION, __version__
from ..context import RequestContext

logger = logging.getLogger("atbrains")

class MaintenanceMixin:
    """运维：元数据、维护巡检、统计、清理。"""

    def _meta_get(self, key: str) -> Optional[str]:
        """读 brain_meta 单键（缺失返回 None，视为“从未发生”）。"""
        self.ensure_schema()
        rows = self._exec(
            "SELECT meta_value FROM brain_meta WHERE meta_key = %s", (key,), fetch=True)
        return str(rows[0]["meta_value"]) if rows else None

    def _meta_set(self, key: str, value: str) -> None:
        self.ensure_schema()
        self._exec(
            "INSERT INTO brain_meta (meta_key, meta_value) VALUES (%s, %s) "
            "ON DUPLICATE KEY UPDATE meta_value = VALUES(meta_value)",
            (key, str(value)))

    def start_maintenance(self) -> None:
        """惰性启动维护线程（每进程一个；daemon，绝不阻塞进程退出）。"""
        if not int(self.cfg.get("maintenance_enabled") or 0):
            return
        with self._maint_lock:
            if self._maint_started:
                return
            self._maint_started = True
        try:
            threading.Thread(target=self._maintenance_loop,
                             name="brain-maintenance", daemon=True).start()
        except Exception as exc:  # noqa: BLE001
            with self._maint_lock:
                self._maint_started = False
            logger.warning("atbrains: 启动维护线程失败: %s", exc)

    def _maintenance_loop(self) -> None:
        interval = max(0.01, float(self.cfg.get("maintenance_interval_min") or 60)) * 60.0
        delay = min(interval, 60.0)   # 首轮稍后开始，不抢占启动路径
        while True:
            try:
                time.sleep(delay)
                self._maintenance_tick()
            except Exception as exc:  # noqa: BLE001
                # 线程绝不因单轮失败退出，更不向上抛。
                logger.warning("atbrains: 维护循环异常（继续下一轮）: %s", exc)
            delay = interval

    def _graph_pending(self) -> int:
        """返回「id 大于图谱游标」的活跃记忆数（增量抽取的待办量）。"""
        cur = self._meta_get("graph_cursor")
        if cur is None:
            return 0
        try:
            after = int(cur)
        except (TypeError, ValueError):
            return 0
        row = cast(list, self._exec(
            "SELECT COUNT(*) AS n FROM brain_memories WHERE active = 1 AND id > %s",
            (after,), fetch=True))
        return int((row[0]["n"] if row else 0) or 0)

    def _graph_autobuild(self) -> None:
        """增量图谱抽取：只补游标之后的新记忆，按间隔节流 + GET_LOCK 多进程互斥。

        首次运行只把游标对齐到当前最大 id（不回溯）——存量由人工 `graph-build` 显式补齐，
        否则升级后的第一个 tick 会把几百条老记忆全部重抽一遍，白烧 API。
        """
        if not int(self.cfg.get("graph_enabled") or 0):
            return
        if not int(self.cfg.get("graph_auto_enabled") or 0):
            return
        every = float(self.cfg.get("graph_auto_interval_min") or 0)
        if every <= 0:
            return
        last = self._meta_get("last_graph_build")
        if last:
            try:
                prev = datetime.strptime(last, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except ValueError:
                prev = None
            if prev is not None and (datetime.now(timezone.utc) - prev).total_seconds() < every * 60:
                return
        cur = self._meta_get("graph_cursor")
        if cur is None:
            row = cast(list, self._exec("SELECT MAX(id) AS mid FROM brain_memories", fetch=True))
            self._meta_set("graph_cursor", str(int((row[0]["mid"] if row else 0) or 0)))
            logger.info("atbrains: 图谱游标初始化（不回溯存量，存量用 graph-build 补齐）")
            return
        if self._graph_pending() <= 0:
            return
        got = cast(list, self._exec("SELECT GET_LOCK('brain_graph', 0) AS got", fetch=True))
        if not (got and got[0].get("got")):
            logger.debug("atbrains: 图谱增量抽取跳过（另一进程正在抽取）")
            return
        try:
            res = self.graph_extract(dry_run=False, limit=int(self.cfg.get("graph_auto_limit") or 12),
                                     after_id=int(cur))
            if int(res.get("max_id") or 0) > int(cur):
                self._meta_set("graph_cursor", str(int(res["max_id"])))
            self._meta_set("last_graph_build",
                           datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
            if res.get("memories"):
                logger.info("atbrains: 图谱增量抽取 memories=%s 实体=%s 关系=%s 提及=%s 失败=%s",
                            res.get("memories"), res.get("entities"), res.get("edges"),
                            res.get("mentions"), res.get("llm_failed"))
        finally:
            try:
                self._exec("SELECT RELEASE_LOCK('brain_graph')")
            except Exception:  # noqa: BLE001
                pass

    def _maintenance_tick(self) -> None:
        # 访问计数回写与 prune 解耦：即使维护清理没到周期（或未启用），也要把 Redis 计数落库。
        try:
            self._flush_access()
        except Exception as exc:  # noqa: BLE001
            logger.debug("atbrains: 访问计数回写异常（忽略）: %s", str(exc)[:160])
        # 图谱增量抽取同理：放在 prune 的「未到周期就 return」之前，否则永远轮不到。
        try:
            self._graph_autobuild()
        except Exception as exc:  # noqa: BLE001
            logger.debug("atbrains: 图谱增量抽取异常（忽略）: %s", str(exc)[:160])
        if not int(self.cfg.get("maintenance_enabled") or 0):
            return
        every = float(self.cfg.get("prune_every_days") or 0)
        if every > 0:
            last = self._meta_get("last_prune")
            if last:
                try:
                    prev = datetime.strptime(last, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                except ValueError:
                    prev = None
                if prev is not None:
                    age_days = (datetime.now(timezone.utc) - prev).total_seconds() / 86400.0
                    if age_days < every:
                        return
        rows = self._exec("SELECT GET_LOCK('brain_prune', 0) AS got", fetch=True)
        if not (rows and rows[0].get("got")):
            logger.debug("atbrains: 维护清理跳过（另一进程正在清理）")
            return
        try:
            result = self.prune(dry_run=False)
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            self._meta_set("last_prune", now)
            next_txt = f"{int(every)} 天后" if every > 0 else "每轮巡检"
            logger.info("atbrains: 维护清理完成 turns=%s sessions=%s archive=%s 下次 %s",
                        result.get("turns", 0), result.get("sessions", 0),
                        result.get("archives", 0), next_txt)
        finally:
            try:
                self._exec("SELECT RELEASE_LOCK('brain_prune')")
            except Exception:  # noqa: BLE001
                pass

    def ping(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        try:
            out["redis"] = bool(self.redis().ping())
        except Exception as exc:  # noqa: BLE001
            out["redis"] = False
            out["redis_error"] = str(exc)[:200]
        try:
            self._exec("SELECT 1", fetch=True)
            out["mariadb"] = True
        except Exception as exc:  # noqa: BLE001
            out["mariadb"] = False
            out["mariadb_error"] = str(exc)[:200]
        return out

    def stats(self, *, force: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        if not force and self._stats_cache and now - self._stats_cache[0] < self._cache_ttl:
            return self._stats_cache[1]
        out: dict[str, Any] = {"mysql": {}, "redis": {}}
        self.ensure_schema()
        row = self._exec(
            "SELECT (SELECT COUNT(*) FROM brain_memories) AS memories_total, "
            "(SELECT COUNT(*) FROM brain_memories WHERE active = 1) AS memories_active, "
            "(SELECT COUNT(*) FROM brain_turns) AS turns, "
            "(SELECT COUNT(*) FROM brain_turn_messages) AS turn_messages, "
            "(SELECT COALESCE(SUM(raw_bytes), 0) FROM brain_turn_messages) AS turn_messages_raw_bytes, "
            "(SELECT COUNT(*) FROM brain_claims WHERE state = 'active') AS claims_active, "
            "(SELECT COUNT(*) FROM brain_claim_versions) AS claim_versions, "
            "(SELECT COUNT(*) FROM brain_episodes) AS episodes, "
            "(SELECT COUNT(*) FROM brain_thoughts) AS thoughts, "
            "(SELECT COALESCE(SUM(reasoning_chars), 0) FROM brain_thoughts) AS thoughts_chars, "
            "(SELECT COUNT(*) FROM brain_tool_calls) AS tool_calls, "
            "(SELECT COUNT(*) FROM brain_tool_calls WHERE status = 'error') AS tool_call_errors, "
            "(SELECT COUNT(*) FROM brain_sessions) AS sessions, "
            "(SELECT COUNT(*) FROM brain_archive) AS archives, "
            "(SELECT COUNT(*) FROM brain_memories WHERE active = 1 AND access_count > 0) AS memories_accessed, "
            "(SELECT COALESCE(SUM(access_count), 0) FROM brain_memories WHERE active = 1) AS access_total, "
            "(SELECT COUNT(*) FROM brain_memories WHERE active = 1 AND expires_at IS NOT NULL) AS memories_with_ttl, "
            "(SELECT COUNT(*) FROM brain_entities) AS entities, "
            "(SELECT COUNT(*) FROM brain_edges) AS graph_edges, "
            "(SELECT COUNT(*) FROM brain_entity_mentions) AS entity_mentions",
            fetch=True)[0]
        size = self._exec(
            "SELECT COALESCE(SUM(data_length + index_length), 0) AS bytes "
            "FROM information_schema.TABLES WHERE table_schema = %s",
            (self.cfg["mariadb_db"],), fetch=True)[0]
        out["mysql"] = {k: int(v or 0) for k, v in row.items()}
        out["mysql"]["size_bytes"] = int(size["bytes"] or 0)
        # 按写入方拆分（跨 agent 共享同一张表，统计仍要能分开看）
        try:
            agent_rows: Any = self._exec(
                "SELECT agent, COUNT(*) AS total, SUM(active = 1) AS active "
                "FROM brain_memories GROUP BY agent ORDER BY total DESC", fetch=True)
            out["agents"] = [{"agent": r["agent"] or "", "total": int(r["total"] or 0),
                              "active": int(r["active"] or 0)} for r in agent_rows]
        except Exception as exc:  # noqa: BLE001
            out["agents"] = []
            logger.debug("atbrains: agent 统计失败: %s", str(exc)[:160])
        try:
            r = self.redis()
            out["redis"] = {
                "keys": int(r.dbsize()),
                "recent_stream": int(r.xlen(self._rk("recent"))),
                "hot_index": int(r.zcard(self._rk("hot", "index"))),
                "used_memory": str(r.info("memory").get("used_memory_human", "")),
            }
        except Exception as exc:  # noqa: BLE001
            out["redis"] = {"error": str(exc)[:200]}
        self._stats_cache = (now, out)
        return out

    def prune(self, *, turns_keep_days: Optional[int] = None,
              archive_keep_days: Optional[int] = None,
              inactive_keep_days: Optional[int] = None,
              sessions_keep_days: Optional[int] = None,
              dry_run: bool = False) -> dict[str, Any]:
        def _days(value: Optional[int], key: str) -> int:
            return int(value if value is not None else (self.cfg.get(key) or 0))

        t_days = _days(turns_keep_days, "turns_keep_days")
        a_days = _days(archive_keep_days, "archive_keep_days")
        i_days = _days(inactive_keep_days, "inactive_keep_days")
        s_days = _days(sessions_keep_days, "sessions_keep_days")
        self.ensure_schema()
        targets: list[tuple[str, str, str, tuple]] = []
        if t_days > 0:
            targets.append((
                "turns",
                "DELETE FROM brain_turns WHERE ts < (NOW() - INTERVAL %s DAY)",
                "SELECT COUNT(*) AS c FROM brain_turns WHERE ts < (NOW() - INTERVAL %s DAY)",
                (t_days,)))
        if a_days > 0:
            targets.append((
                "archives",
                "DELETE FROM brain_archive WHERE ts < (NOW() - INTERVAL %s DAY)",
                "SELECT COUNT(*) AS c FROM brain_archive WHERE ts < (NOW() - INTERVAL %s DAY)",
                (a_days,)))
        if i_days > 0:
            # 不用 COALESCE(deactivated_at, updated_at)：那会让谓词不可索引（全表扫）；
            # 拆成两段等价条件，正常行能走 deactivated_at 索引。
            inactive_cond = ("active = 0 AND (deactivated_at < (NOW() - INTERVAL %s DAY) "
                             "OR (deactivated_at IS NULL AND updated_at < (NOW() - INTERVAL %s DAY)))")
            targets.append((
                "inactive_memories",
                f"DELETE FROM brain_memories WHERE {inactive_cond}",
                f"SELECT COUNT(*) AS c FROM brain_memories WHERE {inactive_cond}",
                (i_days, i_days)))
        if int(self.cfg.get("ttl_enabled") or 0):
            # 按类型 TTL 到期 → 停用（不是删除：仍受 inactive_keep_days 保护，可 restore）。
            # updated_at = updated_at 保持“最后真实更新时间”不变，避免污染召回排序。
            expired_cond = "active = 1 AND expires_at IS NOT NULL AND expires_at < UTC_TIMESTAMP()"
            targets.append((
                "expired",
                "UPDATE brain_memories SET active = 0, deactivated_at = UTC_TIMESTAMP(), "
                f"updated_at = updated_at WHERE {expired_cond}",
                f"SELECT COUNT(*) AS c FROM brain_memories WHERE {expired_cond}",
                ()))
        if s_days > 0:
            # 会话索引：只删“旧、且其轮次已不在库中（或同样过期）”的行
            cond = ("last_active < (NOW() - INTERVAL %s DAY) AND NOT EXISTS "
                    "(SELECT 1 FROM brain_turns t WHERE t.session_id = brain_sessions.session_id "
                    "AND t.ts >= (NOW() - INTERVAL %s DAY))")
            targets.append((
                "sessions",
                f"DELETE FROM brain_sessions WHERE {cond}",
                f"SELECT COUNT(*) AS c FROM brain_sessions WHERE {cond}",
                (s_days, s_days)))
        out: dict[str, Any] = {"dry_run": bool(dry_run)}
        if t_days > 0:
            # 先清转录层（按所属轮次的时间），再删轮次本体
            msg_cond = ("FROM brain_turn_messages m JOIN brain_turns t ON t.id = m.turn_id "
                        "WHERE t.ts < (NOW() - INTERVAL %s DAY)")
            if dry_run:
                out["turn_messages"] = int(self._exec(
                    "SELECT COUNT(*) AS c " + msg_cond, (t_days,), fetch=True)[0]["c"] or 0)
            else:
                rowcount, _ = self._exec("DELETE m " + msg_cond, (t_days,))
                out["turn_messages"] = int(rowcount)
            # 情节头与证据链随轮次一起清（主张版本本身保留，只把断掉的证据引用摘掉）
            ep_cond = "FROM brain_episodes e JOIN brain_turns t ON t.id = e.turn_id WHERE t.ts < (NOW() - INTERVAL %s DAY)"
            ev_cond = ("FROM brain_claim_evidence e JOIN brain_turns t ON t.id = e.turn_id "
                       "WHERE t.ts < (NOW() - INTERVAL %s DAY)")
            # v9 的两张投影表同样随轮次清理（不要留孤儿行）
            th_cond = ("FROM brain_thoughts x JOIN brain_turns t ON t.id = x.turn_id "
                       "WHERE t.ts < (NOW() - INTERVAL %s DAY)")
            tc_cond = ("FROM brain_tool_calls x JOIN brain_turns t ON t.id = x.turn_id "
                       "WHERE t.ts < (NOW() - INTERVAL %s DAY)")
            if dry_run:
                out["episodes"] = int(self._exec("SELECT COUNT(*) AS c " + ep_cond, (t_days,), fetch=True)[0]["c"] or 0)
                out["claim_evidence"] = int(self._exec("SELECT COUNT(*) AS c " + ev_cond, (t_days,), fetch=True)[0]["c"] or 0)
                out["thoughts"] = int(self._exec("SELECT COUNT(*) AS c " + th_cond, (t_days,), fetch=True)[0]["c"] or 0)
                out["tool_calls"] = int(self._exec("SELECT COUNT(*) AS c " + tc_cond, (t_days,), fetch=True)[0]["c"] or 0)
            else:
                out["claim_evidence"] = int(self._exec("DELETE e " + ev_cond, (t_days,))[0] or 0)
                out["episodes"] = int(self._exec("DELETE e " + ep_cond, (t_days,))[0] or 0)
                out["thoughts"] = int(self._exec("DELETE x " + th_cond, (t_days,))[0] or 0)
                out["tool_calls"] = int(self._exec("DELETE x " + tc_cond, (t_days,))[0] or 0)
                self._exec(
                    "UPDATE brain_claim_versions v LEFT JOIN brain_turns t ON t.id = v.source_turn_id "
                    "SET v.source_turn_id = NULL WHERE v.source_turn_id IS NOT NULL AND t.id IS NULL")
        for name, delete_sql, count_sql, params in targets:
            if dry_run:
                out[name] = int(self._exec(count_sql, params, fetch=True)[0]["c"] or 0)
            else:
                rowcount, _ = self._exec(delete_sql, params)
                out[name] = int(rowcount)
        out["skipped"] = [name for name, days in (("turns", t_days), ("archives", a_days),
                                                  ("inactive_memories", i_days), ("sessions", s_days))
                          if days <= 0]
        if not int(self.cfg.get("ttl_enabled") or 0):
            out["skipped"].append("expired")
        return out

    def doctor(self) -> dict[str, Any]:
        """全面体检（纯 Python，不依赖任何外部 agent CLI）：依赖 / 凭据 / 连通性 / 表结构 / 向量。"""
        checks: dict[str, Any] = {"ok": True, "deps": {}, "credentials": {}, "ping": {}, "schema": {}}
        for pkg in ("redis", "pymysql"):
            try:
                __import__(pkg)
                checks["deps"][pkg] = True
            except Exception as exc:  # noqa: BLE001
                checks["deps"][pkg] = False
                checks["deps"][f"{pkg}_error"] = str(exc)[:160]
                checks["ok"] = False
        checks["credentials"]["redis_password"] = bool(self.cfg.get("redis_password"))
        checks["credentials"]["mariadb_password"] = bool(self.cfg.get("mariadb_password"))
        ping = self.ping()
        checks["ping"] = ping
        if not (ping.get("redis") is True and ping.get("mariadb") is True):
            checks["ok"] = False
        try:
            self.ensure_schema()
            ver = self.schema_version()
            tables = [r["TABLE_NAME"] for r in self._exec(
                "SELECT TABLE_NAME FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE' ORDER BY TABLE_NAME",
                (self.cfg["mariadb_db"],), fetch=True)]
            checks["schema"] = {"ok": True, "version": ver, "tables": tables}
        except Exception as exc:  # noqa: BLE001
            checks["schema"] = {"ok": False, "error": str(exc)[:200]}
            checks["ok"] = False
        try:
            checks["vector"] = self.vector_status()
        except Exception as exc:  # noqa: BLE001
            checks["vector"] = {"error": str(exc)[:200]}
        return checks

    def overview(self, *, fresh: bool = False) -> dict[str, Any]:
        """管理台与 CLI 总览（直读引擎与缓存，不再强起 docker/systemctl 子进程）。"""
        out: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "version": PROTOCOL_VERSION,
            "server_version": __version__,
        }
        for key, fn in (
            ("stats", lambda: self.stats(force=fresh)),
            ("ping", lambda: self.ping()),
            ("vector", lambda: self.vector_status(force=fresh)),
            ("qdrant", lambda: self.qdrant_info()),
        ):
            try:
                out[key] = fn()
            except Exception as exc:  # noqa: BLE001
                out[key] = {"error": f"{type(exc).__name__}: {exc}"}
        return out

    def recent(self, limit: int = 20, *, ctx: Optional[RequestContext] = None) -> dict[str, Any]:
        eff = max(1, min(int(limit), 100))
        return {"hot": self.hot_recent(eff), "turns": self.recent_turns(eff, ctx=ctx)}

    def quick_counts(self) -> dict[str, int]:
        """醒来包专用轻量计数：命中暖 stats 缓存则零 SQL；冷时只跑 5 个单列 COUNT。"""
        now = time.monotonic()
        if self._stats_cache and now - self._stats_cache[0] < self._cache_ttl:
            mysql = dict((self._stats_cache[1] or {}).get("mysql") or {})
            return {
                k: int(mysql.get(k) or 0)
                for k in ("memories_active", "turns", "claims_active", "episodes", "sessions")
                if k in mysql
            }
        self.ensure_schema()
        row = self._exec(
            "SELECT (SELECT COUNT(*) FROM brain_memories WHERE active = 1) AS memories_active, "
            "(SELECT COUNT(*) FROM brain_turns) AS turns, "
            "(SELECT COUNT(*) FROM brain_claims WHERE state = 'active') AS claims_active, "
            "(SELECT COUNT(*) FROM brain_episodes) AS episodes, "
            "(SELECT COUNT(*) FROM brain_sessions) AS sessions",
            fetch=True,
        )[0]
        return {k: int(v or 0) for k, v in row.items()}
