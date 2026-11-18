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
  local rsync_args=(-a --delete --exclude='__pycache__/' --exclude='*.pyc' --exclude='tests/' --exclude='_sdk/')
  if [ "$MODE" = check ]; then
    echo "比较 hermes-atbrains: $HERMES_SRC → $HERMES_DST"
    if [ ! -d "$HERMES_DST" ]; then
      echo "  ⚠️ 安装副本不存在"
      return 0
    fi
    rsync -na --itemize-changes "${rsync_args[@]}" "$HERMES_SRC/" "$HERMES_DST/" || true
    return 0
  fi
  mkdir -p "$HERMES_DST/_sdk/atbrains_client"
  rsync "${rsync_args[@]}" "$HERMES_SRC/" "$HERMES_DST/"
  rsync -a --delete --exclude='__pycache__/' --exclude='*.pyc' "$SDK_SRC/" "$HERMES_DST/_sdk/atbrains_client/"
  echo "✅ 已安装 Hermes 薄适配器 → $HERMES_DST（纯标准库 HTTP 客户端，无 DB 驱动）"
  echo "   配置 $HERMES_DIR/atbrains.json: {\"service_url\": \"http://<server>:8787\"}"
  echo "   配置 $HERMES_DIR/.env: ATBRAINS_TOKEN=<令牌>"
}

if [ "$TARGET" = all ] || [ "$TARGET" = server ]; then
  install_server
fi
if [ "$TARGET" = all ] || [ "$TARGET" = hermes ]; then
  install_hermes
fi
