"""纯函数工具：检索词切分、转义、平台名规范化、分层/衰减、类型转换。"""
from __future__ import annotations

import logging
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Optional
import json
import re


logger = logging.getLogger("atbrains")

_CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]{2,}")

_LATIN_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z_\-.]{1,}")

# 常见虚词双字组合，屏蔽以免召回噪声
_CJK_STOPWORDS = {
    "什么", "怎么", "如何", "可以", "我的", "你是", "这个", "那个", "一下", "已经",
    "现在", "今天", "明天", "帮我", "请问", "是否", "以及", "但是", "因为", "所以",
    "然后", "如果", "就是", "还是", "只是", "不是", "没有", "一个", "我们", "他们",
    "需要", "应该", "可能", "这里", "那里", "时候", "问题", "东西", "知道", "觉得",
    "看看", "好的", "谢谢", "麻烦", "想要", "能够", "为了", "关于", "对于", "这么",
}

def search_terms(query: str, max_terms: int = 10) -> list[str]:
    """把自然语言查询拆成 LIKE 检索词：拉丁词 + 中文双字组。"""
    q = (query or "").strip()
    if not q:
        return []
    terms: list[str] = []
    for m in _LATIN_RE.finditer(q):
        terms.append(m.group(0))
    for m in _CJK_RUN_RE.finditer(q):
        run = m.group(0)
        if len(run) <= 4:
            terms.append(run)
        else:
            terms.extend(run[i:i + 2] for i in range(len(run) - 1))
    out: list[str] = []
    for t in terms:
        t = t.strip()
        if len(t) < 2 or t in _CJK_STOPWORDS or t in out:
            continue
        out.append(t)
        if len(out) >= max_terms:
            break
    return out

def _esc_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

# 关键词检索改成单次 REGEXP 扫描（替代 terms×columns 个 OR-LIKE）：MariaDB 的 REGEXP 走
# PCRE，查询词里的元字符必须逐个转义，否则 `.` `*` 之类会退化成通配符（与 LIKE 不再等价）。
_REGEXP_META_RE = re.compile(r"([\\^$.\[\]|()?*+{}])")

def _esc_regexp(term: str) -> str:
    """转义 PCRE 元字符（\\ ^ $ . [ ] | ( ) ? * + { }）；中文与普通字符原样保留。"""
    return _REGEXP_META_RE.sub(r"\\\1", str(term or ""))

def _regexp_pattern(terms: list[str]) -> str:
    """把检索词拼成单次 REGEXP 的 OR 模式；空词丢弃，无有效词返回 ""（调用方回退旧 SQL）。"""
    pats = []
    for t in terms or []:
        pat = _esc_regexp(str(t or "").strip())
        if pat:
            pats.append(pat)
    return "|".join(pats)

def _regexp_target(columns: tuple[str, ...]) -> str:
    """单列直接用列名；多列先 CONCAT_WS 成一串再整体匹配（NULL 由 COALESCE 兜住）。

    与「逐列 LIKE」同一语义：任一词出现在任一列即命中。词里不含空格，而 CONCAT_WS 用
    空格做分隔，因此不会出现跨列拼接产生的假命中。
    """
    cols = tuple(str(c) for c in columns)
    if len(cols) == 1:
        return cols[0]
    return "CONCAT_WS(' ', " + ", ".join(f"COALESCE({c}, '')" for c in cols) + ")"

def _cfg_flag(cfg: dict[str, Any], key: str, default: int = 1) -> bool:
    """int 型开关：缺省 / None / 非法值一律回落到 default。"""
    try:
        return int(cfg.get(key, default)) != 0
    except (TypeError, ValueError):
        return bool(default)

def _clip(text: str, limit: int) -> str:
    text = (text or "").replace("\r", " ").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"

def _mask_secret(value: str) -> str:
    """令牌掩码：最多露前 4 位（短令牌整体隐藏）—— 绝不完整回显、绝不进日志。"""
    v = str(value or "")
    if not v:
        return ""
    return v[:4] + "…" if len(v) > 8 else "…"

# 常见通用入口别名归并；其余平台名（hermes-cli / qq / discord / dsh 等）小写透传。
_PLATFORM_ALIASES = {
    "tool": "cli",
    "local": "desktop",
    "tui": "desktop",
    "gui": "desktop",
}

def normalize_platform(value: Any) -> str:
    """把入口名收敛进词表：小写去空白，别名归并，空值→unknown。"""
    v = str(value or "").strip().lower()
    if not v:
        return "unknown"
    return _PLATFORM_ALIASES.get(v, v)

_LAYER_BY_KIND = {
    "builtin-memory": "profile",   # 用户画像 / 偏好：长期稳定，不衰减
    "fact": "knowledge",           # 事实：长期有效，不衰减
    "decision": "knowledge",       # 决策：长期有效，不衰减
    "insight": "knowledge",        # 归纳类结论：长期有效，不衰减
    "preference": "knowledge",     # 偏好：长期有效，不衰减
}

def _memory_type(kind: str, explicit: str = "") -> str:
    """记忆分层：显式指定优先，否则按 kind 映射（其余归为 episodic）。"""
    explicit = str(explicit or "").strip()
    if explicit:
        return explicit[:16]
    k = str(kind or "").strip()
    return _LAYER_BY_KIND.get(k, "episodic")

def _age_days(value: Any, now_ts: float) -> Optional[float]:
    """库内 DATETIME（UTC 口径）到当前的天数；拿不到或异常返回 None（调用方保持不衰减）。"""
    if value is None:
        return None
    try:
        if hasattr(value, "timestamp"):
            ts = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
            return max(0.0, (now_ts - ts.timestamp()) / 86400.0)
        if isinstance(value, (int, float)):
            return max(0.0, (now_ts - float(value)) / 86400.0)
        text = str(value).strip().replace("T", " ")
        dt = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return max(0.0, (now_ts - dt.timestamp()) / 86400.0)
    except Exception:  # noqa: BLE001
        return None

def _utc_epoch(value: Any) -> Optional[float]:
    """库内 DATETIME（UTC 口径）→ epoch 秒；naive 必须按 UTC 解释。

    直接对 naive datetime 调 .timestamp() 会按本机时区解释：容器是 UTC 时看不出问题，
    Windows 桌面版（UTC+8）会把所有库内时间整体偏移 8 小时。
    """
    if value is None:
        return None
    try:
        if hasattr(value, "timestamp"):
            ts = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
            return float(ts.timestamp())
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value).strip().replace("T", " ")
        dt = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return float(dt.timestamp())
    except Exception:  # noqa: BLE001
        return None

def _cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度（纯 Python 兜底实现；维度不一致返回 0）。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / ((na ** 0.5) * (nb ** 0.5))

class BrainError(RuntimeError):
    pass


class BrainForbidden(PermissionError):
    """跨身份越权访问或修改 → HTTP 403。"""


class BrainHTTPError(BrainError):
    """带 HTTP 状态码的错误（用于 401 重读 key / 404 建集合 等分支）。"""

    def __init__(self, code: int, url: str, body: str = ""):
        super().__init__(f"HTTP {code} {url}: {body[:200]}")
        self.code = int(code)
        self.url = url
        self.body = body

def _as_text(value: Any, limit: int = 1_000_000) -> str:
    """工具参数/结果统一成字符串（已是字符串就截断，其余 JSON 序列化）。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value[:limit]
    try:
        return json.dumps(value, ensure_ascii=False, default=str)[:limit]
    except Exception:  # noqa: BLE001
        return str(value)[:limit]

def _as_tool_calls(value: Any) -> list[dict[str, Any]]:
    """tool_calls 可能是 list，也可能是历史行里的 JSON 字符串 —— 统一成 list[dict]。

    早期归档/某些 provider 把 tool_calls 存成字符串，直接 enumerate() 会逐字符迭代，
    一次回填能写出几百万行垃圾（实测踩过）。
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:  # noqa: BLE001
            return []
    if not isinstance(value, list):
        return []
    return [c for c in value if isinstance(c, dict)]

def _epoch_to_dt(value: Any) -> Optional[datetime]:
    """消息里的 timestamp 统一成 UTC naive：支持 epoch 秒（int/float）与 ISO 串。"""
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), timezone.utc).replace(tzinfo=None)
        s = str(value).strip()
        if re.fullmatch(r"\d+(\.\d+)?", s):
            return datetime.fromtimestamp(float(s), timezone.utc).replace(tzinfo=None)
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt
    except Exception:  # noqa: BLE001
        return None

def _db_safe(value: Any) -> Any:
    """把无法编码成 utf-8 的 str（lone surrogate）替换掉。

    PyMySQL 在绑定参数时会做 utf-8 编码，遇到 JSON 里的 `\\ud800` 这类非法代理字符会抛
    UnicodeEncodeError，导致整条 INSERT（整轮对话/整条记忆）失败。这里只对确有非法字符的
    字符串做一次替换，正常值原样返回。
    """
    if isinstance(value, str):
        if value.isascii():          # 绝大多数值：一次 C 级扫描，省掉 encode 分配
            return value
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return value.encode("utf-8", "replace").decode("utf-8")
    return value

def _cursor_start(msgs: list[dict[str, Any]], done: int) -> int:
    """转录游标切片：正常从 done 续写；历史变短（压缩/回退）时退回最后一个 user。

    独立成纯函数便于自测。done 为负数按 0 处理（坏数据不应导致永久跳过）。
    """
    try:
        done = int(done)
    except (TypeError, ValueError):
        return 0
    if done < 0:
        return 0
    if done > len(msgs):
        for i in range(len(msgs) - 1, -1, -1):
            if str(msgs[i].get("role") or "") == "user":
                return i
        return 0
    return done

def _window_start(msgs: list[dict[str, Any]], done: int,
                 total: Optional[int] = None) -> tuple[int, int]:
    """尾部窗口的起点：(窗口内下标, 对应的绝对游标)。

    客户端会按条数上限把会话**截断成尾部窗口**（dsh 插件默认 400 条）再上传，而游标
    `archived_messages` 记的是整个会话的绝对已归档条数。只按 len(msgs) 比较会让
    「窗口长度 == 游标」永远成立 → 每轮增量都算成空（实测 dsh 会话 400 条后每轮
    message_count=0）。这里用 `total`（会话消息总数）把游标换算到窗口坐标。

    total 缺省或小于 len(msgs) 时按「窗口即全量」处理，与老客户端行为一致。
    返回的绝对游标供 UPDATE 使用，保证不因窗口而漂移。
    """
    n = len(msgs)
    try:
        total_n = int(total) if total is not None else n
    except (TypeError, ValueError):
        total_n = n
    if total_n < n:
        total_n = n
    offset = total_n - n
    try:
        done = int(done)
    except (TypeError, ValueError):
        return 0, offset
    if done < 0:
        done = 0
    if done < offset:
        # 游标落在窗口之前：中间那段补不回来（离线太久/窗口太小），只能从窗口起点续
        logger.warning(
            "atbrains: 转录窗口不足以覆盖游标 %s..%s（会话 %s 条，窗口 %s 条），中间段落丢弃",
            done, offset, total_n, n)
        return 0, offset
    rel = done - offset
    if rel > n:
        # 历史变短（压缩/回退）：退回最后一个 user 重新起算
        for i in range(n - 1, -1, -1):
            if str(msgs[i].get("role") or "") == "user":
                return i, offset + i
        return 0, offset
    return rel, offset + rel
