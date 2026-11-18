#!/bin/bash
# brain 归档清理（brain-prune.timer 每周执行；输出进 journalctl -u brain-prune.service）
# 手动执行：/root/agent-db/prune.sh
set -euo pipefail
PY=$(ls -d /root/.hermes/tools/python-3.14.7+*/bin/python3 2>/dev/null | head -n1)
if [ -z "$PY" ]; then echo "找不到 Hermes 运行时 python"; exit 1; fi
echo "== brain prune $(date '+%F %T %z') =="
HERMES_DISABLE_LAZY_INSTALLS=1 exec "$PY" -I -c '
import sys
sys.path.insert(0, "/root/.hermes/plugins/atbrains-memory")
from core import BrainStore, resolve_config
store = BrainStore(resolve_config("/root/.hermes"))
print("prune:", store.prune())
store.close()
'
