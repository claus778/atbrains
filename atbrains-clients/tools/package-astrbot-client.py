#!/usr/bin/env python3
"""AstrBot 客户端打包：clients/astrbot-atbrains + 纯标准库 sdk/python/atbrains_client → dist/astrbot-atbrains-<版本>.zip

用法:
    python3 tools/package-astrbot-client.py [--out dist] [--no-verify]

zip 内嵌纯标准库 `atbrains_client` 到 `_sdk/`，零第三方依赖（无需 pymysql/redis/vendor）。
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
SDK_SRC = ROOT / "sdk" / "python" / "atbrains_client"
SKIP_DIRS = {"__pycache__", ".pytest_cache", "data", "tests", "_sdk", "vendor"}
SKIP_FILES = {".DS_Store"}
SKIP_SUFFIX = (".pyc", ".pyo", ".zip")
CLIENT_NAME = "astrbot-atbrains"


def _meta(key: str, default: str) -> str:
    try:
        for line in (SRC / "metadata.yaml").read_text(encoding="utf-8").splitlines():
            m = re.match(rf"\s*{re.escape(key)}\s*:\s*(.+?)\s*$", line)
            if m:
                return m.group(1).strip().strip('"').strip("'")
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠️ 读取 metadata.yaml 失败：{exc}")
    return default


def collect_files() -> list[tuple[Path, str]]:
    items: list[tuple[Path, str]] = []
    for p in sorted(SRC.rglob("*")):
        if p.is_dir():
            continue
        rel = p.relative_to(SRC)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if p.name in SKIP_FILES or p.name.endswith(SKIP_SUFFIX):
            continue
        items.append((p, rel.as_posix()))
    for p in sorted(SDK_SRC.rglob("*.py")):
        rel = p.relative_to(SDK_SRC.parent)
        if "__pycache__" in rel.parts:
            continue
        items.append((p, f"_sdk/{rel.as_posix()}"))
    return items


def load_secret_scanner():
    sys.path.insert(0, str(ROOT / "tools"))
    try:
        from package import collect_secrets  # type: ignore
        return collect_secrets()
    except Exception:  # noqa: BLE001
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "dist"), help="输出目录（默认 dist/）")
    ap.add_argument("--no-verify", action="store_true", help="跳过口令复检")
    args = ap.parse_args()

    if not (SRC / "main.py").exists() or not (SDK_SRC / "__init__.py").exists():
        print(f"❌ 源目录不完整：{SRC} / {SDK_SRC}")
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
        for p, rel_out in files:
            data = p.read_bytes()
            if secrets:
                text = data.decode("utf-8", "replace")
                for sec in secrets:
                    if sec in text:
                        leaks.append(f"{rel_out} ({len(sec)} 字符)")
                        break
            zf.writestr(f"{plugin_name}/{rel_out}", data)

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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
