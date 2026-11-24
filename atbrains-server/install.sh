#!/usr/bin/env bash
# atbrains-server 本地 Python 环境安装脚本
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${ATBRAINS_PYTHON:-$(command -v python3)}"
VENV="${ATBRAINS_VENV:-$REPO_DIR/.venv}"

if [ ! -x "$VENV/bin/pip" ]; then
  "$PY" -m venv "$VENV"
fi
"$VENV/bin/pip" install -q -e "$REPO_DIR"
echo "✅ 已安装 atbrains-server → $VENV"
echo "   迁移命令: $VENV/bin/atbrains-server migrate"
echo "   启动命令: $VENV/bin/atbrains-server serve"
