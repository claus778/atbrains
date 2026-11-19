# atbrains

多 Agent 共享长期记忆服务。服务端统一管理 MariaDB、Redis、Qdrant，提供 Web 控制台、HTTP `/v1` 与 `/mcp` 入口；各客户端纯 HTTP 接入，不持有数据库驱动与密码。

## 1. 快速部署（Docker Compose）

```bash
cd server
cp .env.example .env
# 编辑 .env 设置数据库密码与宿主机数据持久化目录
docker compose up -d --build
```

启动后服务包含：
- **Web 控制台 / 统一入口**：`http://<服务器>:8080`（Nginx）
- **后端 API**：`atbrains-server:8787`（支持 `/v1/*` 与 `/mcp`）
- **数据容器**：`mariadb`、`redis`、`qdrant`（数据挂载到宿主机目录持久化保存）

查看管理员初始控制台令牌：

```bash
cat server/data/server/.token
```

## 2. 令牌管理

打开 `http://<服务器>:8080`，输入 `.token`（或任意 `role=admin` 令牌）登录后：
- 按客户端类型（Hermes / AstrBot / DSH / Claude Code / Codex / Cursor / Python SDK / MCP）签发独立 Token；
- 签发后自动生成对应客户端配置片段；
- 实时查看各 Agent 在线状态、来源通道（`v1` / `mcp`）、IP、请求数、记忆分布以及数据库健康状态。

## 3. 客户端接入

| 客户端 | 协议 | 安装 / 接入方式 |
|---|---|---|
| **Hermes** | `/v1` | `./install.sh hermes`，配置 `~/.hermes/atbrains.json` 与 `~/.hermes/.env`（`ATBRAINS_TOKEN`） |
| **AstrBot** | `/v1` | `python3 tools/package-astrbot-client.py` 解压至插件目录，配置 `ATBRAINS_URL` 与 `ATBRAINS_TOKEN` |
| **DSH** | `/v1` | 复制 `clients/dsh-atbrains` 至插件目录，设置 `ATBRAINS_MCP_TOKEN` 与 `cordis.patch.yml` |
| **Claude Code / Codex / Cursor / omp** | `/mcp` | 在 MCP 配置中填入 `http://<服务器>:8080/mcp` 与 `Authorization: Bearer <token>` |
| **Python SDK** | `/v1` | `pip install ./sdk/python`，使用 `AtbrainsClient(base_url=..., token=...)` |

## 4. 文档

- 架构、目录结构、环境变量与测试：[docs/development.md](docs/development.md)
- HTTP `/v1` 协议说明：[docs/protocol-v1.md](docs/protocol-v1.md) / [contracts/openapi.yaml](contracts/openapi.yaml)
- MCP 接入说明：[docs/mcp.md](docs/mcp.md)
- 数据库表结构：[docs/database-schema.md](docs/database-schema.md)
- 多 Agent 与兼容策略：[docs/multi-agent.md](docs/multi-agent.md) / [docs/compat.md](docs/compat.md)
