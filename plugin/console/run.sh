#!/bin/sh
# systemd 入口：用 Hermes 自带 python（目录名含构建号，用 glob 解析，勿写死）
PY=$(ls -d /root/.hermes/tools/python-3.14.9*/bin/python3 2>/dev/null | head -1)
[ -x "$PY" ] || PY=$(command -v python3)
exec "$PY" /root/.hermes/plugins/atbrains/console/server.py
