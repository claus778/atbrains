#!/usr/bin/env python3
"""Hermes 客户端打包：clients/hermes-atbrains + 纯标准库 sdk/python/atbrains_client → dist/hermes-atbrains-<版本>.zip"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "clients" / "hermes-atbrains"
SDK_SRC = ROOT / "sdk" / "python" / "atbrains_client"
SKIP_DIRS = {"__pycache__", ".pytest_cache", "data", "tests", "_sdk", "vendor"}
SKIP_FILES = {".DS_Store"}
SKIP_SUFFIX = (".pyc", ".pyo", ".zip")
CLIENT_NAME = "hermes-atbrains"


def _meta(key: str, default: str) -> str:
    try:
        for line in (SRC / "plugin.yaml").read_text(encoding="utf-8").splitlines():
            m = re.match(rf"\s*{re.escape(key)}\s*:\s*(.+?)\s*$", line)
            if m:
                return m.group(1).strip().strip('"').strip("'")
    except Exception:  # noqa: BLE001
        pass
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "dist"))
    args = ap.parse_args()

    plugin_name = _meta("name", "atbrains-memory")
    version = _meta("version", "0.0.0")
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{CLIENT_NAME}-{version}.zip"

    files = collect_files()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for p, rel_out in files:
            zf.writestr(f"{plugin_name}/{rel_out}", p.read_bytes())

    h = hashlib.sha256(target.read_bytes()).hexdigest()
    print(f"✅ {target.relative_to(ROOT) if out_dir == ROOT / 'dist' else target}")
    print(f"   插件目录：{plugin_name}/   版本：{version}   文件数：{len(files)}")
    print(f"   sha256：{h}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
