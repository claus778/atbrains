"""结论蒸馏：把对话轮次交给 LLM 提炼成长期结论。"""
from __future__ import annotations

import logging
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from typing import Optional
import json
import re
import threading
import time

from ..util import (  # noqa: F401
    BrainError,
    BrainHTTPError,
    _clip,
)

logger = logging.getLogger("atbrains")

class DistillMixin:
    """结论蒸馏：把对话轮次交给 LLM 提炼成长期结论。"""

    _DISTILL_SYSTEM_PROMPT = (
        "你是长期记忆蒸馏器。输入是一批对话轮次（含 id、时间、来源）与当前 UTC 时间。\n"
        "提取“跨周、跨月仍然成立”的结论：事实、偏好、决策、目标、稳定的行为模式"
        "（归纳类 kind=insight）、进行中项目的稳定状态。\n"
        "排除：一次性任务细节、工具输出、寒暄、临时状态；"
        "凭证/密钥/口令/token 一律禁止出现在输出里；宁少而准。\n"
        "每条结论必须自足可读（脱离上下文也能看懂）。\n"
        "importance 分档（必须拉开差距，禁止一律写 7）："
        "9-10=用户明确的偏好/硬约束/不可违反的约定；7-8=架构决策、已定方案、关键事实；"
        "5-6=一般背景事实；3-4=一次性过程记录。\n"
        "结构化槽位（可选）：若结论是一个「会变化的可核对值」——端口 / 版本 / 路径 / 账号 / "
        "开关 / 配置项 / 表结构版本这类——额外给出 slot：subject 用稳定名词（服务器 / 项目名 / "
        "设备名），predicate 用不超过 12 字的短谓词（如 SSH 端口、记忆库表结构版本），"
        "value 只放最新值本身。偏好、决策、洞察、一次性记录不要给 slot。\n"
        "例：content「服务器 SSH 端口已从 22 迁移到 7622」→ "
        "slot {\"subject\":\"服务器\",\"predicate\":\"SSH 端口\",\"value\":\"7622\"}；"
        "content「记忆库 schema 已升级到 v10」→ "
        "slot {\"subject\":\"atbrains 记忆库\",\"predicate\":\"schema 版本\",\"value\":\"v10\"}。\n"
        "只输出严格 JSON 数组，不要任何多余文字：\n"
        "[{\"content\":\"...\",\"kind\":\"fact|preference|decision|insight\","
        "\"importance\":1-10,\"tags\":[\"≤3个\"],\"evidence_turn_ids\":[...],"
        "\"slot\":{\"subject\":\"...\",\"predicate\":\"...\",\"value\":\"...\"}}]\n"
        "最多 {max_items} 条；没有可提炼的结论就输出 []。"
    )

    _DISTILL_RECONCILE_PROMPT = (
        "你是记忆去重裁决器。输入是一批候选结论，每条带 index、content 与相似邻居列表"
        "（邻居含 id、余弦相似度 sim、内容）。逐条裁决：\n"
        "  * skip：邻居已表达同一结论且没有新增信息；\n"
        "  * update：同一条事实/偏好出现了更新版本，需由新结论取代旧记忆"
        "（target_id 必须取自该候选的邻居 id）；\n"
        "  * new：与所有邻居都不是同一结论。\n"
        "target_id 只能来自该候选自己的邻居列表，否则按 new。\n"
        "只输出严格 JSON，无多余文字："
        "{\"decisions\":[{\"index\":0,\"decision\":\"skip|new|update\",\"target_id\":0}]}"
    )

    # 写入双保险：LLM 已按提示词排除凭证，落库前再兜底过滤一次。
    _DISTILL_SENSITIVE_RE = re.compile(
        r'(sk-[A-Za-z0-9_\-]{8,}|Bearer\s+[A-Za-z0-9._\-]{16,}|'
        r'(?i:(password|passwd|secret|token|密码|密钥|口令)\s*[:=：]\s*\S{6,}))')

    def _meta_incr(self, key: str, amount: int = 1) -> None:
        """brain_meta 计数自增（缺失即从 amount 开始；并发安全）。"""
        self.ensure_schema()
        self._exec(
            "INSERT INTO brain_meta (meta_key, meta_value) VALUES (%s, %s) "
            "ON DUPLICATE KEY UPDATE meta_value = CAST(meta_value AS UNSIGNED) + %s",
            (key, str(int(amount)), int(amount)))

    def start_distill(self) -> None:
        """惰性启动蒸馏线程（每进程一个；daemon，绝不阻塞进程退出）。"""
        if not int(self.cfg.get("distill_enabled") or 0):
            return
        with self._distill_lock:
            if self._distill_started:
                return
            self._distill_started = True
        try:
            threading.Thread(target=self._distill_loop,
                             name="brain-distill", daemon=True).start()
        except Exception as exc:  # noqa: BLE001
            with self._distill_lock:
                self._distill_started = False
            logger.warning("atbrains: 启动蒸馏线程失败: %s", exc)

    def _distill_loop(self) -> None:
        interval = max(0.01, float(self.cfg.get("distill_interval_min") or 60)) * 60.0
        delay = min(interval, 60.0)   # 首轮稍后开始，不抢占启动路径
        while True:
            try:
                time.sleep(delay)
                self._distill_tick()
            except Exception as exc:  # noqa: BLE001
                logger.warning("atbrains: 蒸馏循环异常（继续下一轮）: %s", exc)
            delay = interval

    def distill_status(self) -> dict[str, Any]:
        """蒸馏状态：开关 / 水位 / 待处理轮次 / 上次时间 / 五项计数 / API 配置（key 只报存在与否）。"""
        wm_txt = self._meta_get("distill_watermark")
        try:
            wm = int(wm_txt) if wm_txt not in (None, "") else None
        except (TypeError, ValueError):
            wm = None
        pending = 0
        if wm is not None:
            row = self._exec(
                "SELECT COUNT(*) AS c FROM brain_turns WHERE id > %s", (wm,), fetch=True)[0]
            pending = int(row["c"] or 0)
        counters: dict[str, int] = {}
        for key in ("distill_runs", "distill_added", "distill_updated",
                    "distill_skipped", "distill_errors"):
            try:
                counters[key] = int(self._meta_get(key) or 0)
            except (TypeError, ValueError):
                counters[key] = 0
        return {
            "enabled": bool(int(self.cfg.get("distill_enabled") or 0)),
            "interval_min": int(self.cfg.get("distill_interval_min") or 60),
            "watermark": wm,
            "pending": pending,
            "last_distill": self._meta_get("last_distill"),
            "counters": counters,
            "api_base_url": str(self.cfg.get("distill_api_base_url") or ""),
            "api_model": str(self.cfg.get("distill_api_model") or ""),
            "api_key_present": bool(str(self.cfg.get("distill_api_key") or "").strip()),
        }

    def _distill_tick(self, force: bool = False, turns_override: int = 0,
                      dry_run: bool = False) -> dict[str, Any]:
        """一轮蒸馏。force 无视开关（手动命令用）；dry_run 不取锁、不写库、水位不动。"""
        result: dict[str, Any] = {
            "ok": True, "reason": "", "dry_run": bool(dry_run),
            "turns": [], "items": [], "added": [], "updated": [],
            "skipped": 0, "errors": 0, "watermark": None,
        }
        if not force and not int(self.cfg.get("distill_enabled") or 0):
            result["reason"] = "disabled"
            logger.debug("atbrains: 蒸馏未启用，跳过")
            return result
        self.ensure_schema()
        cfg = self.cfg
        max_turns = max(1, int(cfg.get("distill_max_turns") or 16))
        turn_chars = max(100, int(cfg.get("distill_turn_chars") or 1000))
        override = max(0, int(turns_override or 0))

        # 1) 水位：首次启用只记当前最大 id（不回灌历史），绝不拿历史对话突然烧 API。
        wm_txt = self._meta_get("distill_watermark")
        try:
            wm = int(wm_txt) if wm_txt not in (None, "") else 0
        except (TypeError, ValueError):
            wm = 0
        if not override and wm_txt in (None, ""):
            if dry_run:
                logger.info("atbrains: 蒸馏水位未初始化，dry-run 按 0 计（不写库）")
            else:
                row = self._exec(
                    "SELECT COALESCE(MAX(id), 0) AS m FROM brain_turns", fetch=True)[0]
                wm = int(row["m"] or 0)
                self._meta_set("distill_watermark", str(wm))
                logger.info("atbrains: 蒸馏水位初始化 id=%s（不回灌历史）", wm)
                result["reason"] = "initialized"
                result["watermark"] = wm
                return result
        result["watermark"] = wm
        now = datetime.now(timezone.utc)

        # 2) 取批：override 取最近 N 轮（升序）；否则按水位 + 静默窗口/积压门槛。
        capacity = max_turns
        if override:
            raw = self._exec(
                "SELECT id, session_id, machine, platform, user_content, assistant_content, ts "
                "FROM brain_turns ORDER BY id DESC LIMIT %s", (override,), fetch=True)
            raw = list(reversed(raw))
            capacity = override
        else:
            cutoff = (now - timedelta(seconds=120)).strftime("%Y-%m-%d %H:%M:%S")
            stat = self._exec(
                "SELECT COUNT(*) AS c, MIN(ts) AS oldest FROM brain_turns "
                "WHERE id > %s AND ts < %s", (wm, cutoff), fetch=True)[0]
            count = int(stat["c"] or 0)
            if count <= 0:
                result["reason"] = "no_turns"
                return result
            min_turns = max(1, int(cfg.get("distill_min_turns") or 8))
            if count < min_turns:
                oldest = stat.get("oldest")
                stalled = False
                if oldest is not None:
                    try:
                        age_h = (now - oldest.replace(tzinfo=timezone.utc)).total_seconds() / 3600.0
                        stalled = age_h >= float(cfg.get("distill_stagnation_hours") or 24)
                    except (TypeError, ValueError):
                        stalled = False
                if not stalled:
                    result["reason"] = "below_threshold"
                    return result
            limit = max_turns * 2 + 10
            raw = self._exec(
                "SELECT id, session_id, machine, platform, user_content, assistant_content, ts "
                "FROM brain_turns WHERE id > %s AND ts < %s ORDER BY id LIMIT %s",
                (wm, cutoff, limit), fetch=True)

        rows: list[dict[str, Any]] = []
        for r in raw:
            user = str(r.get("user_content") or "")
            if not user.strip() or user.startswith("[IMPORTANT"):
                continue
            rows.append({
                "id": int(r["id"]),
                "session_id": str(r.get("session_id") or ""),
                "machine": str(r.get("machine") or ""),
                "platform": str(r.get("platform") or ""),
                "ts": r.get("ts"),
                "user_content": _clip(user, turn_chars),
                "assistant_content": _clip(str(r.get("assistant_content") or ""), turn_chars),
            })
            if len(rows) >= capacity:
                break
        result["turns"] = [r["id"] for r in rows]
        if not rows:
            result["reason"] = "no_turns"
            return result

        # 3) dry-run：不取锁、不写库、水位不动，但仍然跑真实提取 + 对账。
        lock_held = False
        if not dry_run:
            got = self._exec("SELECT GET_LOCK('brain_distill', 0) AS got", fetch=True)
            if not (got and got[0].get("got")):
                logger.debug("atbrains: 蒸馏跳过（另一进程正在蒸馏）")
                result["reason"] = "locked"
                return result
            lock_held = True
        try:
            try:
                candidates = self._distill_extract(rows)
            except Exception as exc:  # noqa: BLE001
                result["ok"] = False
                result["reason"] = "extract_failed"
                result["errors"] = 1
                result["error"] = str(exc)[:300]
                if not dry_run:
                    self._meta_incr("distill_errors", 1)
                logger.warning("atbrains: 蒸馏提取失败（不推水位）: %s", str(exc)[:200])
                return result

            items = self._distill_candidates(candidates, dry_run=dry_run)
            result["items"] = items
            result["skipped"] = sum(1 for it in items if it["decision"] == "skip")
            if dry_run:
                result["reason"] = "dry_run"
                return result

            added_ids = [int(it["id"]) for it in items
                         if it["decision"] in ("new", "update") and it.get("id")]
            updated_ids = [int(it["id"]) for it in items
                           if it["decision"] == "update" and it.get("id")]
            batch_max = max(r["id"] for r in rows)
            # 水位推进：无论本轮写入 0 条还是 N 条都推进，避免同一批反复烧 API。
            # 例外：手动 --now 取的是「最近 N 轮」，如果它与旧水位之间还有积压轮次，
            # 直接推到 batch_max 会让这些轮次永远不再被自动蒸馏；此时保持原水位。
            # 另外绝不把水位往回推（其它进程可能已经推到更后面）。
            advance = batch_max > wm
            if advance and override:
                first_processed = int(raw[0]["id"]) if raw else batch_max
                gap = self._exec(
                    "SELECT COUNT(*) AS c FROM brain_turns WHERE id > %s AND id < %s",
                    (wm, first_processed), fetch=True)[0]
                advance = not int(gap["c"] or 0)
            if advance:
                self._meta_set("distill_watermark", str(batch_max))
            else:
                logger.info("atbrains: 手动蒸馏批次之前仍有积压轮次，水位保持 %s（不跳到 %s）",
                            wm, batch_max)
            self._meta_set("last_distill", now.strftime("%Y-%m-%d %H:%M:%S"))
            self._meta_incr("distill_runs", 1)
            if added_ids:
                self._meta_incr("distill_added", len(added_ids))
            if updated_ids:
                self._meta_incr("distill_updated", len(updated_ids))
            if result["skipped"]:
                self._meta_incr("distill_skipped", result["skipped"])
            result["watermark"] = batch_max if advance else wm
            result["added"] = added_ids
            result["updated"] = updated_ids
            logger.info("atbrains: 蒸馏完成 新增=%s 更新=%s 跳过=%s (批 %s 轮, id %s-%s)",
                        len(added_ids), len(updated_ids), result["skipped"],
                        len(rows), rows[0]["id"], rows[-1]["id"])
            return result
        finally:
            if lock_held:
                try:
                    self._exec("SELECT RELEASE_LOCK('brain_distill')")
                except Exception:  # noqa: BLE001
                    pass

    def _distill_extract(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """调用 LLM 从一批轮次中提取结论；任何解析失败都抛 BrainError（调用方只计数不推水位）。"""
        max_items = max(1, int(self.cfg.get("distill_max_items") or 6))
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        lines = [f"当前 UTC 时间: {now}", ""]
        for r in rows:
            ts = r.get("ts")
            ts_txt = ts.strftime("%Y-%m-%d %H:%M:%S") if hasattr(ts, "strftime") else str(ts or "?")
            src = "/".join(x for x in (r.get("machine"), r.get("platform")) if x) or "?"
            lines.append(f"--- 轮次 id={r['id']} 时间={ts_txt} 来源={src}")
            lines.append(f"用户: {r.get('user_content') or ''}")
            lines.append(f"助手: {r.get('assistant_content') or ''}")
        text = self._distill_llm_chat([
            {"role": "system",
             "content": self._DISTILL_SYSTEM_PROMPT.replace("{max_items}", str(max_items))},
            {"role": "user", "content": "\n".join(lines)},
        ])
        raw = text.strip()
        if raw.startswith("```"):                     # 剥 ```json 围栏
            first_nl = raw.find("\n")
            raw = raw[first_nl + 1:] if first_nl >= 0 else ""
            if raw.rstrip().endswith("```"):
                raw = raw.rstrip()[:-3]
        start, end = raw.find("["), raw.rfind("]")
        if start < 0 or end <= start:
            raise BrainError("蒸馏输出未找到 JSON 数组")
        try:
            data = json.loads(raw[start:end + 1])
        except Exception as exc:  # noqa: BLE001
            raise BrainError(f"蒸馏输出 JSON 解析失败: {str(exc)[:160]}") from exc
        if not isinstance(data, list):
            raise BrainError("蒸馏输出不是 JSON 数组")
        out: list[dict[str, Any]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            kind = str(item.get("kind") or "fact").strip()
            if kind not in ("fact", "preference", "decision", "insight"):
                kind = "fact"
            try:
                imp = int(item.get("importance") or 5)
            except (TypeError, ValueError):
                imp = 5
            tags_raw = item.get("tags")
            tags: list[str] = []
            if isinstance(tags_raw, list):
                tags = [str(t).strip() for t in tags_raw if str(t).strip()]
            elif isinstance(tags_raw, str):
                tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
            out.append({
                "content": content, "kind": kind, "importance": imp,
                "tags": ",".join(tags[:3])[:255],
                "evidence_turn_ids": item.get("evidence_turn_ids") or [],
            })
            if len(out) >= max_items:
                break
        return out

    def _distill_llm_chat(self, messages: list[dict[str, str]]) -> str:
        """POST {base}/chat/completions；必须复用 _http_json（默认 UA 是 CommandCode 前置的刚需）。

        key 支持多候选：`distill_api_key` + `distill_api_key_fallbacks`（逗号分隔的环境变量名）。
        401/403 视为「这把 key 废了」→ 自动换下一把；其它错误直接抛。历史教训：CommandCode 轮换
        key 后主变量会被留成死值，而系统里往往还有一把能用的（2026-10-29 实测主键 401、备用键 200），
        没有回退就只能眼睁睁看着蒸馏持续失败。
        """
        cfg = self.cfg
        names = [str(cfg.get("distill_api_key") or "").strip()]
        names += [x.strip() for x in str(cfg.get("distill_api_key_fallbacks") or "").split(",") if x.strip()]
        keys = list(dict.fromkeys(n for n in names if n))
        if not keys:
            raise BrainError("蒸馏 API key 缺失（请设置 ATBRAINS_DISTILL_API_KEY 或 OPENAI_API_KEY）")
        url = str(cfg.get("distill_api_base_url") or "").rstrip("/") + "/chat/completions"
        payload = {
            "model": str(cfg.get("distill_api_model") or ""),
            "messages": messages,
            "temperature": 0.2,
            # 该模型是推理模型：reasoning_tokens 也计入 max_tokens，1500 会把 JSON 截断，
            # 因此留足预算（输出仍受 distill_max_items 约束，成本可控）。
            "max_tokens": int(cfg.get("distill_api_max_tokens") or 8000),
        }
        status, data, used = 0, {}, ""
        last_exc: Optional[BaseException] = None
        for name in keys:
            try:
                status, data = self._http_json(
                    "POST", url, headers={"Authorization": f"Bearer {name}"}, payload=payload,
                    timeout=float(cfg.get("distill_api_timeout") or 120))
            except BrainHTTPError as exc:
                last_exc = exc
                if exc.code in (401, 403):
                    logger.warning("atbrains: 蒸馏 key 鉴权失败（HTTP %s），换下一个候选择试", exc.code)
                continue
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                continue
            if int(status) in (401, 403):
                logger.warning("atbrains: 蒸馏 key 鉴权失败（HTTP %s），换下一个候选择试", status)
                last_exc = BrainError(f"蒸馏 API 返回 HTTP {status}")
                continue
            used = name
            break
        if not used:
            raise BrainError(f"蒸馏 API 请求失败: {str(last_exc)[:200]}")
        if int(status) != 200:
            raise BrainError(f"蒸馏 API 返回 HTTP {status}")
        choice = (data.get("choices") or [{}])[0] if isinstance(data, dict) else {}
        if str(choice.get("finish_reason") or "") == "length":
            raise BrainError("蒸馏 API 输出被 max_tokens 截断")
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise BrainError("蒸馏 API 响应缺少 choices[0].message.content") from exc
        if not isinstance(content, str) or not content.strip():
            raise BrainError("蒸馏 API 返回空内容")
        return content

    _DISTILL_SLOT_PROMPT = (
        "你是「可变值」槽位抽取器。输入是一批已蒸馏的结论，每行形如 [序号] 结论正文。\n"
        "只对「会变化的可核对值」抽槽位：端口、版本号、路径、库名/表名、账号、开关、配置项取值、\n"
        "表结构版本、模型名、URL。偏好、决策理由、过程记录、一次性任务、泛泛描述一律不抽。\n"
        "subject 用稳定名词（服务器 / 项目名 / 设备名 / 子系统），predicate 不超过 12 字，"
        "value 只放值本身（不要整句，不要带「已升级到」这类叙述）。\n"
        "只输出严格 JSON，不要多余文字："
        '{"slots":[{"index":0,"subject":"...","predicate":"...","value":"..."}]}；\n'
        "没有合格的就输出 {\"slots\":[]}。有就抽、没把握就跳过——宁可漏，不要编。"
    )

    def _distill_slot_pass(self, contents: list[str]) -> dict[int, dict[str, str]]:
        """对缺槽位的蒸馏结论做一次专项补抽（单次 LLM），返回值经形状校验。

        为什么需要这一步：extract 阶段把 slot 当可选字段，模型在长提示里经常直接省略，
        实测「schema 已从 v9 升级到 v10」这种教科书级可变值也没带 slot，claims 就一直是空的。
        """
        if not contents:
            return {}
        lines = "\n".join(f"[{i}] {str(c)[:600]}" for i, c in enumerate(contents))
        try:
            text = self._distill_llm_chat([
                {"role": "system", "content": self._DISTILL_SLOT_PROMPT},
                {"role": "user", "content": lines}])
        except Exception as exc:  # noqa: BLE001
            logger.warning("atbrains: 槽位补抽 LLM 调用失败: %s", str(exc)[:200])
            return {}
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return {}
        try:
            data = json.loads(text[start:end + 1])
        except (TypeError, ValueError):
            return {}
        out: dict[int, dict[str, str]] = {}
        for item in (data.get("slots") or []):
            if not isinstance(item, dict):
                continue
            raw_idx = item.get("index")
            if raw_idx is None:
                continue
            try:
                idx = int(raw_idx)
            except (TypeError, ValueError):
                continue
            if not (0 <= idx < len(contents)):
                continue
            subject = str(item.get("subject") or "").strip()
            pred = str(item.get("predicate") or "").strip()
            val = str(item.get("value") or "").strip()
            # 形状校验：槽位值是「一个值」而不是一段叙述，超长/多句直接判定为不可靠。
            if not subject or not pred or not val:
                continue
            if len(subject) > 60 or len(pred) > 24 or len(val) > 120:
                continue
            if val.count("。") > 1 or len(val.split("，")) > 3:
                continue
            out[idx] = {"subject": subject[:191], "predicate": pred[:96], "value": val[:2000]}
        return out

    def _distill_reconcile(self, candidates: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
        """灰区统一裁决：一次 LLM 调用返回 index → {decision,target_id}。"""
        text = self._distill_llm_chat([
            {"role": "system", "content": self._DISTILL_RECONCILE_PROMPT},
            {"role": "user", "content": json.dumps(candidates, ensure_ascii=False)},
        ])
        raw = text.strip()
        if raw.startswith("```"):
            first_nl = raw.find("\n")
            raw = raw[first_nl + 1:] if first_nl >= 0 else ""
            if raw.rstrip().endswith("```"):
                raw = raw.rstrip()[:-3]
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            raise BrainError("对账输出未找到 JSON 对象")
        try:
            data = json.loads(raw[start:end + 1])
        except Exception as exc:  # noqa: BLE001
            raise BrainError(f"对账输出 JSON 解析失败: {str(exc)[:160]}") from exc
        decisions = data.get("decisions") if isinstance(data, dict) else None
        if not isinstance(decisions, list):
            raise BrainError("对账输出缺少 decisions 数组")
        out: dict[int, dict[str, Any]] = {}
        for d in decisions:
            if not isinstance(d, dict):
                continue
            try:
                idx = int(d.get("index"))
            except (TypeError, ValueError):
                continue
            out[idx] = d
        return out

    def _distill_candidates(self, candidates: list[dict[str, Any]], *,
                            dry_run: bool) -> list[dict[str, Any]]:
        """候选逐条裁剪 → 向量对账 → （真实模式下）写入；返回带决策的明细。"""
        cfg = self.cfg
        min_imp = int(cfg.get("distill_min_importance") or 4)
        max_imp = int(cfg.get("distill_max_importance") or 10)
        if min_imp > max_imp:
            min_imp, max_imp = max_imp, min_imp
        low = float(cfg.get("distill_low_sim") or 0.85)
        high = float(cfg.get("distill_high_sim") or 0.94)
        now_txt = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        items: list[dict[str, Any]] = []
        gray: list[dict[str, Any]] = []

        for cand in candidates:
            content = str(cand.get("content") or "").strip()
            raw_tags = [t.strip() for t in str(cand.get("tags") or "").split(",") if t.strip()]
            tags = ",".join(["distill"] + [t for t in raw_tags if t != "distill"][:3])[:255]
            try:
                imp = int(cand.get("importance") or 5)
            except (TypeError, ValueError):
                imp = 5
            imp = max(min_imp, min(max_imp, imp))
            item: dict[str, Any] = {
                "content": content,
                "kind": str(cand.get("kind") or "fact"),
                "importance": imp,
                "tags": tags,
                "sim": None, "decision": "", "target_id": None,
                "id": None, "deduped": False, "reason": "",
                "evidence_turn_ids": cand.get("evidence_turn_ids") or [],
                "slot": cand.get("slot") if isinstance(cand.get("slot"), dict) else None,
                "claim": None,
            }
            items.append(item)
            if not content:
                item["decision"], item["reason"] = "skip", "empty"
                continue
            if self._DISTILL_SENSITIVE_RE.search(content):
                item["decision"], item["reason"] = "skip", "sensitive"
                logger.warning("atbrains: 蒸馏候选命中敏感信息，已丢弃")
                continue
            hits: list[dict[str, Any]] = []
            try:
                hits = self.vector_search_memories(content, limit=3)
            except Exception as exc:  # noqa: BLE001
                logger.debug("atbrains: 蒸馏对账向量检索失败（按新增）: %s", str(exc)[:160])
            if hits:
                item["sim"] = float(hits[0].get("vscore") or 0.0)
            if item["sim"] is not None and item["sim"] >= high:
                item["decision"], item["reason"] = "skip", "duplicate"
                continue
            if item["sim"] is None or item["sim"] < low:
                item["decision"] = "new"
                continue
            item["_hits"] = hits                 # 灰区：留到统一裁决
            gray.append(item)

        if gray:
            decisions: dict[int, dict[str, Any]] = {}
            try:
                decisions = self._distill_reconcile([
                    {"index": i, "content": it["content"],
                     "neighbors": [{"id": int(h["id"]), "sim": h.get("vscore"),
                                    "content": str(h.get("content") or "")[:400]}
                                   for h in it.get("_hits") or []]}
                    for i, it in enumerate(gray)
                ])
            except Exception as exc:  # noqa: BLE001
                logger.warning("atbrains: 蒸馏对账失败，灰区全部按新增: %s", str(exc)[:200])
                decisions = {}
            for i, it in enumerate(gray):
                dec = decisions.get(i) or {}
                choice = str(dec.get("decision") or "new").strip().lower()
                valid = {int(h["id"]) for h in it.get("_hits") or []}
                target: Optional[int] = None
                try:
                    target = int(dec.get("target_id")) if dec.get("target_id") is not None else None
                except (TypeError, ValueError):
                    target = None
                if choice not in ("skip", "new", "update"):
                    choice = "new"
                if choice == "update" and target not in valid:
                    choice = "new"
                it["decision"] = choice
                it["target_id"] = target if choice == "update" else None
                if choice == "skip":
                    it["reason"] = "reconciled"

        # 槽位补抽：extract 阶段模型经常漏掉 slot（实测 5 条蒸馏里，含「schema 已升级到 v10」
        # 这种教科书级可变值也没给 slot）。对将要写入却没有 slot 的条目做一次专项补抽：
        # 单次 LLM 调用、失败只记 warning 不影响写入，抽出来的值经形状校验才采用。
        if not dry_run:
            missing = [it for it in items if it["decision"] in ("new", "update") and not it.get("slot")]
            if missing:
                try:
                    got = self._distill_slot_pass([it["content"] for it in missing])
                except Exception as exc:  # noqa: BLE001
                    got = {}
                    logger.warning("atbrains: 蒸馏槽位补抽失败（按无槽位写入）: %s", str(exc)[:200])
                filled = 0
                for i, it in enumerate(missing):
                    slot = got.get(i)
                    if slot:
                        it["slot"] = slot
                        filled += 1
                if filled:
                    logger.info("atbrains: 蒸馏槽位补抽 %s/%s 条命中", filled, len(missing))

        for it in items:
            it.pop("_hits", None)
            if dry_run or it["decision"] not in ("new", "update"):
                continue
            try:
                saved = self.remember(it["content"], kind=it["kind"], tags=it["tags"],
                                      importance=it["importance"], source="distill")
            except Exception as exc:  # noqa: BLE001
                it["decision"], it["reason"] = "skip", "write_failed"
                logger.warning("atbrains: 蒸馏写入失败: %s", str(exc)[:200])
                continue
            new_id = saved.get("id")
            it["id"] = new_id
            it["deduped"] = bool(saved.get("deduped"))
            if it["deduped"]:
                it["decision"], it["reason"] = "skip", "deduped"
                it["target_id"] = None
                continue
            if it["decision"] == "update" and it["target_id"] and new_id:
                try:
                    # updated_at = updated_at 必须显式保留：否则 ON UPDATE 会把旧记忆的
                    # “最后更新/衰减年龄”冲成当下。
                    self._exec(
                        "UPDATE brain_memories SET superseded_by = %s, superseded_at = %s, "
                        "updated_at = updated_at WHERE id = %s AND active = 1",
                        (int(new_id), now_txt, int(it["target_id"])))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("atbrains: 标记旧记忆取代失败（新记忆已写入）: %s", str(exc)[:200])
            # 知识层：带 slot 的结论同时写进 brain_claims —— 同槽位只递增 revision、老值自动封版，
            # 这是把「316 条 knowledge 靠 0.90 相似度猜旧值」换成确定性版本的入口。
            slot = it.get("slot") or {}
            if isinstance(slot, dict) and slot:
                subject = str(slot.get("subject") or "").strip()
                pred = str(slot.get("predicate") or "").strip()
                val = str(slot.get("value") or "").strip()
                if subject and pred and val:
                    try:
                        ev = it.get("evidence_turn_ids") or []
                        try:
                            tid: Optional[int] = int(ev[0]) if ev else None
                        except (TypeError, ValueError):
                            tid = None
                        res = self.claim_remember(
                            subject, pred, val, kind="fact", basis="derived_summary",
                            turn_id=tid, quote=str(it["content"])[:500],
                            confidence=70, importance=int(it.get("importance") or 5))
                        it["claim"] = {"claim_id": res.get("claim_id"),
                                       "revision": res.get("revision"),
                                       "created": bool(res.get("created"))}
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("atbrains: 蒸馏结论写 claims 失败: %s", str(exc)[:200])
        return items
