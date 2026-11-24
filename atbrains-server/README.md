# atbrains-server

多 Agent 共享长期记忆服务端（`/v1` HTTP API + `/mcp` Streamable HTTP / stdio + Web 控制台），统一管理 MariaDB、Redis 与 Qdrant。

> 本目录可独立拷贝或单独部署，不依赖仓库其他目录；客户端项目位于 `../atbrains-clients/`。

## 1. Docker Compose 部署（推荐）

数据库容器与应用容器已拆分，后续更新代码不会停止或重建数据库：

```bash
cd atbrains-server
cp .env.example .env
# 按需修改 .env 中的数据库口令、API Key 与宿主机持久化目录

# 1) 首次启动数据库容器（MariaDB / Redis / Qdrant，仅需执行一次）
docker compose -f docker-compose.db.yml up -d

# 2) 构建并启动应用容器（atbrains-server + nginx；以后重新部署只需这一条）
docker compose up -d --build
```

启动后：
- **Web 控制台 / 统一入口**：`http://<服务器>:8080`（Nginx）
- **后端 API**：`atbrains-server:8787`（支持 `/v1/*` 与 `/mcp`）
- **查看控制台初始口令**：`cat data/server/.token`（或查看容器启动日志）

## 2. 常用运维命令

```bash
# 查看应用容器 / 数据库容器状态
docker compose ps
docker compose -f docker-compose.db.yml ps

# 签发客户端 Bearer 令牌（<machine>|<agent>）
docker compose exec atbrains-server atbrains-server token-add laptop|codex

# 读取网页控制台口令
docker compose exec atbrains-server cat /data/.token

# 服务总览 / 系统体检
docker compose exec atbrains-server atbrains-server status
docker compose exec atbrains-server atbrains-server doctor
```

## 3. 本地开发与测试

```bash
./install.sh
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
```
