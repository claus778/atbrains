"""记忆整理：访问计数回写、rerank、取代关系、合并。"""
from __future__ import annotations

import logging
from typing import Any
from typing import Optional
import json
import time

from ..util import (  # noqa: F401
    BrainError,
    _clip,
    _cosine,
    _memory_type,
)

logger = logging.getLogger("atbrains")

class CurationMixin:
    """记忆整理：访问计数回写、rerank、取代关系、合并。"""

    def _bump_access(self, ids: list[int]) -> None:
        """召回命中即计数（只写 Redis：HINCRBY + 脏集合，零 DB 压力）。"""
        if not ids or not int(self.cfg.get("access_boost_enabled") or 0):
            return
        try:
            r = self.redis()
            key, dirty = self._rk("hit"), self._rk("hit", "dirty")
            now = time.time()
            pipe = r.pipeline()
            for mid in list(ids)[:50]:
                pipe.hincrby(key, str(int(mid)), 1)
                pipe.zadd(dirty, {str(int(mid)): now})
            pipe.execute()
        except Exception as exc:  # noqa: BLE001
            logger.debug("atbrains: 访问计数失败（忽略）: %s", str(exc)[:160])

    def _flush_access(self) -> int:
        """把 Redis 里的访问计数批量回写 MariaDB（维护线程调用；幂等）。"""
        if not int(self.cfg.get("access_boost_enabled") or 0):
            return 0
        try:
            r = self.redis()
            key, dirty = self._rk("hit"), self._rk("hit", "dirty")
            members = [str(x) for x in r.zrange(dirty, 0, -1)]
            if not members:
                return 0
            counts = {str(k): int(v or 0) for k, v in (r.hgetall(key) or {}).items()}
            n = 0
            for mid in members:
                c = counts.get(mid, 0)
                if c <= 0:
                    continue
                # ⚠️ 必须显式 updated_at = updated_at：否则列上的 ON UPDATE CURRENT_TIMESTAMP
                # 会把“最后更新时间”冲到当下，直接污染召回排序（历史踩过一次）。
                self._exec(
                    "UPDATE brain_memories SET access_count = access_count + %s, "
                    "last_accessed_at = UTC_TIMESTAMP(), updated_at = updated_at "
                    "WHERE id = %s AND active = 1", (c, int(mid)))
                n += 1
            pipe = r.pipeline()
            if counts:
                pipe.hdel(key, *list(counts.keys()))
            pipe.zrem(dirty, *members)
            pipe.execute()
            if n:
                self._invalidate_caches()
            return n
        except Exception as exc:  # noqa: BLE001
            logger.warning("atbrains: 访问计数回写失败（下轮重试）: %s", str(exc)[:200])
            return 0

    def rerank(self, query: str, candidates: list[dict[str, Any]], *,
               limit: int) -> list[dict[str, Any]]:
        """用硅基流动 rerank 重排候选（只送前 rerank_topn 条；key 复用向量层 embed key）。

        失败（无 key / 网络 / 响应异常）直接抛异常，由调用方兜底回落原顺序。
        """
        head = list(candidates)[: max(1, int(self.cfg.get("rerank_topn") or 20))]
        if not head:
            return []
        key = self._vector_secrets().get("embed")
        if not key:
            raise BrainError("rerank 不可用：未找到 embed/rerank API key")
        payload = {
            "model": str(self.cfg.get("rerank_model")),
            "query": str(query or ""),
            "documents": [str(c.get("content") or "") for c in head],
            "top_n": len(head),
            "return_documents": False,
        }
        _, data = self._http_json(
            "POST", str(self.cfg.get("rerank_url")),
            headers={"Authorization": f"Bearer {key}"}, payload=payload,
            timeout=float(self.cfg.get("rerank_timeout") or 20))
        try:
            min_score = float(self.cfg.get("rerank_min_score") or 0.0)
        except (TypeError, ValueError):
            min_score = 0.0
        out: list[dict[str, Any]] = []
        for item in sorted(data.get("results") or [],
                           key=lambda r: -float(r.get("relevance_score") or 0.0)):
            idx = int(item.get("index") or 0)
            if not 0 <= idx < len(head):
                continue
            score = float(item.get("relevance_score") or 0.0)
            if score < min_score:
                continue
            m = dict(head[idx])
            m["rerank_score"] = round(score, 6)
            out.append(m)
        return out[: max(1, int(limit))]

    def detect_supersedes(self, *, dry_run: bool = True, threshold: float = 0.90,
                          limit: int = 200) -> dict[str, Any]:
        """同一 memory_type 内两两比对，把语义重叠的旧记忆标记为「已被新记忆取代」。

        只使用 Qdrant 里已存在的向量（不重新调 embed API），没有向量的行跳过；
        胜出方：created_at 更新者（相同则 id 更大者）；一条旧记忆只标记一次，不会成环。
        dry_run=True 只返回将要发生的变更，不写库。
        """
        self.ensure_schema()
        try:
            th = float(threshold)
        except (TypeError, ValueError):
            th = 0.90
        capped_limit = max(2, min(500, int(limit)))
        rows = [dict(r) for r in self._exec(
            "SELECT id, kind, memory_type, content, created_at, updated_at FROM brain_memories "
            "WHERE active = 1 AND superseded_by IS NULL ORDER BY created_at ASC, id ASC LIMIT %s",
            (capped_limit,), fetch=True)]
        out: dict[str, Any] = {"pairs": [], "applied": 0, "skipped": 0, "dry_run": bool(dry_run)}
        if len(rows) < 2:
            return out
        try:
            vectors = self.qdrant_get_vectors([int(r["id"]) for r in rows])
        except Exception as exc:  # noqa: BLE001
            logger.warning("atbrains: 读取 Qdrant 向量失败，本次不做取代检测: %s", str(exc)[:200])
            out["skipped"] = len(rows)
            out["error"] = str(exc)[:200]
            return out
        groups: dict[str, list[tuple[dict, list[float]]]] = {}
        for r in rows:
            vec = vectors.get(int(r["id"]))
            if not vec:
                out["skipped"] += 1     # 库里没有该 id 的向量：跳过
                continue
            layer = str(r.get("memory_type") or "").strip() or _memory_type(str(r.get("kind") or ""))
            groups.setdefault(layer, []).append((r, vec))
        cands: list[tuple[float, dict, dict, str]] = []
        for layer, group in groups.items():
            for i in range(len(group) - 1):
                ri, vi = group[i]
                for j in range(i + 1, len(group)):
                    rj, vj = group[j]
                    sim = _cosine(vi, vj)
                    if sim < th:
                        continue
                    # cands 元组语义 = (sim, lose, win, layer)：胜出方是时间更新者
                    # （created_at 相同则 id 更大者），旧的那条才是 lose。
                    if (ri["created_at"], int(ri["id"])) > (rj["created_at"], int(rj["id"])):
                        cands.append((sim, rj, ri, layer))
                    else:
                        cands.append((sim, ri, rj, layer))
        cands.sort(key=lambda c: (-c[0], int(c[1]["id"])))
        marked: set[int] = set()
        for sim, lose, win, layer in cands:
            lid, wid = int(lose["id"]), int(win["id"])
            if lid in marked:
                continue                # 一条旧记忆只标记一次（同时保证无环）
            marked.add(lid)
            out["pairs"].append({
                "id": lid, "superseded_by": wid, "similarity": round(float(sim), 4),
                "memory_type": layer, "kind": str(lose.get("kind") or ""),
                "content": _clip(str(lose.get("content") or ""), 160),
                "winner_kind": str(win.get("kind") or ""),
                "winner_content": _clip(str(win.get("content") or ""), 160),
            })
            if not dry_run:
                # 显式 updated_at = updated_at：标记取代关系不该刷新记忆的“最后更新/年龄”信号
                rowcount, _ = self._exec(
                    "UPDATE brain_memories SET superseded_by = %s, superseded_at = NOW(), "
                    "updated_at = updated_at "
                    "WHERE id = %s AND active = 1 AND superseded_by IS NULL", (wid, lid))
                if rowcount:
                    out["applied"] += 1
        if out["applied"]:
            logger.info("atbrains: 取代检测标记 %s 条旧记忆（阈值 %s）", out["applied"], th)
        return out

    def unsupersede(self, mem_id: int) -> dict[str, Any]:
        """撤销取代标记（可回滚）：把 superseded_by / superseded_at 置回 NULL。"""
        self.ensure_schema()
        rowcount, _ = self._exec(
            "UPDATE brain_memories SET superseded_by = NULL, superseded_at = NULL, "
            "updated_at = updated_at "
            "WHERE id = %s AND superseded_by IS NOT NULL", (int(mem_id),))
        return {"id": int(mem_id), "cleared": rowcount}

    _CONSOLIDATE_PROMPT = (
        "你是记忆合并器。输入是两条语义高度相似的长期记忆（余弦相似度 {sim}）。\n"
        "判定规则：\n"
        "  * 若它们表达同一件事的不同说法，或同一条事实的详略两个版本 → 合并成一条："
        "信息取并集、冲突处以时间较新的那条为准、必须保留具体的数字/端口/路径/账号，不要编造。\n"
        "  * 若它们虽然相近但确实是两件事（不同对象、不同阶段、并列事实）→ keep。\n"
        "只输出严格 JSON，无多余文字：{\"action\":\"merge|keep\",\"content\":\"合并后的完整句子\"}"
    )

    def _merge_pair(self, older: dict[str, Any], newer: dict[str, Any],
                    sim: float) -> Optional[str]:
        """让 LLM 判定两条近重复是否该合并。

        返回值三态：合并后的文本 / "" = 判为 keep / None = 调用或鉴权失败（调用方要区分，
        否则「全部判 keep」会把「全部 401」伪装成一次成功的保守裁决）。
        """
        try:
            prompt = self._CONSOLIDATE_PROMPT.replace("{sim}", f"{sim:.3f}")
            user = (f"A（较早，{older['created_at']}）：{older['content']}\n"
                    f"B（较新，{newer['created_at']}）：{newer['content']}")
            text = self._distill_llm_chat([
                {"role": "system", "content": prompt},
                {"role": "user", "content": user}])
        except Exception as exc:  # noqa: BLE001
            logger.warning("atbrains: 合并裁决 LLM 调用失败，保留原样: %s", str(exc)[:200])
            return None
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return ""
        try:
            data = json.loads(text[start:end + 1])
        except (TypeError, ValueError):
            return ""
        if str(data.get("action") or "").strip().lower() != "merge":
            return ""
        merged = str(data.get("content") or "").strip()
        return merged if len(merged) >= 8 else ""

    def consolidate(self, *, dry_run: bool = True, low: float = 0.82, high: float = 0.90,
                    window_hours: Optional[float] = 24, limit: int = 200,
                    use_llm: bool = False) -> dict[str, Any]:
        """近重复合并：≥high 直接取代（确定性），[low,high) 交 LLM 裁决是否真合并。

        只处理 active 且未被取代的行，同 memory_type 内两两比 Qdrant 已有向量（不重新 embed）；
        只比较 created_at 相差 window_hours 以内的对（默认 24h）——目标是收敛「同一次整理里
        写出来的近重复」，不动跨月的老条目。LLM 说 keep 就原样保留（不删、不取代）。
        """
        self.ensure_schema()
        try:
            lo, hi = float(low), float(high)
        except (TypeError, ValueError):
            lo, hi = 0.82, 0.90
        if lo > hi:
            lo, hi = hi, lo
        win_sec = float(window_hours) * 3600.0 if window_hours else 0.0
        capped_limit = max(2, min(500, int(limit)))
        rows = [dict(r) for r in self._exec(
            "SELECT id, kind, memory_type, content, importance, created_at FROM brain_memories "
            "WHERE active = 1 AND superseded_by IS NULL ORDER BY created_at DESC, id DESC LIMIT %s",
            (capped_limit,), fetch=True)]
        out: dict[str, Any] = {"dry_run": bool(dry_run), "scanned": len(rows), "low": lo, "high": hi,
                               "window_hours": window_hours, "use_llm": bool(use_llm),
                               "auto": [], "review": [], "superseded": 0, "merged": 0,
                               "kept": 0, "llm_failed": 0, "skipped": 0, "error": ""}
        if len(rows) < 2:
            return out
        try:
            vectors = self.qdrant_get_vectors([int(r["id"]) for r in rows])
        except Exception as exc:  # noqa: BLE001
            out["error"] = str(exc)[:200]
            out["skipped"] = len(rows)
            return out
        pairs: list[tuple[float, dict, dict]] = []
        for i in range(len(rows) - 1):
            ri = rows[i]
            vi = vectors.get(int(ri["id"]))
            if not vi:
                out["skipped"] += 1
                continue
            for j in range(i + 1, len(rows)):
                rj = rows[j]
                vj = vectors.get(int(rj["id"]))
                if not vj:
                    continue
                if str(ri.get("memory_type") or "") != str(rj.get("memory_type") or ""):
                    continue
                if win_sec > 0:
                    gap = abs((ri["created_at"] - rj["created_at"]).total_seconds())
                    if gap > win_sec:
                        continue
                sim = _cosine(vi, vj)
                if sim < lo:
                    continue
                older, newer = ((ri, rj)
                                if (ri["created_at"], int(ri["id"])) < (rj["created_at"], int(rj["id"]))
                                else (rj, ri))
                pairs.append((sim, older, newer))
        pairs.sort(key=lambda p: -p[0])
        marked: set[int] = set()
        for sim, older, newer in pairs:
            old_id, new_id = int(older["id"]), int(newer["id"])
            if old_id in marked or new_id in marked:
                continue
            rec = {"sim": round(sim, 4), "old_id": old_id, "new_id": new_id,
                   "old_head": str(older["content"])[:70], "new_head": str(newer["content"])[:70]}
            if sim >= hi:
                out["auto"].append(rec)
                if not dry_run:
                    self._exec(
                        "UPDATE brain_memories SET superseded_by = %s, superseded_at = UTC_TIMESTAMP(), "
                        "updated_at = updated_at WHERE id = %s AND active = 1", (new_id, old_id))
                    out["superseded"] += 1
                    marked.add(old_id)
                continue
            if not (use_llm and not dry_run):
                out["review"].append(rec)
                continue
            merged_text = self._merge_pair(older, newer, sim)
            if merged_text is None:
                out["review"].append(rec)
                out["llm_failed"] += 1
                continue
            if not merged_text:
                out["review"].append(rec)
                out["kept"] += 1
                continue
            try:
                saved = self.remember(merged_text, kind=str(newer.get("kind") or "note"),
                                      tags="merge", importance=max(int(older.get("importance") or 5),
                                                                   int(newer.get("importance") or 5)),
                                      source="merge")
            except Exception as exc:  # noqa: BLE001
                logger.warning("atbrains: 合并写入失败，保留原样: %s", str(exc)[:200])
                out["review"].append(rec)
                continue
            mid = saved.get("id")
            if not mid:
                out["review"].append(rec)
                continue
            self._exec(
                "UPDATE brain_memories SET superseded_by = %s, superseded_at = UTC_TIMESTAMP(), "
                "updated_at = updated_at WHERE id IN (%s, %s) AND active = 1", (int(mid), old_id, new_id))
            rec["merged_id"] = int(mid)
            out["review"].append(rec)
            out["merged"] += 1
            marked.add(old_id)
            marked.add(new_id)
        return out
