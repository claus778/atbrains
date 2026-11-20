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

# 1) 首次启动数据库容器（MariaDB / Redis / Qdrant，后续更新无需重复执行）
docker compose -f docker-compose.db.yml up -d

# 2) 构建并启动应用容器（atbrains-server + nginx）；以后重新部署只跑这一条
docker compose up -d --build
```

> 数据库栈（`docker-compose.db.yml`，项目名 `atbrains-db`）与应用栈（`docker-compose.yml`）通过共享网络 `atbrains-net` 通信，完全解耦：后续执行 `docker compose up -d --build` 或 `docker compose down` 都不会停止或重建数据库容器。

常用运维命令：

```bash
# 查看应用容器 / 数据库容器状态
docker compose ps
docker compose -f docker-compose.db.yml ps

# 签发客户端 Bearer 令牌（<machine>|<agent>）
docker compose exec atbrains-server atbrains-server token-add laptop|codex

# 读取网页控制台 X-Auth-Token 口令
docker compose exec atbrains-server cat /data/.token

# 手动查看服务总览 / 体检
docker compose exec atbrains-server atbrains-server status
docker compose exec atbrains-server atbrains-server doctor
```

> 更多架构与客户端接入说明见仓库根目录 [README.md](../README.md)。
