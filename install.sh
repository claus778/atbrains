#!/usr/bin/env bash
# atbrains 安装脚本 —— 把本仓库的 plugin/ 安装(同步)到 Hermes 插件目录。
#
#   ./install.sh                  安装/更新到 ${HERMES_HOME:-~/.hermes}/plugins/atbrains
#   ./install.sh check            只检查:显示仓库 ↔ 已安装副本的差异(不写入)
#   ./install.sh --restart-console   安装后顺便重启网页控制台(如果在跑)
#
# 约定:
#   · 代码的家 = 本仓库 plugin/(git 管理);~/.hermes/plugins/atbrains 是安装副本,勿直接改。
#   · .token(控制台访问口令)是运行时状态 —— 同步时保留安装副本里的,不覆盖、不删除。
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$REPO_DIR/plugin"
DST="${HERMES_HOME:-$HOME/.hermes}/plugins/atbrains"
PIDFILE="/tmp/atbrains-console.pid"

MODE=install
RESTART=0
for a in "$@"; do
  case "$a" in
    check|--check)        MODE=check ;;
    install|--install)    MODE=install ;;
    --restart-console)    RESTART=1 ;;
    *) echo "用法: $0 [install|check] [--restart-console]"; exit 2 ;;
  esac
done

if [ ! -f "$SRC/plugin.yaml" ]; then
  echo "❌ 找不到 $SRC/plugin.yaml —— 请在仓库根目录运行本脚本"; exit 1
fi

RSYNC_ARGS=(-a --delete --exclude='__pycache__/' --exclude='*.pyc' --exclude='.token')

if [ "$MODE" = check ]; then
  echo "比较: $SRC  →  $DST"
  if [ ! -d "$DST" ]; then
    echo "⚠️ 安装副本不存在(尚未安装)"; exit 0
  fi
  OUT="$(rsync -na --itemize-changes "${RSYNC_ARGS[@]}" "$SRC/" "$DST/")"
  if [ -n "$OUT" ]; then
    echo "$OUT"
    echo "↑ 以上为差异;[>f..] = 安装副本落后,跑 ./install.sh 同步"
  else
    echo "✅ 已同步,无差异"
  fi
  exit 0
fi

mkdir -p "$DST"
rsync "${RSYNC_ARGS[@]}" "$SRC/" "$DST/"
VER="$(grep -m1 '^version:' "$SRC/plugin.yaml" | awk '{print $2}' | tr -d '\"')"
echo "✅ 已安装 atbrains v$VER → $DST"
echo "   代码的家: $REPO_DIR/plugin(git);该目录是安装副本,勿直接改代码。"
echo "   验证: hermes plugins info atbrains && hermes atbrains status"

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null; then
  if [ "$RESTART" = 1 ]; then
    hermes atbrains stop >/dev/null 2>&1 || true
    hermes atbrains serve --background
  else
    echo "⚠️ 网页控制台正在运行(pid $(cat "$PIDFILE")),它仍跑着旧代码。"
    echo "   重启: ./install.sh --restart-console   或   hermes atbrains stop && hermes atbrains serve --background"
  fi
fi
