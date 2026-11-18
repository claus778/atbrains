# atbrains · 通用多 Agent 共享记忆库

> **后端唯一持库，前端纯 HTTP 解耦**：一套服务端 (`atbrains-server`) 管理 MariaDB（长期/知识/转录/图谱）+ Redis（短期/热点/缓存）+ Qdrant（语义向量）；所有 Agent 前端（Hermes、AstrBot、DSH、Claude Code、Codex、Cursor、自定义脚本）一律通过 `/v1` REST API 或 `/mcp` 接入，**不再在任何客户端保存数据库密码或驱动**。

---

## 一、总体架构

```
  Hermes (clients/hermes-atbrains) ──┐
  AstrBot (clients/astrbot-atbrains) ├─▶ atbrains-client (纯 stdlib SDK + 离线队列)
  自定义 Python Agent ───────────────┘          │
  DSH / Node Agent (clients/dsh-atbrains) ──────┼─▶ HTTP /v1 (OpenAPI v2.1.0)
  Claude / Codex / Cursor ──────────────────────┴─▶ /mcp (HTTP & stdio)
                                                         │
                                                         ▼
                                            atbrains-server (server/)
                                            ├── api: /v1 · /mcp · Web Console
                                            ├── engine: 召回RRF · 衰减 · rerank · 图谱 · 蒸馏
                                            ├── schema: 幂等建表与版本迁移 (v1..v12)
                                            └── 唯一持有 DB 凭据与单实例后台 worker
                                                         │
                                     ┌───────────────────┼───────────────────┐
                                     ▼                   ▼                   ▼
                                 MariaDB               Redis               Qdrant
                              (长期/转录/图谱)      (流/热点/缓存)        (2560维向量)
```

---

## 二、目录结构

```
atbrains/
├── contracts/
│   └── openapi.yaml             ← /v1 协议 OpenAPI 3.1 契约 (v2.1.0)
├── server/                      ← atbrains-server 独立包 (v3.0.0，唯一碰数据库)
│   ├── pyproject.toml
│   ├── atbrains_server/
│   │   ├── config.py            ← 通用配置 (ATBRAINS_* > ~/.config/atbrains > /etc/atbrains)
│   │   ├── schema.py            ← DDL + 迁移 v1..v12
│   │   ├── engine/              ← 模块化存储引擎 (base/memories/knowledge/transcript/recall/vectors/curation/graph/maintenance/distill)
│   │   ├── tools_registry.py    ← 12 个通用工具的唯一定义源
│   │   ├── tokens.py            ← Bearer 令牌与身份/权限 (`<machine>|<agent>`, role=agent|admin)
│   │   ├── service.py           ← /v1/* + /v1/admin/* 路由层
│   │   ├── mcp.py / mcp_stdio.py← MCP Streamable HTTP & stdio
│   │   ├── http.py / console/   ← 统一 HTTP 服务与网页控制台
│   │   └── cli.py               ← `atbrains-server` 命令行
│   └── tests/                   ← test_pure.py · test_service.py · test_integration.py
├── sdk/python/                  ← atbrains-client 纯标准库 Python SDK（零第三方依赖）
│   └── atbrains_client/
│       ├── client.py            ← 完整 /v1 客户端 + keep-alive + 召回缓存
│       ├── spool.py             ← 有界 JSONL 离线队列（断网缓冲，恢复重放）
│       ├── schemas.py           ← 通用工具 JSON Schema
│       └── mcp_clients.py       ← MCP 客户端一键配置生成器
├── clients/                     ← 各 Agent 薄适配器（全部纯 HTTP，零 DB 凭据/驱动）
│   ├── hermes-atbrains/         ← Hermes MemoryProvider 适配器
│   ├── astrbot-atbrains/        ← AstrBot 插件
│   └── dsh-atbrains/            ← DeepSeek Harness Node/Cordis 插件
├── deploy/                      ← Docker Compose / Dockerfile / systemd / schema.sql / init.sql
├── ops/                         ← 运维审计脚本 & `/v1` 契约测试 (contract-v1.py)
├── tools/                       ← 客户端打包与 schema 导出脚本
└── docs/                        ← 协议契约、兼容矩阵、数据库字典、设计文档
```

---

## 三、快速开始

### 1. 启动服务端（推荐 Docker Compose 或 venv）

**方式 A：Docker Compose 一键拉起（MariaDB + Redis + Qdrant + atbrains-server）**
```bash
cd deploy
ATBRAINS_MARIADB_PASSWORD=your-db-pass ATBRAINS_REDIS_PASSWORD=your-redis-pass \
  docker compose up -d
```

**方式 B：Python 环境安装运行**
```bash
./install.sh server
.venv/bin/atbrains-server migrate         # 显式初始化/迁移数据库表结构
.venv/bin/atbrains-server token-add laptop|codex
.venv/bin/atbrains-server serve --background
```

核心环境变量（无需绑定任何特定 Agent 框架）：
- `ATBRAINS_MARIADB_HOST` / `ATBRAINS_MARIADB_PORT` / `ATBRAINS_MARIADB_USER` / `ATBRAINS_MARIADB_DB` / `ATBRAINS_MARIADB_PASSWORD`
- `ATBRAINS_REDIS_HOST` / `ATBRAINS_REDIS_PORT` / `ATBRAINS_REDIS_DB` / `ATBRAINS_REDIS_PASSWORD`
- `ATBRAINS_QDRANT_URL` / `ATBRAINS_QDRANT_API_KEY` / `ATBRAINS_EMBED_API_KEY`
- `ATBRAINS_HOME` / `ATBRAINS_DATA_DIR` / `ATBRAINS_PORT` (默认 `8787`)

### 2. 接入任意 Agent

**Python SDK（通用）**：
```bash
pip install ./sdk/python
```
```python
from atbrains_client import AtbrainsClient

cli = AtbrainsClient(
    base_url="http://127.0.0.1:8787",
    token="<token>",
    spool_dir="~/.local/share/atbrains/spool",  # 断网自动写本地队列
)
cli.remember("部署端口固定为 8787", kind="fact", importance=8)
ctx = cli.context(focus="端口", fmt="markdown")
```

**MCP 客户端（Claude Code / Codex / Cursor / Claude Desktop）**：
```bash
.venv/bin/atbrains-server mcp install claude-code --transport http --agent claude --write
# 或本机 stdio：
.venv/bin/atbrains-server mcp install codex --agent codex --write
```

**打包独立客户端**：
- Hermes：`./install.sh hermes` 或 `python3 tools/package-hermes-client.py`
- AstrBot：`python3 tools/package-astrbot-client.py`
- DSH：见 [`clients/dsh-atbrains/README.md`](clients/dsh-atbrains/README.md)

---

## 四、测试与 Git 版本管理

```bash
# 运行全部纯单元测试、契约测试与适配器测试
.venv/bin/python server/tests/test_pure.py
.venv/bin/python server/tests/test_service.py
.venv/bin/python sdk/python/tests/test_client.py
.venv/bin/python clients/hermes-atbrains/tests/test_hermes_adapter.py
.venv/bin/python clients/astrbot-atbrains/tests/test_astrbot_adapter.py
(cd clients/dsh-atbrains && npm test && node test/timestamp.mjs)

# 真库集成测试（自动启停临时 MariaDB + Redis + Qdrant 容器）
ATBRAINS_DOCKER_IT=1 .venv/bin/python server/tests/test_integration.py
```

Git 规范见 [`docs/compat.md`](docs/compat.md)（按组件前缀打 tag：`server-v*` / `protocol-v*` / `sdk-v*` / `hermes-v*` / `astrbot-v*` / `dsh-v*`，第三方依赖不入库，`.github/workflows/ci.yml` 自动跑全量检查）。
