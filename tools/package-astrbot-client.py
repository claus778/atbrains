#!/usr/bin/env python3
"""astrbot 客户端打包：clients/astrbot-atbrains → dist/astrbot-atbrains-<版本>.zip

    python3 tools/package-astrbot-client.py [--out dist] [--no-verify]

与 atbrains-memory 的插件包**分开**：这个 zip 只含 AstrBot 客户端（main.py /
metadata.yaml / README.md / vendor/），不含控制台、不含服务端代码 / 文档 / 台账。
zip 里的顶层目录取 metadata.yaml 的 `name:`（= AstrBot 的插件 id `atbrains-memory`），
所以 `unzip -d <AstrBot>/data/plugins/` 解出来就是可用的插件目录。

出包前做一次口令复检（复用 tools/package.py 的 collect_secrets），命中即拒绝出包。
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "clients" / "astrbot-atbrains"
SKIP_DIRS = {"__pycache__", ".pytest_cache", "data"}
SKIP_FILES = {".DS_Store"}
SKIP_SUFFIX = (".pyc", ".pyo", ".zip")
CLIENT_NAME = "astrbot-atbrains"


def _meta(key: str, default: str) -> str:
    """从 metadata.yaml 取 name/version（不引 yaml 依赖，只认 `key: value`）。"""
    try:
        for line in (SRC / "metadata.yaml").read_text(encoding="utf-8").splitlines():
            m = re.match(rf"\s*{re.escape(key)}\s*:\s*(.+?)\s*$", line)
            if m:
                return m.group(1).strip().strip('"').strip("'")
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠️ 读取 metadata.yaml 失败：{exc}")
    return default


def collect_files() -> list[Path]:
    files: list[Path] = []
    for p in sorted(SRC.rglob("*")):
        if p.is_dir():
            continue
        rel = p.relative_to(SRC)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if p.name in SKIP_FILES or p.name.endswith(SKIP_SUFFIX):
            continue
        files.append(p)
    return files


def load_secret_scanner():
    sys.path.insert(0, str(ROOT / "tools"))
    try:
        from package import collect_secrets  # type: ignore
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠️ 口令复检不可用（{exc}），跳过")
        return None
    try:
        return collect_secrets()
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠️ 口令收集失败（{exc}），跳过")
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "dist"), help="输出目录（默认 dist/）")
    ap.add_argument("--no-verify", action="store_true", help="跳过口令复检")
    args = ap.parse_args()

    if not (SRC / "main.py").exists():
        print(f"❌ 源目录不完整：{SRC}")
        return 1

    plugin_name = _meta("name", CLIENT_NAME)
    version = _meta("version", "0.0.0")
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{CLIENT_NAME}-{version}.zip"

    files = collect_files()
    secrets = None if args.no_verify else load_secret_scanner()
    leaks: list[str] = []

    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in files:
            data = p.read_bytes()
            if secrets:
                text = data.decode("utf-8", "replace")
                for sec in secrets:
                    if sec in text:
                        leaks.append(f"{p.relative_to(SRC)} ({len(sec)} 字符)")
                        break
            zf.writestr(f"{plugin_name}/{p.relative_to(SRC).as_posix()}", data)

    if leaks:
        target.unlink(missing_ok=True)
        print("❌ 检出疑似口令/密钥，已删除 zip：")
        for item in leaks:
            print("   -", item)
        return 1

    h = hashlib.sha256(target.read_bytes()).hexdigest()
    print(f"✅ {target.relative_to(ROOT) if out_dir == ROOT / 'dist' else target}")
    print(f"   插件 id（zip 顶层目录）：{plugin_name}/   版本：{version}")
    print(f"   文件数：{len(files)}   大小：{target.stat().st_size / 1024:.1f} KiB")
    print(f"   sha256：{h}")
    print(f"   安装：unzip -o {target.name} -d <AstrBot>/data/plugins/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
