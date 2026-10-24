"""账号验证：从迁移 SQL 原文提取密码（零手抄），机械测试三个 agent 账号与越权隔离。"""
import re
import sys

sys.path.insert(0, "/root/.hermes/plugins/atbrains-memory/vendor")
import pymysql  # noqa: E402

SQL = "/root/agent-db/migration-20261012.sql"
text = open(SQL, encoding="utf-8").read()


def pw(name: str) -> str:
    m = re.search(r"'" + name + r"'@'%'\s+IDENTIFIED BY '([^']*)'", text)
    assert m, f"password for {name} not found"
    return m.group(1)


print("密码长度:", {n: len(pw(n)) for n in ("omp", "dsh", "artex")})

ok = True
for name in ("omp", "dsh", "artex"):
    try:
        conn = pymysql.connect(host="127.0.0.1", port=57306, user=name,
                               password=pw(name), database=f"{name}_agent_memory",
                               connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("SELECT DATABASE()")
            print(f"{name}: 登录成功, db={cur.fetchone()[0]}")
        conn.close()
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"{name}: 登录失败 {exc}")

# 自己的库应可访问, 别人的库应被拒
checks = [
    ("agent", "agent_memory", True),
    ("omp", "agent_memory", False),
    ("dsh", "omp_agent_memory", False),
    ("artex", "dsh_agent_memory", False),
]
for user, target, expect_ok in checks:
    password = pw(user) if user != "agent" else None
    if user == "agent":
        # 从 .env 读
        for line in open("/root/.hermes/.env", encoding="utf-8"):
            if line.startswith("BRAIN_MARIADB_PASSWORD="):
                v = line.split("=", 1)[1].strip()
                password = v[1:-1] if v[:1] in "\"'" else v
    try:
        conn = pymysql.connect(host="127.0.0.1", port=57306, user=user,
                               password=password, database=target, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = DATABASE()")
            n = cur.fetchone()[0]
        conn.close()
        verdict = "可访问" if expect_ok else "!!! 意外放行（应拒绝）"
        if not expect_ok:
            ok = False
        print(f"{user} -> {target}: {verdict} (表数={n})")
    except Exception as exc:  # noqa: BLE001
        verdict = "被拒绝(符合预期)" if not expect_ok else f"!!! 应可访问却失败: {exc}"
        if expect_ok:
            ok = False
        print(f"{user} -> {target}: {verdict}")

print()
print("全部符合预期 ✓" if ok else "存在问题 ✗")
sys.exit(0 if ok else 1)
