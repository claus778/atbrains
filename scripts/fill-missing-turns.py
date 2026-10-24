"""把「未被 brain 记录、但段内有工具调用」的真实用户轮补进 brain_turns（随后由回填脚本补转录）。"""
import datetime
import re
import sqlite3
import sys

sys.path.insert(0, "/root/.hermes/plugins/atbrains-memory")
from core import BrainStore, resolve_config  # noqa: E402

KEY_RE = re.compile(r"sk-[A-Za-z0-9._\-]{4,}", re.I)
norm = lambda s: " ".join((s or "").split())
mask = lambda s: KEY_RE.sub("<key>", s or "")
_ORIGIN = "gateway message origin"


def real_user_text(text: str):
    t = (text or "").strip()
    low = t.lower()
    if low.startswith("[context compaction") or low.startswith("[system"):
        return None
    if low.startswith(_ORIGIN):
        parts = t.split("\n\n", 1)
        return parts[1].strip() if len(parts) == 2 else ""
    return t


store = BrainStore(resolve_config("/root/.hermes"))
con = sqlite3.connect("file:/root/.hermes/state.db?mode=ro", uri=True)
CURRENT = "local-67c50350a0834719ab729678818d1e24f5622868ddaa636f7e1eab119d6d09e7"

sess = [r["session_id"] for r in store._exec(
    "SELECT DISTINCT session_id FROM brain_turns WHERE machine='server'", fetch=True)]
added = 0
for sid in sess:
    msgs = con.execute(
        "SELECT role, content, timestamp FROM messages WHERE session_id=? AND role!='system' ORDER BY id",
        (sid,)).fetchall()
    bt = store._exec("SELECT user_content FROM brain_turns WHERE session_id=%s", (sid,), fetch=True)
    pool = [norm(mask(x["user_content"])) for x in bt]
    try:
        source = con.execute("SELECT source FROM sessions WHERE id=?", (sid,)).fetchone()[0] or ""
    except Exception:
        source = ""
    for i, (role, content, ts) in enumerate(msgs):
        if role != "user":
            continue
        real = real_user_text(content)
        if not real:
            continue
        m = mask(norm(real))
        if any(p == m or (len(p) >= 8 and m.startswith(p)) or (len(m) >= 8 and p.startswith(m))
               or (p[:60] == m[:60]) for p in pool):
            continue
        seg = []
        for j in range(i + 1, len(msgs)):
            if msgs[j][0] == "user":
                break
            seg.append(msgs[j])
        if not seg or "tool" not in [s[0] for s in seg]:
            continue
        if sid == CURRENT and i >= len(msgs) - 3:
            print(f"  跳过在途轮：{sid} | {real[:40]}")
            continue
        last_ts = seg[-1][2]
        asst = next((s[1] for s in reversed(seg) if s[0] == "assistant" and (s[1] or "").strip()), "")
        ts_utc = datetime.datetime.fromtimestamp(float(last_ts), datetime.timezone.utc)
        _, tid = store._exec(
            "INSERT INTO brain_turns (session_id, platform, author, machine, truncated, "
            "user_content, assistant_content, ts) VALUES (%s, %s, '', 'server', 0, %s, %s, %s)",
            (sid, str(source)[:48], str(real)[:16000], str(asst)[:16000], ts_utc))
        store._exec(
            "INSERT INTO brain_sessions (session_id, platform, machine, started_at, last_active, turn_count) "
            "VALUES (%s, %s, 'server', NOW(), NOW(), 1) ON DUPLICATE KEY UPDATE turn_count = turn_count + 1",
            (sid, str(source)[:48]))
        added += 1
        print(f"  补 {tid} | {sid} | {real[:40]} | 段内 {len(seg)} 条")
print(f"新增 {added} 轮")
