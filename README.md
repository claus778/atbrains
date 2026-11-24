# atbrains

多 Agent 共享长期记忆系统。仓库内分为两个完全独立的子项目目录，可各自独立部署、测试与分发：

| 目录 | 说明 | 文档 |
|---|---|---|
| **`atbrains-server/`** | 服务端（`/v1` HTTP API + `/mcp` + Web 控制台 + Docker Compose 数据库/应用拆分编排 + 协议契约 + 运维脚本） | [atbrains-server/README.md](atbrains-server/README.md) |
| **`atbrains-clients/`** | 客户端集合（零依赖 Python SDK + Hermes / AstrBot / DSH 适配器插件 + 打包安装脚本） | [atbrains-clients/README.md](atbrains-clients/README.md) |

## 快速开始

### 1. 部署服务端（`atbrains-server/`）

```bash
cd atbrains-server
cp .env.example .env
# 首次启动数据库容器（仅需执行一次）
docker compose -f docker-compose.db.yml up -d
# 构建并启动应用容器（以后重新部署只需此命令）
docker compose up -d --build
```

打开 `http://<服务器>:8080`，输入启动日志中的控制台 Token（`atbrains-server/data/server/.token`）即可管理各客户端令牌与属性。

### 2. 接入客户端（`atbrains-clients/`）

```bash
cd atbrains-clients
./install.sh hermes
hermes atbrains-memory tokset "<token>" --url "http://<服务器>:8080"
```

AstrBot、DSH、MCP 与 Python SDK 的接入方式见 [atbrains-clients/README.md](atbrains-clients/README.md)。
