#!/usr/bin/env python3
"""atbrains /v1 契约测试（见 docs/protocol-v1.md）。

做四件事：
  1. health：状态码 / ok / version
  2. status：machine + agent 存在（证明令牌身份解析正常）
  3. recall：text / count / items 形状
  4. turn：写一轮带消息的对话，第二次重复提交必须 message_count 增量为 0（游标幂等）
     —— 这一项要读库，用 --no-db 可跳过（只做 HTTP 部分）

用法（在服务器上）：
  python3 scripts/contract-v1.py                     # 用本机 8787 + service-tokens.json 的 server 令牌
  python3 scripts/contract-v1.py --url http://127.0.0.1:8787 --token-file /path/token.txt
  python3 scripts/contract-v1.py --no-db             # 只测 HTTP

退出码 0 = 全过；1 = 有用例失败。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

TEST_SESSION = "zz-contract-v1"
HERMES_HOME = Path(os.environ.get("HERMES_HOME") or "/root/.hermes")
TOKENS = HERMES_HOME / "plugins" / "atbrains" / "service-tokens.json"
ENV_FILE = HERMES_HOME / ".env"


def load_token(explicit: str, token_file: str, env_name: str) -> str:
    if explicit:
        return explicit.strip()
    if token_file:
        return Path(token_file).read_text(encoding="utf-8").splitlines()[0].strip()
    v = os.environ.get(env_name, "").strip()
    if v:
        return v
    data = json.loads(TOKENS.read_text(encoding="utf-8"))
    for name in ("server", "r9000p-Windows|dsh"):
        if name in data:
            return str(data[name])
    raise SystemExit(f"没找到令牌：{TOKENS} 里没有 server/dsh 条目")


def call(base: str, path: str, token: str, body: dict | None = None) -> tuple[int, dict]:
    url = base.rstrip("/") + path
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, method="POST" if body is not None else "GET",
        headers={"content-type": "application/json", "authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:  # noqa: BLE001
            return e.code, {}


def mysql(sql: str) -> list[list[str]]:
    pwd = ""
    for line in ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("BRAIN_MARIADB_PASSWORD"):
            v = line.split("=", 1)[1].strip()
            pwd = v[1:-1] if v[:1] == v[-1:] and v[:1] in "\"'" else v
            break
    env = dict(os.environ, MYSQL_PWD=pwd)
    out = subprocess.run(
        ["mysql", "-h127.0.0.1", "-P57306", "-uagent", "agent_memory",
         "--default-character-set=utf8mb4", "-N", "-B", "-e", sql],
        capture_output=True, text=True, env=env, timeout=30)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[:300])
    return [line.split("\t") for line in out.stdout.strip().splitlines() if line]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8787")
    ap.add_argument("--token", default="")
    ap.add_argument("--token-file", default="")
    ap.add_argument("--token-env", default="ATBRAINS_CONTRACT_TOKEN")
    ap.add_argument("--no-db", action="store_true", help="跳过数据库断言与清理")
    args = ap.parse_args()
    token = load_token(args.token, args.token_file, args.token_env)

    fails: list[str] = []

    def check(label: str, cond: bool, detail: str = "") -> None:
        print(f"{'PASS' if cond else 'FAIL'}  {label}{(' — ' + detail) if detail and not cond else ''}")
        if not cond:
            fails.append(label)

    # 0. 清掉上次可能的残留，保证断言干净
    if not args.no_db:
        try:
            mysql(f"DELETE FROM brain_turn_messages WHERE turn_id IN "
                  f"(SELECT id FROM brain_turns WHERE session_id='{TEST_SESSION}');"
                  f"DELETE FROM brain_turns WHERE session_id='{TEST_SESSION}';"
                  f"DELETE FROM brain_sessions WHERE session_id='{TEST_SESSION}';")
        except Exception as exc:  # noqa: BLE001
            print(f"WARN  预清理跳过：{exc}")

    # 1. health
    code, health = call(args.url, "/v1/health", token)
    check("health 200", code == 200, str(code))
    check("health.ok", health.get("ok") is True)
    version = str(health.get("version") or "")
    check("health.version 存在", bool(version), version)
    major = version.split(".")[0]

    # 2. status（验身份解析）
    code, status = call(args.url, "/v1/memory/status", token, {})
    check("status 200", code == 200, str(code))
    check("status.machine 存在", bool(status.get("machine")), str(status.get("machine")))
    check("status.agent 存在", bool(status.get("agent")), str(status.get("agent")))

    # 3. recall
    code, recall = call(args.url, "/v1/memory/recall", token, {"query": "记忆库叫什么名字", "limit": 3})
    check("recall 200", code == 200, str(code))
    check("recall.text 是字符串", isinstance(recall.get("text"), str))
    check("recall.count 是整数", isinstance(recall.get("count"), int))
    check("recall.items 是数组", isinstance(recall.get("items"), list))

    # 4. turn + 游标幂等
    msgs = [
        {"role": "user", "content": "契约测试：这一轮应该被归档"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "list_dir", "arguments": "{}"}}]},
        {"role": "tool", "tool_name": "list_dir", "tool_call_id": "c1", "content": "a.txt"},
        {"role": "assistant", "content": "只有一个 a.txt"},
    ]
    payload = {"session_id": TEST_SESSION, "user": msgs[0]["content"], "assistant": msgs[-1]["content"],
               "platform": "dsh", "messages": msgs}
    code, t1 = call(args.url, "/v1/memory/turn", token, payload)
    check("turn#1 200", code == 200 and t1.get("ok") is True, str(t1))
    check("turn#1 返回 id", isinstance(t1.get("id"), int) and t1["id"] > 0)
    code, t2 = call(args.url, "/v1/memory/turn", token, payload)
    check("turn#2 200", code == 200 and t2.get("ok") is True, str(t2))
    check("turn#2 是新的一轮", isinstance(t2.get("id"), int) and t2["id"] != t1.get("id"))

    if not args.no_db:
        rows = mysql(
            f"SELECT id, platform, machine, author, message_count FROM brain_turns "
            f"WHERE session_id='{TEST_SESSION}' ORDER BY id;")
        check("库内 2 轮", len(rows) == 2, str(len(rows)))
        if len(rows) == 2:
            first, second = rows[0], rows[1]
            check("platform=dsh", first[1] == "dsh", first[1])
            check("machine 非空", bool(first[2]), first[2])
            check("author 非空", bool(first[3]), first[3])
            check("第一轮转录 4 条", first[4] == "4", first[4])
            check("第二轮增量 0 条（游标幂等）", second[4] == "0", second[4])
        else:
            fails.extend(["库内 2 轮", "platform=dsh", "machine 非空", "author 非空",
                          "第一轮转录 4 条", "第二轮增量 0 条（游标幂等）"])
        mysql(f"DELETE FROM brain_turn_messages WHERE turn_id IN "
              f"(SELECT id FROM brain_turns WHERE session_id='{TEST_SESSION}');"
              f"DELETE FROM brain_turns WHERE session_id='{TEST_SESSION}';"
              f"DELETE FROM brain_sessions WHERE session_id='{TEST_SESSION}';")
        left = mysql(
            f"SELECT (SELECT COUNT(*) FROM brain_turns WHERE session_id='{TEST_SESSION}')"
            f"+(SELECT COUNT(*) FROM brain_sessions WHERE session_id='{TEST_SESSION}');")
        check("测试数据已清理", bool(left) and left[0][0] == "0", str(left))

    print(f"\n协议主版本 = {major}；{'全部通过' if not fails else f'{len(fails)} 项失败'}")
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
