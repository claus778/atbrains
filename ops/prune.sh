#!/usr/bin/env bash
# atbrains 归档清理（可选定时任务；优先使用已安装的 atbrains-server）
set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${ATBRAINS_PYTHON:-$(command -v python3)}"
echo "== atbrains prune $(date '+%F %T %z') =="
PYTHONPATH="$REPO_DIR/server${PYTHONPATH:+:$PYTHONPATH}" exec "$PY" -c '
from atbrains_server.config import resolve_config
from atbrains_server.store import AtbrainsStore
store = AtbrainsStore(resolve_config(), start_workers=False)
print("prune:", store.prune())
store.close()
'
