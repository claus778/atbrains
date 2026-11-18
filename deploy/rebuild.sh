#!/bin/bash
# =====================================================================
# Agent 记忆数据库 —— 容器重建命令 (2026-10-12 定稿; 2026-10-15 起数据为绑定挂载)
# 数据在宿主机目录: /root/mariadb (=mariadb) / /root/redis (=redis), 重建容器不丢数据 (旧命名卷 mariadb_data/redis_data 已弃用, 保留未删)。
# init.sql 仅对空数据目录首次初始化生效; 已有数据的目录不受影响。
# 时区: 固定 UTC (default-time-zone=+00:00, 99-timezone.cnf 挂载; 2026-10-13 曾切 +08:00 后按用户要求回退)。
#
# 布局台账 (详见同目录 credentials.txt):
#   hermes 系实例: 库 agent_memory  user agent | Redis db0-db4 预留(brain:; server=db0, x260-debian=db1, windows-laptop=db2, db3=新实例(10-13))
#   omp        : 库 omp_agent_memory      user omp     | Redis db5  前缀 omp:
#   dsh        : 库 dsh_agent_memory      user dsh     | Redis db6  前缀 dsh:
#   artex      : 库 artex_agent_memory    user artex   | Redis db7  前缀 artex:
# =====================================================================
set -e

docker stop -t 60 mariadb redis 2>/dev/null || true
docker rm mariadb redis 2>/dev/null || true

docker run -d \
  --name mariadb \
  --restart unless-stopped \
  -p 57306:3306 \
  -e MARIADB_ROOT_PASSWORD='***REDACTED***' \
  -e MARIADB_DATABASE=agent_memory \
  -e MARIADB_USER=agent \
  -e MARIADB_PASSWORD='***REDACTED***' \
  -v /root/mariadb:/var/lib/mysql \
  -v /root/agent-db/init.sql:/docker-entrypoint-initdb.d/10-agents.sql:ro \
  -v /root/agent-db/mysql-conf.d/99-timezone.cnf:/etc/mysql/conf.d/99-timezone.cnf:ro \
  mariadb:latest \
  --character-set-server=utf8mb4 \
  --collation-server=utf8mb4_unicode_ci

docker run -d \
  --name redis \
  --restart unless-stopped \
  -p 9079:6379 \
  -v /root/redis:/data \
  redis:latest \
  redis-server --appendonly yes --requirepass '***REDACTED***'

echo "done: mariadb(57306)=hermes/omp/dsh/artex 四库 + redis(9079): db0-4=hermes 系预留, db5-7=omp/dsh/artex"
