# 架构、配置与开发测试指南

## 总体架构

```
  Hermes (clients/hermes-atbrains) ──┐
  AstrBot (clients/astrbot-atbrains) ├─▶ atbrains-client (纯 stdlib SDK + 离线队列)
  自定义 Python Agent ───────────────┘          │
  DSH / Node Agent (clients/dsh-atbrains) ──────┼─▶ HTTP /v1 (OpenAPI v2.1.0)
  Claude / Codex / Cursor / omp ────────────────┴─▶ /mcp (HTTP & stdio)
                                                         │
                                                         ▼
                                         Nginx (8080) → atbrains-server (8787)
                                         ├── api: /v1 · /mcp · Web Console
                                         ├── engine: RRF 召回 · 衰减 · rerank · 图谱 · 蒸馏
                                         ├── schema: 幂等建表与迁移 (v1..v13)
                                         └── 唯一持有 DB 凭据与后台 worker
                                                         │
                                     ┌───────────────────┼───────────────────┐
                                     ▼                   ▼                   ▼
                                 MariaDB               Redis               Qdrant
                              (长期/转录/图谱)      (流/热点/缓存)        (向量检索)
```

## 目录结构

```
atbrains/
├── contracts/openapi.yaml       # /v1 OpenAPI 3.1 契约
├── server/                      # 服务端包、Dockerfile、docker-compose.yml、Nginx 配置
│   ├── atbrains_server/
│   │   ├── config.py            # 配置解析 (ATBRAINS_* > ~/.config/atbrains > /etc/atbrains)
│   │   ├── schema.py            # 表结构与自动迁移
│   │   ├── engine/              # 存储与检索核心模块
│   │   ├── tokens.py            # 令牌与 <machine>|<agent> + client 身份解析
│   │   ├── service.py           # /v1/* 与 /v1/admin/* 路由及连接活动追踪
│   │   ├── mcp.py / mcp_stdio.py# MCP Streamable HTTP 与 stdio
│   │   ├── http.py / console/   # HTTP 服务与纯静态控制台
│   │   └── cli.py               # atbrains-server CLI
│   ├── nginx/default.conf       # Nginx 前端静态页与反代配置
│   └── tests/                   # test_pure.py / test_service.py / test_integration.py
├── sdk/python/                  # atbrains-client 纯标准库 Python SDK（零第三方依赖）
├── clients/                     # 各框架薄客户端（hermes / astrbot / dsh）
├── tools/                       # 客户端打包与 schema 导出脚本
├── ops/                         # 运维脚本与契约校验
└── docs/                        # 协议、表结构、架构与兼容文档
```

## 宿主机 Python 部署

```bash
./install.sh server
.venv/bin/atbrains-server migrate
.venv/bin/atbrains-server token-add laptop|codex
.venv/bin/atbrains-server serve --background
```

## 核心环境变量

| 变量 | 说明 |
|---|---|
| `ATBRAINS_MARIADB_HOST` / `PORT` / `USER` / `DB` / `PASSWORD` | MariaDB 连接凭据 |
| `ATBRAINS_REDIS_HOST` / `PORT` / `DB` / `PASSWORD` | Redis 连接凭据 |
| `ATBRAINS_QDRANT_URL` / `COLLECTION` / `API_KEY` | Qdrant 向量库配置 |
| `ATBRAINS_EMBED_BASE_URL` / `MODEL` / `DIM` / `API_KEY` | 向量 Embedding 接口 |
| `ATBRAINS_DISTILL_ENABLED` / `API_BASE_URL` / `API_MODEL` / `API_KEY` | 可选蒸馏模型配置 |
| `ATBRAINS_DATA_DIR` / `ATBRAINS_PORT` | 服务端数据目录与监听端口（默认 `8787`） |
| `ATBRAINS_WEB_PORT` | Docker Compose Nginx 对外端口（默认 `8080`） |
| `ATBRAINS_*_DATA_DIR` | Docker Compose 宿主机数据目录挂载路径（见 `server/.env.example`） |

## 客户端打包

```bash
python3 tools/package-hermes-client.py
python3 tools/package-astrbot-client.py
```

## 测试

```bash
.venv/bin/python server/tests/test_pure.py
.venv/bin/python server/tests/test_service.py
.venv/bin/python sdk/python/tests/test_client.py
.venv/bin/python clients/hermes-atbrains/tests/test_hermes_adapter.py
.venv/bin/python clients/astrbot-atbrains/tests/test_astrbot_adapter.py
(cd clients/dsh-atbrains && npm test && node test/timestamp.mjs)

# 真库集成测试（需 Docker）
ATBRAINS_DOCKER_IT=1 .venv/bin/python server/tests/test_integration.py
```
