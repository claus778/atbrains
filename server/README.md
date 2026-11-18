# atbrains-server

Universal multi-agent memory backend (`/v1` HTTP API + MCP Streamable HTTP / stdio + Web Console), backed by MariaDB, Redis, and Qdrant.

## Docker Compose 部署（推荐，支持挂载数据库文件持久化）

`server/` 目录可独立部署，不依赖仓库其他目录：

```bash
cd server
cp .env.example .env
# 按需修改 .env 中的数据库口令、API Key，以及宿主机持久化目录：
#   ATBRAINS_MARIADB_DATA_DIR=./data/mariadb   (或 /var/lib/atbrains/mariadb 等既有库目录)
#   ATBRAINS_REDIS_DATA_DIR=./data/redis
#   ATBRAINS_QDRANT_DATA_DIR=./data/qdrant
#   ATBRAINS_SERVER_DATA_DIR=./data/server     (存放 service-tokens.json、.token、access.log)

docker compose up -d --build
```

常用运维命令：

```bash
# 查看容器与健康状态
docker compose ps

# 签发客户端 Bearer 令牌（<machine>|<agent>）
docker compose exec atbrains-server atbrains-server token-add laptop|codex

# 读取网页控制台 X-Auth-Token 口令
docker compose exec atbrains-server cat /data/.token

# 手动查看服务总览 / 体检
docker compose exec atbrains-server atbrains-server status
docker compose exec atbrains-server atbrains-server doctor
```

> 更多架构与客户端接入说明见仓库根目录 [README.md](../README.md)。
