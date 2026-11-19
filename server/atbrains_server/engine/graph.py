"""图谱层：实体 / 关系 / 提及抽取与第三路召回。"""
from __future__ import annotations

import logging
from typing import Any
from typing import Optional
from typing import cast
import json
import time

from ..util import (  # noqa: F401
    _esc_like,
    search_terms,
)

logger = logging.getLogger("atbrains")

class GraphMixin:
    """图谱层：实体 / 关系 / 提及抽取与第三路召回。"""

    _GRAPH_PROMPT = (
        "你是知识图谱抽取器。输入是一批长期记忆（每条带 id 与正文）。\n"
        "抽取实体与它们之间的关系。\n"
        "实体名要求：稳定、可复用的短名（≤24 字），不要带修饰语；服务器用主机名或 IP，"
        "项目用项目名，服务用服务名（如 MariaDB / Redis / Qdrant / Hermes / AstrBot）。\n"
        "关系只允许四种：runs_on（A 运行在 B 上）/ uses（A 使用 B）/ part_of（A 属于 B）/ related（其它关联）。\n"
        "已有实体（优先复用这些名字，不要造同义新名）：{known}\n"
        "宁可少抽取，也不要编造。没有可抽取的就输出空数组。\n"
        "只输出严格 JSON，无多余文字："
        '{"entities":[{"name":"...","kind":"server|project|service|person|device|topic"}],'
        '"relations":[{"src":"...","dst":"...","relation":"runs_on|uses|part_of|related"}]}'
    )

    _GRAPH_STOP = {"user", "用户", "助手", "系统", "管理员", "assistant", "system", "unknown", "unspecified"}

    @classmethod
    def _graph_entity_ok(cls, name: str) -> bool:
        """实体名过滤：太短、以标点开头、或纯泛称（用户/助手/系统）都不收。"""
        n = (name or "").strip()
        if len(n) < 2 or len(n) > 60:
            return False
        if n[0] in ".-_/#*·,，。":
            return False
        if n.lower() in cls._GRAPH_STOP:
            return False
        return True

    def graph_extract(self, *, dry_run: bool = True, limit: int = 200,
                      batch: int = 6, rebuild: bool = False,
                      after_id: Optional[int] = None) -> dict[str, Any]:
        """用 LLM 从记忆里抽实体与关系（离线批处理，不参与在线召回）。

        mentions 不用 LLM 给的 id，而用「实体名出现在正文里」做确定性归属；
        entities 走 name 唯一键幂等 upsert，edges 走 (src,dst,relation) 唯一键。

        after_id: 只处理 id 更大的记忆（增量游标）。默认 None 时按「还没有提及记录」选，
        这样人工 graph-build 可以无限次补齐存量；增量维护走 after_id，避免无实体可抽的
        记忆被反复送进 LLM 花冤枉钱。
        """
        self.ensure_schema()
        out: dict[str, Any] = {"dry_run": bool(dry_run), "memories": 0, "batches": 0,
                               "entities": 0, "edges": 0, "mentions": 0,
                               "llm_failed": 0, "error": "", "max_id": 0}
        if not int(self.cfg.get("graph_enabled") or 0):
            out["error"] = "graph_disabled"
            return out
        if rebuild:
            rows = cast(list, self._exec(
                "SELECT id, content FROM brain_memories WHERE active = 1 ORDER BY id LIMIT %s",
                (max(1, int(limit)),), fetch=True))
        elif after_id is not None:
            rows = cast(list, self._exec(
                "SELECT id, content FROM brain_memories WHERE active = 1 AND id > %s "
                "ORDER BY id LIMIT %s",
                (int(after_id), max(1, int(limit))), fetch=True))
        else:
            rows = cast(list, self._exec(
                "SELECT m.id, m.content FROM brain_memories m "
                "LEFT JOIN brain_entity_mentions e ON e.memory_id = m.id "
                "WHERE m.active = 1 AND e.memory_id IS NULL ORDER BY m.id LIMIT %s",
                (max(1, int(limit)),), fetch=True))
        out["memories"] = len(rows)
        if rows:
            out["max_id"] = max(int(r["id"]) for r in rows)
        if not rows:
            return out
        known = [str(r["name"]) for r in self._exec(
            "SELECT name FROM brain_entities ORDER BY mention_count DESC LIMIT 40", fetch=True)]
        known_txt = "、".join(known) if known else "（暂无）"
        ents: dict[str, str] = {}
        edges: set[tuple[str, str, str]] = set()
        mentions: set[tuple[str, int]] = set()
        step = max(1, int(batch))
        for i in range(0, len(rows), step):
            chunk = rows[i:i + step]
            user = "记忆：\n" + "\n".join(
                f"id={r['id']} | {str(r['content'])[:400]}" for r in chunk)
            try:
                text = self._distill_llm_chat([
                    {"role": "system", "content": self._GRAPH_PROMPT.replace("{known}", known_txt)},
                    {"role": "user", "content": user}])
                start, end = text.find("{"), text.rfind("}")
                data = json.loads(text[start:end + 1]) if start >= 0 and end > start else {}
            except Exception as exc:  # noqa: BLE001
                out["llm_failed"] += 1
                logger.warning("atbrains: 图谱抽取失败（跳过该批）: %s", str(exc)[:200])
                continue
            out["batches"] += 1
            names: list[str] = []
            for e in data.get("entities") or []:
                if not isinstance(e, dict):
                    continue
                name = str(e.get("name") or "").strip()[:191]
                if not self._graph_entity_ok(name):
                    continue
                ents.setdefault(name, str(e.get("kind") or "topic").strip()[:32] or "topic")
                names.append(name)
            for rel in data.get("relations") or []:
                if not isinstance(rel, dict):
                    continue
                src = str(rel.get("src") or "").strip()[:191]
                dst = str(rel.get("dst") or "").strip()[:191]
                kind = str(rel.get("relation") or "related").strip().lower()
                if not src or not dst or src == dst:
                    continue
                if not (self._graph_entity_ok(src) and self._graph_entity_ok(dst)):
                    continue
                if kind not in ("runs_on", "uses", "part_of", "related"):
                    kind = "related"
                edges.add((src, dst, kind))
                ents.setdefault(src, "topic")
                ents.setdefault(dst, "topic")
            for r in chunk:
                low = str(r["content"]).lower()
                for name in names:
                    if name.lower() in low:
                        mentions.add((name, int(r["id"])))
        out["entities"] = len(ents)
        out["edges"] = len(edges)
        out["mentions"] = len(mentions)
        if dry_run:
            out["sample"] = sorted(ents)[:15]
            return out
        for name, kind in ents.items():
            self._exec(
                "INSERT INTO brain_entities (name, kind, machine, agent, mention_count) "
                "VALUES (%s, %s, %s, %s, 0) "
                "ON DUPLICATE KEY UPDATE kind = IF(kind = '', VALUES(kind), kind), updated_at = updated_at",
                (name, kind, self.machine[:48], self.agent))
        id_map: dict[str, int] = {}
        names = sorted(ents)
        for i in range(0, len(names), 200):
            part = names[i:i + 200]
            marks = ", ".join(["%s"] * len(part))
            for r in self._exec(f"SELECT id, name FROM brain_entities WHERE name IN ({marks})",
                                tuple(part), fetch=True):
                id_map[str(r["name"])] = int(r["id"])
        for name, mem_id in mentions:
            eid = id_map.get(name)
            if not eid:
                continue
            self._exec("INSERT IGNORE INTO brain_entity_mentions (entity_id, memory_id) VALUES (%s, %s)",
                       (eid, mem_id))
        for src, dst, kind in edges:
            sid, did = id_map.get(src), id_map.get(dst)
            if not sid or not did or sid == did:
                continue
            self._exec(
                "INSERT INTO brain_edges (src_id, dst_id, relation, weight, machine, agent) "
                "VALUES (%s, %s, %s, 1.000, %s, %s) "
                "ON DUPLICATE KEY UPDATE weight = GREATEST(weight, VALUES(weight))",
                (sid, did, kind, self.machine[:48], self.agent))
        eids = sorted(set(id_map.values()))
        for i in range(0, len(eids), 200):
            chunk_ids = eids[i:i + 200]
            marks = ", ".join(["%s"] * len(chunk_ids))
            self._exec(
                f"UPDATE brain_entities e "
                f"JOIN (SELECT entity_id, COUNT(*) AS c FROM brain_entity_mentions "
                f"WHERE entity_id IN ({marks}) GROUP BY entity_id) m ON m.entity_id = e.id "
                f"SET e.mention_count = m.c, e.updated_at = e.updated_at",
                tuple(chunk_ids),
            )
        self._invalidate_caches(bump_qver=False)
        return out

    def graph_search_memories(self, query: str, *, limit: int = 12) -> list[dict[str, Any]]:
        """查询期图谱路：实体名匹配 → 直接提及的记忆 + 1 跳邻居提及的记忆（不调 LLM）。"""
        if not int(self.cfg.get("graph_enabled") or 0) or not (query or "").strip():
            return []
        terms = search_terms(query, max_terms=4) or [str(query)]
        if not terms:
            return []
        clauses, params = [], []
        for t in terms:
            clauses.append("name LIKE %s")
            params.append(f"%{_esc_like(t)}%")
        ents = self._exec(
            "SELECT id, name FROM brain_entities WHERE " + " OR ".join(clauses) + " LIMIT 20",
            tuple(params), fetch=True)
        if not ents:
            return []
        direct = [int(e["id"]) for e in ents]
        marks = ", ".join(["%s"] * len(direct))
        neigh_rows = self._exec(
            f"SELECT DISTINCT dst_id AS id FROM brain_edges WHERE src_id IN ({marks}) "
            f"UNION SELECT DISTINCT src_id AS id FROM brain_edges WHERE dst_id IN ({marks})",
            tuple(direct + direct), fetch=True)
        neigh = [int(r["id"]) for r in neigh_rows if int(r["id"]) not in direct]
        scored: dict[int, float] = {}

        def add_memories(entity_ids: list[int], weight: float) -> None:
            if not entity_ids:
                return
            mk = ", ".join(["%s"] * len(entity_ids))
            for r in self._exec(
                    f"SELECT DISTINCT memory_id FROM brain_entity_mentions WHERE entity_id IN ({mk})",
                    tuple(entity_ids), fetch=True):
                mid = int(r["memory_id"])
                scored[mid] = max(scored.get(mid, 0.0), weight)

        add_memories(direct, 1.0)
        add_memories(neigh[:40], 0.5)
        if not scored:
            return []
        top = sorted(scored.items(), key=lambda kv: (-kv[1], -kv[0]))[: max(1, int(limit))]
        ids = [mid for mid, _ in top]
        mk2 = ", ".join(["%s"] * len(ids))
        rows = self._exec(
            "SELECT id, kind, memory_type, content, tags, importance, source, session_id, machine, agent, "
            "created_at, updated_at, access_count, last_accessed_at, expires_at "
            f"FROM brain_memories WHERE active = 1 AND superseded_by IS NULL AND id IN ({mk2})",
            tuple(ids), fetch=True)
        by_id = {int(r["id"]): dict(r) for r in rows}
        out: list[dict[str, Any]] = []
        for mid, sc in top:
            row = by_id.get(mid)
            if not row:
                continue
            row["gscore"] = round(sc, 3)
            row["via"] = "graph"
            out.append(row)
        return out

    def graph_status(self) -> dict[str, Any]:
        self.ensure_schema()
        row = self._exec(
            "SELECT (SELECT COUNT(*) FROM brain_entities) AS entities, "
            "(SELECT COUNT(*) FROM brain_edges) AS edges, "
            "(SELECT COUNT(*) FROM brain_entity_mentions) AS mentions, "
            "(SELECT COUNT(DISTINCT memory_id) FROM brain_entity_mentions) AS covered, "
            "(SELECT COUNT(*) FROM brain_memories WHERE active = 1) AS active", fetch=True)[0]
        top = self._exec(
            "SELECT name, kind, mention_count FROM brain_entities "
            "ORDER BY mention_count DESC, id ASC LIMIT 12", fetch=True)
        out: dict[str, Any] = {"enabled": bool(int(self.cfg.get("graph_enabled") or 0)),
                               "route_weight": float(self.cfg.get("graph_route_weight") or 0)}
        out.update({k: int(v or 0) for k, v in row.items()})
        active = int(out.get("active") or 0)
        out["coverage"] = round(100.0 * int(out.get("covered") or 0) / active, 1) if active else 0.0
        out["auto"] = {"enabled": bool(int(self.cfg.get("graph_auto_enabled") or 0)),
                       "interval_min": float(self.cfg.get("graph_auto_interval_min") or 0),
                       "limit": int(self.cfg.get("graph_auto_limit") or 0)}
        cur = self._meta_get("graph_cursor")
        out["cursor"] = int(cur) if cur and str(cur).isdigit() else None
        out["pending"] = self._graph_pending()
        out["last_build"] = self._meta_get("last_graph_build")
        out["top"] = [dict(r) for r in top]
        return out

    def vector_status(self, *, force: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        if not force and self._vector_cache and now - self._vector_cache[0] < self._cache_ttl:
            return self._vector_cache[1]
        key_src = str(self.cfg.get("qdrant_config_file") or "").strip()
        out: dict[str, Any] = {
            "enabled": bool(int(self.cfg.get("vector_enabled") or 0)),
            "embed_model": str(self.cfg.get("embed_model")),
            "embed_dim": int(self.cfg.get("embed_dim") or 0),
            "qdrant": f"{self.cfg.get('qdrant_url')} / {self.cfg.get('qdrant_collection')}",
            "key_source": ("env/config" if (self.cfg.get("qdrant_api_key") or self.cfg.get("embed_api_key"))
                           else (f"yaml:{key_src}" if key_src else "auto-detect")),
        }
        sec = self._vector_secrets()
        out["keys_found"] = {"embed": bool(sec.get("embed")), "qdrant": bool(sec.get("qdrant"))}
        self.ensure_schema()
        row = self._exec(
            "SELECT "
            "(SELECT COUNT(*) FROM brain_memories WHERE active = 1 AND superseded_by IS NULL) AS active, "
            "(SELECT COUNT(*) FROM brain_vector_index v JOIN brain_memories m ON m.id = v.mem_id "
            " WHERE m.active = 1 AND m.superseded_by IS NULL AND v.status = 'ok') AS ok, "
            "(SELECT COUNT(*) FROM brain_vector_index v JOIN brain_memories m ON m.id = v.mem_id "
            " WHERE m.active = 1 AND m.superseded_by IS NULL AND v.status = 'error') AS err, "
            "(SELECT COUNT(*) FROM brain_memories m LEFT JOIN brain_vector_index v "
            " ON v.mem_id = m.id WHERE m.active = 1 AND m.superseded_by IS NULL AND v.mem_id IS NULL) AS missing, "
            "(SELECT COUNT(*) FROM brain_vector_index v JOIN brain_memories m ON m.id = v.mem_id "
            " WHERE m.active = 1 AND m.superseded_by IS NULL AND v.status = 'ok' AND (v.model <> %s OR v.content_hash IS NULL "
            " OR v.content_hash <> m.content_hash)) AS stale, "
            "(SELECT COUNT(*) FROM brain_memories WHERE active = 1 AND superseded_by IS NOT NULL) AS superseded, "
            "(SELECT MAX(embedded_at) FROM brain_vector_index WHERE status = 'ok') AS last_ok",
            (str(self.cfg.get("embed_model"))[:64],), fetch=True)[0]
        out["memories"] = {k: (v if k == "last_ok" else int(v or 0)) for k, v in row.items()}
        try:
            data = self._qdrant("GET", f"/collections/{self.cfg['qdrant_collection']}", timeout=8)
            res = data.get("result") or {}
            out["qdrant_state"] = {"status": res.get("status"), "points": res.get("points_count")}
        except Exception as exc:  # noqa: BLE001
            out["qdrant_state"] = {"error": str(exc)[:200]}
        self._vector_cache = (now, out)
        return out
