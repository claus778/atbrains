#!/usr/bin/env bash
# atbrains 安装脚本（通用记忆服务端 + 可选 Hermes 薄客户端适配器）
#
#   ./install.sh server          安装 atbrains-server + atbrains-client 到 Python 环境
#   ./install.sh hermes          安装 Hermes 薄适配器（含纯标准库 SDK，零数据库驱动/密码）
#   ./install.sh check           检查 Hermes 适配器安装副本与仓库差异
#   ./install.sh all             同时安装 server 与 hermes 适配器
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_DIR="${HERMES_HOME:-$HOME/.hermes}"
HERMES_SRC="$REPO_DIR/clients/hermes-atbrains"
HERMES_DST="$HERMES_DIR/plugins/atbrains-memory"
SDK_SRC="$REPO_DIR/sdk/python/atbrains_client"

MODE=install
TARGET="${1:-all}"
case "$TARGET" in
  check|--check) MODE=check; TARGET=hermes ;;
  server)        TARGET=server ;;
  hermes|memory) TARGET=hermes ;;
  all)           TARGET=all ;;
  *) echo "用法: $0 [all|server|hermes|check]"; exit 2 ;;
esac

install_server() {
  local py="${ATBRAINS_PYTHON:-$(command -v python3)}"
  local venv="${ATBRAINS_VENV:-$REPO_DIR/.venv}"
  if [ ! -x "$venv/bin/pip" ]; then
    "$py" -m venv "$venv"
  fi
  "$venv/bin/pip" install -q -e "$REPO_DIR/sdk/python" -e "$REPO_DIR/server"
  echo "✅ 已安装 atbrains-server & atbrains-client → $venv"
  echo "   迁移命令: $venv/bin/atbrains-server migrate"
  echo "   启动命令: $venv/bin/atbrains-server serve"
}

install_hermes() {
  [ -f "$HERMES_SRC/plugin.yaml" ] || { echo "❌ 找不到 $HERMES_SRC/plugin.yaml"; exit 1; }
  if [ "$MODE" = check ]; then
    echo "比较 hermes-atbrains: $HERMES_SRC → $HERMES_DST"
    if [ ! -d "$HERMES_DST" ]; then
      echo "  ⚠️ 安装副本不存在"
      return 0
    fi
    if command -v rsync >/dev/null 2>&1; then
      rsync -na --itemize-changes -a --delete --exclude='__pycache__/' --exclude='*.pyc' --exclude='tests/' --exclude='_sdk/' "$HERMES_SRC/" "$HERMES_DST/" || true
    else
      diff -rq -x '__pycache__' -x '*.pyc' -x 'tests' -x '_sdk' "$HERMES_SRC" "$HERMES_DST" || true
    fi
    return 0
  fi
  local py="${ATBRAINS_PYTHON:-$(command -v python3)}"
  "$py" - "$HERMES_SRC" "$SDK_SRC" "$HERMES_DST" <<'PY'
import shutil, sys
from pathlib import Path

src, sdk, dst = map(Path, sys.argv[1:4])
if dst.exists():
    shutil.rmtree(dst)
shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "tests", "_sdk"))
shutil.copytree(sdk, dst / "_sdk" / "atbrains_client", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
PY
  echo "✅ 已安装 Hermes 薄适配器 → $HERMES_DST（纯标准库 HTTP 客户端，无 DB 驱动）"
  echo "   快速配置: hermes atbrains-memory tokset <令牌> --url http://<server>:8080"
  echo "   或手动写: $HERMES_DIR/atbrains.json 与 $HERMES_DIR/.env (ATBRAINS_TOKEN)"
}

if [ "$TARGET" = all ] || [ "$TARGET" = server ]; then
  install_server
fi
if [ "$TARGET" = all ] || [ "$TARGET" = hermes ]; then
  install_hermes
fi
