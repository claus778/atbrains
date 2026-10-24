#!/usr/bin/env bash
# atbrains 安装脚本 —— 把本仓库的两个 Hermes 插件同步到 Hermes 插件目录。
#
#   ./install.sh                安装/更新 console(atbrains 控制台) + atbrains-memory(记忆引擎)
#   ./install.sh console        只装 atbrains 控制台插件
#   ./install.sh memory         只装 atbrains-memory 记忆插件（memory provider）
#   ./install.sh check          只检查两侧差异（不写入）
#   ./install.sh --restart-console   安装后顺便重启网页控制台（如果在跑）
#
# 约定:
#   · 代码的家 = 本仓库 `plugin/`（控制台）与 `atbrains-memory/`（记忆插件），git 管理；
#     ${HERMES_HOME:-~/.hermes}/plugins/<name> 是安装副本，勿直接改。
#   · .token（控制台访问口令）是运行时状态 —— 同步时保留安装副本里的，不覆盖、不删除。
#   · 记忆插件改完必须重启网关才生效（provider 不热加载），且不能在网关进程内重启。
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_DIR="${HERMES_HOME:-$HOME/.hermes}"
CONSOLE_SRC="$REPO_DIR/plugin"
CONSOLE_DST="$HERMES_DIR/plugins/atbrains"
BRAIN_SRC="$REPO_DIR/atbrains-memory"
BRAIN_DST="$HERMES_DIR/plugins/atbrains-memory"
PIDFILE="/tmp/atbrains-console.pid"

MODE=install
TARGET=all
RESTART=0
for a in "$@"; do
  case "$a" in
    check|--check)      MODE=check ;;
    install|--install)  MODE=install ;;
    console|atbrains)   TARGET=console ;;
    memory|brain)       TARGET=memory ;;
    all)                TARGET=all ;;
    --restart-console)  RESTART=1 ;;
    *) echo "用法: $0 [install|check] [all|console|memory] [--restart-console]"; exit 2 ;;
  esac
done

[ -f "$CONSOLE_SRC/plugin.yaml" ] || { echo "❌ 找不到 $CONSOLE_SRC/plugin.yaml"; exit 1; }
[ -f "$BRAIN_SRC/plugin.yaml" ]   || { echo "❌ 找不到 $BRAIN_SRC/plugin.yaml"; exit 1; }

RSYNC_ARGS=(-a --delete --exclude='__pycache__/' --exclude='*.pyc' --exclude='.token' --exclude='service-tokens.json')

sync_one() {  # $1=name $2=src $3=dst
  local name="$1" src="$2" dst="$3"
  if [ "$MODE" = check ]; then
    echo "比较 $name: $src  →  $dst"
    if [ ! -d "$dst" ]; then
      echo "  ⚠️ 安装副本不存在（尚未安装）"
      return 0
    fi
    local out
    out="$(rsync -na --itemize-changes "${RSYNC_ARGS[@]}" "$src/" "$dst/")"
    if [ -n "$out" ]; then
      echo "$out"
      echo "  ↑ 以上为差异；跑 ./install.sh $name 同步"
    else
      echo "  ✅ 已同步，无差异"
    fi
    return 0
  fi
  mkdir -p "$dst"
  rsync "${RSYNC_ARGS[@]}" "$src/" "$dst/"
  local ver
  ver="$(grep -m1 '^version:' "$src/plugin.yaml" | awk '{print $2}' | tr -d '"')"
  echo "✅ 已安装 $name v$ver → $dst"
}

if [ "$TARGET" = all ] || [ "$TARGET" = console ]; then
  sync_one "atbrains(控制台)" "$CONSOLE_SRC" "$CONSOLE_DST"
fi
if [ "$TARGET" = all ] || [ "$TARGET" = memory ]; then
  sync_one "atbrains-memory(记忆插件)" "$BRAIN_SRC" "$BRAIN_DST"
fi

if [ "$MODE" = check ]; then
  exit 0
fi

echo ""
echo "下一步（atbrains-memory 记忆插件）："
echo "  1) 凭据写入 $HERMES_DIR/.env：ATBRAINS_REDIS_PASSWORD=…  ATBRAINS_MARIADB_PASSWORD=…"
echo "     旧名 BRAIN_* 仍兼容，沿用即可"
echo "  2) 非密配置 $HERMES_DIR/atbrains.json（host/port/db/machine，见 atbrains-memory/README.md；旧 brain.json 仍兼容）"
echo "  3) hermes config set memory.provider atbrains-memory"
echo "  4) 重启网关（不能在网关进程内重启）：hermes gateway restart"
echo "  5) 验收：hermes atbrains-memory doctor && hermes atbrains-memory status"

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null; then
  if [ "$RESTART" = 1 ]; then
    hermes atbrains stop >/dev/null 2>&1 || true
    hermes atbrains serve --background
  else
    echo ""
    echo "⚠️ 网页控制台正在运行(pid $(cat "$PIDFILE"))，它仍跑着旧代码。"
    echo "   重启: ./install.sh --restart-console   或   hermes atbrains stop && hermes atbrains serve --background"
  fi
fi
