#!/bin/sh
# systemd 入口：用 Hermes 自带的 python。
# ⚠️ 目录名带构建号（python-3.14.9+2026...），Hermes 升级会变 —— 用 glob + sort -V 取最新，别写死。
PY=$(ls -d /root/.hermes/tools/python-3.*/bin/python3 2>/dev/null | sort -V | tail -1)
[ -x "$PY" ] || PY=$(command -v python3)
exec "$PY" /root/.hermes/plugins/atbrains/console/server.py
