#!/usr/bin/env python3
"""atbrains 资料库打包/脱敏生成器(仓库内版)。

把活系统的文档/脚本/部署材料复制进本仓库(docs/ scripts/ setup/),并做值级脱敏:
    python3 tools/package.py

规矩(与 agent-db/generate-artifacts.py 同):
  · 只写 docs/ scripts/ setup/ 清单里的文件;**不碰** plugin/、README.md、install.sh、tools/。
  · 想改这些拷贝的内容,改"源"(活系统里的原文件)或改本脚本清单,别只手改副本。
"""
import os, re, shutil, subprocess, json, sys

SRC_TO_DST = [
    # (源, 目标相对路径)
    ("/root/agent_memory.md",                          "docs/agent_memory.md"),
    ("/root/.hermes/plugins/brain/README.md",          "docs/plugin-README.md"),
    ("/root/.hermes/skills/devops/hermes-brain-memory/SKILL.md", "docs/skill-hermes-brain-memory.md"),
    ("/root/.hermes/plugins/brain/skills/brain/SKILL.md",        "docs/skill-brain-memory.md"),
    ("/root/agent-db/brain-overview.txt",              "docs/brain-overview.txt"),
    ("/root/agent-db/migration-20261012.sql",          "setup/migration-20261012.sql"),
    ("/root/agent-db/init.sql",                        "setup/init.sql"),
    ("/root/agent-db/rebuild.sh",                      "setup/rebuild.sh"),
    ("/root/agent-db/verify-accounts.py",              "setup/verify-accounts.py"),
    ("/root/agent-db/credentials.txt",                 "setup/credentials-LEDGER-scrubbed.txt"),
    ("/etc/systemd/system/brain-vector-drain.service", "setup/systemd/brain-vector-drain.service"),
    ("/etc/systemd/system/brain-vector-drain.timer",   "setup/systemd/brain-vector-drain.timer"),
    ("/root/agent-db/audit-turns.py",                  "scripts/audit-turns.py"),
    ("/root/brain-audit-kit/audit-memories.py",        "scripts/audit-memories.py"),
    ("/root/agent-db/brain-overview.py",               "scripts/brain-overview.py"),
    ("/root/agent-db/prune.sh",                        "scripts/prune.sh"),
    ("/root/agent-db/refresh-hot-machine.py",          "scripts/refresh-hot-machine.py"),
]

ROOT = "/root/atbrains"

# 复检时排除:git 仓库、Python 缓存、二进制与运行时机密
SKIP_DIRS = {".git", "__pycache__"}
SKIP_FILES = {".token"}
SKIP_SUFFIX = (".pyc", ".png", ".gz", ".zip", ".tar")


def collect_secrets():
    s = set()
    def add(v):
        if isinstance(v, str):
            v = v.strip().strip('"').strip("'")
            if len(v) >= 8 and not v.startswith("/") and not v.startswith("http"):
                s.add(v)
    # .env
    p = "/root/.hermes/.env"
    if os.path.exists(p):
        for line in open(p, encoding="utf-8", errors="replace"):
            if "=" in line and re.match(r"\s*[A-Z_]*(PASSWORD|KEY|TOKEN|SECRET)", line):
                add(line.split("=", 1)[1])
    # qdrant config
    p = "/root/qdrant/qdrant-config/production.yaml"
    if os.path.exists(p):
        for line in open(p, encoding="utf-8", errors="replace"):
            m = re.match(r"\s*api_key:\s*(\S+)", line)
            if m: add(m.group(1))
    # docker envs
    for c in ("mariadb", "redis", "qdrant"):
        try:
            out = subprocess.run(["docker", "inspect", c, "--format", "{{range .Config.Env}}{{println .}}{{end}}"],
                                 capture_output=True, text=True, timeout=15).stdout
            for line in out.splitlines():
                if re.search(r"(PASSWORD|API_KEY|_KEY|TOKEN|SECRET)=", line, re.I):
                    add(line.split("=", 1)[1])
        except Exception:
            pass
    # credentials.txt 里的口令样式 token
    p = "/root/agent-db/credentials.txt"
    if os.path.exists(p):
        txt = open(p, encoding="utf-8", errors="replace").read()
        for m in re.finditer(r"['\"]([!-~]{12,})['\"]", txt):
            add(m.group(1))
        for m in re.finditer(r"(?:password|口令|密码)[:：=\s]+([!-~]{10,})", txt, re.I):
            add(m.group(1))
    return sorted(s, key=len, reverse=True)


def main():
    secrets = collect_secrets()
    print(f"收集到 {len(secrets)} 个待脱敏字符串(最长 {len(secrets[0]) if secrets else 0} 字符)")
    print("(plugin/、README.md、install.sh、tools/ 不参与生成,不会被本脚本修改)\n")

    os.makedirs(ROOT, exist_ok=True)
    for sub in ("docs", "setup", "setup/systemd", "scripts"):
        os.makedirs(os.path.join(ROOT, sub), exist_ok=True)

    total_hits = 0
    for src, rel in SRC_TO_DST:
        if not os.path.exists(src):
            print(f"  ⚠️ 跳过(不存在): {src}")
            continue
        dst = os.path.join(ROOT, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            data = open(src, encoding="utf-8", errors="replace").read()
        except Exception as e:
            print(f"  ⚠️ 读取失败 {src}: {e}")
            continue
        hits = 0
        for sec in secrets:
            if sec in data:
                n = data.count(sec)
                data = data.replace(sec, "***REDACTED***")
                hits += n
        total_hits += hits
        open(dst, "w", encoding="utf-8").write(data)
        flag = f"  🔒 脱敏 {hits} 处" if hits else ""
        print(f"  ✅ {rel}{flag}")

    print(f"\n总计脱敏替换 {total_hits} 处")
    # 复检:再扫一遍有没有漏网
    print("\n=== 复检:仓库里还有没有密钥残留 ===")
    leaked = []
    for dirpath, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f in SKIP_FILES or f.endswith(SKIP_SUFFIX):
                continue
            p = os.path.join(dirpath, f)
            try:
                txt = open(p, encoding="utf-8", errors="replace").read()
            except Exception:
                continue
            for sec in secrets:
                if sec in txt:
                    leaked.append((p, len(sec)))
    if leaked:
        for p, l in leaked:
            print(f"  ❌ 仍有残留({l} 字符): {p}")
    else:
        print("  ✅ 干净,无残留")


if __name__ == "__main__":
    main()
