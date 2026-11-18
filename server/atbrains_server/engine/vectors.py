"""向量层：embedding、Qdrant、向量回填、混合召回（RRF / 衰减 / 访问强化）。"""
from __future__ import annotations

import logging
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Optional
import hashlib
import json
import math
import threading
import time

from ..util import (  # noqa: F401
    BrainError,
    BrainHTTPError,
    _age_days,
    _memory_type,
)

logger = logging.getLogger("atbrains")

class VectorsMixin:
    """向量层：embedding、Qdrant、向量回填、混合召回（RRF / 衰减 / 访问强化）。"""

    def embed_texts(self, texts: list[str], *, timeout: Optional[float] = None) -> list[list[float]]:
        key = self._vector_secrets().get("embed")
        if not key:
            raise BrainError("向量层未就绪：未找到 embed API key（设 ATBRAINS_EMBED_API_KEY 或 qdrant_config_file）")
        url = str(self.cfg["embed_base_url"]).rstrip("/") + "/embeddings"
        limit = int(self.cfg.get("vector_text_max") or 12000)
        payload = {"model": str(self.cfg["embed_model"]),
                   "input": [str(t)[:limit] for t in texts],
                   "encoding_format": "float"}
        _, data = self._http_json("POST", url, headers={"Authorization": f"Bearer {key}"},
                                  payload=payload,
                                  timeout=timeout or float(self.cfg.get("embed_timeout") or 60))
        items = sorted(data.get("data") or [], key=lambda d: int(d.get("index") or 0))
        vecs = [d.get("embedding") or [] for d in items]
        if len(vecs) != len(texts) or any(not v for v in vecs):
            raise BrainError(f"embedding 响应异常（期望 {len(texts)} 条向量）")
        return vecs

    def _qdrant_headers(self) -> dict:
        key = self._vector_secrets().get("qdrant")
        return {"api-key": key} if key else {}

    def _qdrant(self, method: str, path: str, payload: Optional[dict] = None,
                timeout: float = 15) -> dict:
        url = str(self.cfg["qdrant_url"]).rstrip("/") + path
        # 明确 no_proxy：向量层是本地服务，绝不能因为进程带着 HTTP_PROXY 而兜一圈代理
        _, data = self._http_json(method, url, headers=self._qdrant_headers(),
                                  payload=payload, timeout=timeout, no_proxy=True)
        return data

    def qdrant_ensure_collection(self) -> None:
        name = str(self.cfg["qdrant_collection"])
        dim = int(self.cfg["embed_dim"])
        try:
            data = self._qdrant("GET", f"/collections/{name}")
        except BrainHTTPError as exc:
            if exc.code != 404:
                raise
            self._qdrant("PUT", f"/collections/{name}",
                         {"vectors": {"size": dim, "distance": "Cosine"}}, timeout=30)
            logger.info("atbrains: 已创建 Qdrant 集合 %s（%s 维, Cosine）", name, dim)
            return
        vectors = (((data.get("result") or {}).get("config") or {})
                   .get("params") or {}).get("vectors")
        size = vectors.get("size") if isinstance(vectors, dict) else None
        if size and int(size) != dim:
            raise BrainError(f"Qdrant 集合 {name} 维度为 {size}，与配置 {dim} 不一致："
                             "需换集合名或清库重建")

    def qdrant_upsert(self, points: list[dict]) -> None:
        name = str(self.cfg["qdrant_collection"])
        self._qdrant("PUT", f"/collections/{name}/points?wait=true", {"points": points}, timeout=30)

    def qdrant_delete_points(self, ids: list[int]) -> None:
        if not ids:
            return
        name = str(self.cfg["qdrant_collection"])
        self._qdrant("POST", f"/collections/{name}/points/delete?wait=true",
                     {"points": [int(i) for i in ids]}, timeout=30)

    def qdrant_query(self, vector: list[float], topk: int) -> list[dict]:
        name = str(self.cfg["qdrant_collection"])
        data = self._qdrant("POST", f"/collections/{name}/points/query",
                            {"query": vector, "limit": int(topk), "with_payload": False},
                            timeout=20)
        pts = ((data.get("result") or {}).get("points")) or []
        return [{"id": int(p["id"]), "score": float(p.get("score") or 0.0)} for p in pts]

    def qdrant_get_vectors(self, ids: list[int]) -> dict[int, list[float]]:
        """按 id 取 Qdrant 里已有的向量（不重新调 embed API）；缺失的 id 不在返回结果里。"""
        name = str(self.cfg["qdrant_collection"])
        out: dict[int, list[float]] = {}
        for i in range(0, len(ids), 256):
            chunk = [int(x) for x in ids[i:i + 256]]
            data = self._qdrant("POST", f"/collections/{name}/points",
                                {"ids": chunk, "with_vector": True, "with_payload": False},
                                timeout=30)
            for p in (data.get("result") or []):
                vec = p.get("vector")
                if isinstance(vec, dict):        # 命名向量兜底
                    vec = next((v for v in vec.values() if isinstance(v, list)), None)
                if isinstance(vec, list) and vec:
                    out[int(p["id"])] = [float(x) for x in vec]
        return out

    def _pending_memories(self, limit: int, *, force: bool = False,
                          cursor: int = 0) -> list[dict]:
        self.ensure_schema()
        base = ("SELECT m.id, m.content, m.kind, m.tags, m.importance, m.machine, m.agent, m.content_hash "
                "FROM brain_memories m ")
        if force:
            sql = base + ("WHERE m.active = 1 AND m.id > %s "
                          "ORDER BY m.id ASC LIMIT %s")
            return list(self._exec(sql, (int(cursor), int(limit)), fetch=True))
        sql = (base +
               "LEFT JOIN brain_vector_index v ON v.mem_id = m.id "
               "WHERE m.active = 1 AND ("
               "v.mem_id IS NULL "
               "OR (v.status = 'ok' AND (v.model <> %s OR v.content_hash IS NULL "
               "OR v.content_hash <> m.content_hash)) "
               "OR (v.status = 'error' AND v.updated_at < (NOW() - INTERVAL 30 MINUTE))"
               ") ORDER BY m.updated_at DESC, m.id DESC LIMIT %s")
        return list(self._exec(sql, (str(self.cfg["embed_model"])[:64], int(limit)), fetch=True))

    def _mark_vector(self, mem_id: int, *, status: str, model: str = "", dim: int = 0,
                     content_hash: Optional[str] = None, error: str = "") -> None:
        # 库内时间统一 UTC；本机时区的 time.strftime 会让 vector-status 的 last_ok 错位
        embedded_at = (datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                       if status == "ok" else None)
        self._exec(
            "INSERT INTO brain_vector_index (mem_id, status, model, dim, content_hash, error, embedded_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE status = VALUES(status), model = VALUES(model), dim = VALUES(dim), "
            "content_hash = VALUES(content_hash), error = VALUES(error), embedded_at = VALUES(embedded_at)",
            (int(mem_id), status[:16], str(model)[:64], int(dim), content_hash, str(error)[:255],
             embedded_at))

    def drain_vectors(self, *, max_items: int = 0, force: bool = False) -> dict:
        """把未向量化的记忆推入 Qdrant（跨进程用 GET_LOCK 串行，失败带节流重试）。"""
        stats = {"enabled": bool(int(self.cfg.get("vector_enabled") or 0)),
                 "embedded": 0, "errors": 0, "deleted": 0, "remaining": 0}
        if not stats["enabled"]:
            return stats
        if not force and not self._vec_dirty:
            # 没有新写入：pending 的 LEFT JOIN + OR 是全表扫，60 秒定时唤醒不必跑。
            # 每 10 次仍放行一轮，兜底重建/手工改库后的欠账。
            self._drain_ticks += 1
            if self._drain_ticks % 10 != 0:
                stats["remaining"] = self._drain_last_remaining
                return stats
        self._drain_ticks = 0
        self._vec_dirty = False
        self._vector_cache = None
        got = False
        try:
            rows = self._exec("SELECT GET_LOCK('brain_vector_drain', 0) AS got", fetch=True)
            got = bool(rows and rows[0].get("got"))
            if not got:
                logger.info("atbrains: 另一路 drain 正持有锁，本轮跳过")
                stats["skipped_locked"] = True
                return stats
            if not self._qdrant_ready:
                self.qdrant_ensure_collection()
                self._qdrant_ready = True
            model = str(self.cfg["embed_model"])
            batch_n = max(1, int(self.cfg.get("vector_drain_batch") or 8))
            budget = int(max_items) if int(max_items) > 0 else 10 ** 9
            cursor = 0
            while budget > 0:
                pending = self._pending_memories(min(batch_n, budget), force=force, cursor=cursor)
                if not pending:
                    break
                if force:
                    cursor = max(int(r["id"]) for r in pending)
                texts = [str(r["content"]) for r in pending]
                try:
                    vecs: list = list(self.embed_texts(texts))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("atbrains: 批量向量化失败（%s），逐条重试", str(exc)[:160])
                    vecs = []
                    for t in texts:
                        try:
                            vecs.append(self.embed_texts([t])[0])
                        except Exception as exc2:  # noqa: BLE001
                            logger.warning("atbrains: 单条向量化失败: %s", str(exc2)[:160])
                            vecs.append(None)
                ok_pairs = []
                for row, vec in zip(pending, vecs):
                    if vec:
                        ok_pairs.append((row, vec))
                    else:
                        self._mark_vector(int(row["id"]), status="error", model=model,
                                          error="embed 失败")
                        stats["errors"] += 1
                if ok_pairs:
                    points = [{
                        "id": int(row["id"]),
                        "vector": vec,
                        "payload": {"kind": row["kind"], "importance": int(row["importance"]),
                                    "tags": row["tags"], "machine": row["machine"],
                                    "agent": row.get("agent", ""),
                                    "preview": str(row["content"])[:200]},
                    } for row, vec in ok_pairs]
                    try:
                        self.qdrant_upsert(points)
                        for row, vec in ok_pairs:
                            self._mark_vector(int(row["id"]), status="ok", model=model, dim=len(vec),
                                              content_hash=row["content_hash"])
                            stats["embedded"] += 1
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("atbrains: Qdrant 写入失败: %s", str(exc)[:160])
                        for row, _vec in ok_pairs:
                            self._mark_vector(int(row["id"]), status="error", model=model,
                                              error=f"qdrant: {str(exc)[:200]}")
                        stats["errors"] += len(ok_pairs)
                budget -= len(pending)
                logger.info("atbrains: 向量化批次 +%s（成功累计 %s / 失败累计 %s）",
                            len(pending), stats["embedded"], stats["errors"])
                if len(pending) < batch_n:
                    break
            # 清理已失活记忆的向量
            gone = self._exec(
                "SELECT v.mem_id FROM brain_vector_index v "
                "JOIN brain_memories m ON m.id = v.mem_id WHERE m.active = 0 LIMIT 50", fetch=True)
            if gone:
                ids = [int(g["mem_id"]) for g in gone]
                try:
                    self.qdrant_delete_points(ids)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("atbrains: 清理 Qdrant 失活向量失败: %s", exc)
                marks = ", ".join(["%s"] * len(ids))
                self._exec(f"DELETE FROM brain_vector_index WHERE mem_id IN ({marks})", tuple(ids))
                stats["deleted"] = len(ids)
            # remaining 的 LEFT JOIN COUNT 也是全表级；没有新写入时复用上次结果（≤60 秒）
            if (stats["embedded"] or stats["errors"] or stats["deleted"] or force
                    or time.monotonic() - self._drain_remaining_ts > 60):
                row = self._exec(
                    "SELECT COUNT(*) AS c FROM brain_memories m LEFT JOIN brain_vector_index v "
                    "ON v.mem_id = m.id WHERE m.active = 1 AND (v.mem_id IS NULL OR v.status <> 'ok' "
                    "OR v.model <> %s OR (v.content_hash IS NULL OR v.content_hash <> m.content_hash))",
                    (model[:64],), fetch=True)
                stats["remaining"] = int(row[0]["c"] or 0)
                self._drain_last_remaining = stats["remaining"]
                self._drain_remaining_ts = time.monotonic()
            else:
                stats["remaining"] = self._drain_last_remaining
        finally:
            if got:
                try:
                    self._exec("SELECT RELEASE_LOCK('brain_vector_drain')")
                except Exception:  # noqa: BLE001
                    pass
        return stats

    def _query_embedding(self, query: str, *, timeout: Optional[float] = None) -> list[float]:
        """查询向量化：Redis 缓存（默认 1 小时），降低重复查询的延迟与波动。"""
        cache_key = self._rk("qemb", hashlib.sha1(
            (str(self.cfg.get("embed_model")) + "\x00" + query).encode("utf-8")).hexdigest())
        try:
            raw = self.redis().get(cache_key)
            if raw:
                val = json.loads(raw)
                if isinstance(val, list) and val:
                    return [float(x) for x in val]
        except Exception:  # noqa: BLE001
            pass
        vec = self.embed_texts(
            [query], timeout=timeout or float(self.cfg.get("vector_query_timeout") or 20))[0]
        try:
            self.redis().set(cache_key, json.dumps([round(float(x), 6) for x in vec]),
                             ex=int(self.cfg.get("vector_qemb_ttl") or 3600))
        except Exception:  # noqa: BLE001
            pass
        return vec

    def vector_search_memories(self, query: str, *, limit: int = 8,
                               kind: Optional[str] = None,
                               agent: Optional[str] = None) -> list[dict[str, Any]]:
        if not int(self.cfg.get("vector_enabled") or 0) or not (query or "").strip():
            return []
        vec = self._query_embedding(query)
        hits = self.qdrant_query(vec, int(self.cfg.get("vector_topk") or 12))
        cutoff = float(self.cfg.get("vector_min_score") or 0.0)
        hits = [h for h in hits if h["score"] >= cutoff]
        if not hits:
            return []
        ids = [h["id"] for h in hits]
        marks = ", ".join(["%s"] * len(ids))
        sql = ("SELECT id, kind, memory_type, content, tags, importance, source, session_id, machine, agent, "
               "created_at, updated_at, access_count, last_accessed_at, expires_at "
               f"FROM brain_memories WHERE active = 1 AND superseded_by IS NULL AND id IN ({marks}) ")
        params: list = list(ids)
        if kind:
            sql += "AND kind = %s "
            params.append(kind)
        if agent:
            sql += "AND agent = %s "
            params.append(str(agent).strip().lower()[:32])
        self.ensure_schema()
        rows = {int(r["id"]): r for r in self._exec(sql, tuple(params), fetch=True)}
        out = []
        for h in hits:
            r = rows.get(h["id"])
            if r:
                r = dict(r)
                r["vscore"] = round(h["score"], 4)
                r["via"] = "vec"
                out.append(r)
        return out[: int(limit)]

    def search_memories_hybrid(self, query: str, *, limit: int = 5,
                               kind: Optional[str] = None,
                               agent: Optional[str] = None) -> list[dict[str, Any]]:
        """关键词 + 向量 RRF 融合；向量任一环节失败自动回落关键词。"""
        # 融合池必须对称：关键词路若只取 limit 条，第 limit+1..topk 名的纯关键词命中
        # 根本进不了 RRF（向量路却取满 topk），等于结构性偏袒向量路。
        pool = max(int(limit) * 3, int(self.cfg.get("vector_topk") or 12))
        kw = self.search_memories(query, limit=pool, kind=kind, agent=agent)
        try:
            vec = self.vector_search_memories(
                query, limit=int(self.cfg.get("vector_topk") or 12), kind=kind, agent=agent)
        except Exception as exc:  # noqa: BLE001
            logger.warning("atbrains: 向量检索失败，回落纯关键词: %s", str(exc)[:200])
            vec = []
        # 第三路：图谱（实体命中 + 1 跳邻居）。必须在「向量为空就返回」之前算，
        # 否则向量层一旦降级，图谱这一路也一起失效。
        graph: list[dict[str, Any]] = []
        w_graph = 0.0
        if int(self.cfg.get("graph_enabled") or 0):
            try:
                w_graph = float(self.cfg.get("graph_route_weight") or 0.0)
            except (TypeError, ValueError):
                w_graph = 0.0
            if w_graph > 0:
                try:
                    graph = self.graph_search_memories(query, limit=max(pool, 12))
                except Exception as exc:  # noqa: BLE001
                    logger.debug("atbrains: 图谱召回失败（忽略）: %s", str(exc)[:160])
                    graph = []
        if not vec and not graph:
            for m in kw:
                m.setdefault("via", "kw")
            return kw
        k = float(self.cfg.get("vector_rrf_k") or 60)
        scores: dict[int, float] = {}
        items: dict[int, dict] = {}
        kw_ids, vec_ids = set(), set()
        graph_ids: set[int] = set()
        for rank, m in enumerate(kw):
            mid = int(m["id"])
            kw_ids.add(mid)
            scores[mid] = scores.get(mid, 0.0) + 1.0 / (k + rank + 1)
            items.setdefault(mid, m)
        for rank, m in enumerate(vec):
            mid = int(m["id"])
            vec_ids.add(mid)
            scores[mid] = scores.get(mid, 0.0) + 1.0 / (k + rank + 1)
            if mid in items:
                items[mid]["vscore"] = m.get("vscore")
            else:
                items[mid] = m
        # 第三路打分（图谱在上面的提前返回判断之前已经取好，这里只计分）
        if w_graph > 0:
            for rank, m in enumerate(graph):
                mid = int(m["id"])
                graph_ids.add(mid)
                scores[mid] = scores.get(mid, 0.0) + w_graph / (k + rank + 1)
                items.setdefault(mid, m)
        # 访问强化：对所有层生效（这是“有没有被用到”，不是“新旧”），与衰减相互独立
        self._apply_access_boost(items, scores)
        # 时间衰减：RRF 打分完成后、排序之前，对白名单层（默认 episodic）乘衰减系数
        if int(self.cfg.get("decay_enabled") or 0):
            self._apply_decay(items, scores)
        ordered = sorted(items.items(),
                         key=lambda kv: (-scores[kv[0]], -int(kv[1].get("importance") or 0)))
        out = []
        for mid, m in ordered:
            m = dict(m)
            if mid in graph_ids:
                m["via"] = "+".join(
                    p for p, s in (("kw", kw_ids), ("vec", vec_ids), ("graph", graph_ids)) if mid in s)
            else:
                m["via"] = ("both" if (mid in kw_ids and mid in vec_ids)
                            else ("vec" if mid in vec_ids else "kw"))
            out.append(m)
        if out and int(self.cfg.get("rerank_enabled") or 0):
            try:
                out = self.rerank(query, out, limit=int(limit))
            except Exception as exc:  # noqa: BLE001
                logger.warning("atbrains: rerank 失败，保持 RRF 顺序: %s", str(exc)[:200])
        return out[: int(limit)]

    def _apply_decay(self, items: dict[int, dict], scores: dict[int, float]) -> None:
        """按记忆分层做时间衰减（原地改 scores）；年龄取不到时 factor=1，不丢结果。"""
        layers = {t.strip() for t in str(self.cfg.get("decay_types") or "").split(",") if t.strip()}
        try:
            half_life = float(self.cfg.get("decay_half_life_days") or 0)
        except (TypeError, ValueError):
            half_life = 0.0
        if not layers or half_life <= 0:
            return
        now_ts = time.time()
        for mid, score in list(scores.items()):
            item = items.get(mid) or {}
            layer = str(item.get("memory_type") or "").strip() or _memory_type(str(item.get("kind") or ""))
            if layer not in layers:
                continue
            age = _age_days(item.get("updated_at"), now_ts)
            # 被注入/回忆过就不再按“写于何时”老化：取 updated_at 与 last_accessed_at 里更新的那个
            age2 = _age_days(item.get("last_accessed_at"), now_ts)
            if age2 is not None and (age is None or age2 < age):
                age = age2
            if age is None:
                age = _age_days(item.get("created_at"), now_ts)
            if age is None:
                continue
            scores[mid] = score * (0.5 ** (age / half_life))

    def _apply_access_boost(self, items: dict[int, dict], scores: dict[int, float]) -> None:
        """按 access_count 提权（原地改 scores）：1 + factor*log1p(count)。

        只乘不除，count=0 时乘数为 1（与关闭本功能逐字节等价）；任何异常都不影响排序。
        """
        if not int(self.cfg.get("access_boost_enabled") or 0):
            return
        try:
            factor = float(self.cfg.get("access_boost_factor") or 0.0)
        except (TypeError, ValueError):
            factor = 0.0
        if factor <= 0:
            return
        for mid, score in list(scores.items()):
            item = items.get(mid) or {}
            try:
                n = int(item.get("access_count") or 0)
            except (TypeError, ValueError):
                n = 0
            if n > 0:
                scores[mid] = score * (1.0 + factor * math.log1p(n))

    def start_vector_drainer(self) -> None:
        """启动进程级单实例后台向量 drainer（仅服务端 worker 调用）。"""
        if not int(self.cfg.get("vector_enabled") or 0):
            return
        with self._drainer_lock:
            if self._drainer_started:
                return
            self._drainer_started = True

        def _loop() -> None:
            interval = max(15, int(self.cfg.get("vector_drain_interval") or 60))
            try:
                self.drain_vectors()
            except Exception as exc:  # noqa: BLE001
                logger.debug("atbrains: drainer 首扫异常: %s", exc)
            while True:
                try:
                    self._vec_wake.wait(timeout=interval)
                    self._vec_wake.clear()
                    self.drain_vectors()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("atbrains: 向量 drainer 轮次异常: %s", exc)
                time.sleep(0.5)

        try:
            threading.Thread(target=_loop, name="atbrains-vector-drain", daemon=True).start()
        except Exception as exc:  # noqa: BLE001
            with self._drainer_lock:
                self._drainer_started = False
            logger.warning("atbrains: 启动向量 drainer 失败: %s", exc)

    def qdrant_info(self) -> dict[str, Any]:
        """Qdrant 集合与活跃记忆向量覆盖率详情（供管理台/监控使用）。"""
        name = str(self.cfg["qdrant_collection"])
        res: dict[str, Any] = {"url": str(self.cfg["qdrant_url"]), "collection": name}
        try:
            data = self._qdrant("GET", f"/collections/{name}", timeout=10)
            rr = data.get("result") or {}
            params = rr.get("config", {}).get("params", {}).get("vectors", {})
            res.update({
                "status": rr.get("status"),
                "optimizer": rr.get("optimizer_status"),
                "points": rr.get("points_count"),
                "indexed": rr.get("indexed_vectors_count"),
                "segments": rr.get("segments_count"),
                "dim": params.get("size"),
                "distance": params.get("distance"),
            })
            ids: set[int] = set()
            offset: Any = None
            while True:
                body: dict[str, Any] = {"limit": 2000, "with_payload": False, "with_vector": False}
                if offset is not None:
                    body["offset"] = offset
                scroll = self._qdrant("POST", f"/collections/{name}/points/scroll", body, timeout=20)
                sr = scroll.get("result") or {}
                pts = sr.get("points") or []
                for p in pts:
                    ids.add(int(p["id"]))
                offset = sr.get("next_page_offset")
                if offset is None or not pts:
                    break
            self.ensure_schema()
            rows = self._exec("SELECT id FROM brain_memories WHERE active = 1", fetch=True)
            active = {int(x["id"]) for x in rows}
            res["coverage"] = {
                "active_memories": len(active),
                "with_vector": len(active & ids),
                "missing_count": len(active - ids),
                "missing": sorted(active - ids)[:50],
                "stale_vectors": sorted(ids - active)[:50],
            }
        except Exception as exc:  # noqa: BLE001
            res["error"] = f"{type(exc).__name__}: {exc}"
        return res
