# 组件版本与协议兼容矩阵

atbrains 使用 monorepo 管理服务端、通用 SDK 与各 Agent 前端适配器，并按组件前缀使用独立 Git Tag：

| 组件 | Git Tag 格式 | 当前版本 | 要求 `/v1` 协议主版本 | 说明 |
|---|---|---|---|---|
| 协议契约 (`contracts/openapi.yaml`) | `protocol-vX.Y.Z` | `2.1.0` | `2` | 主版本变更 = 破坏性变更 |
| 服务端 (`server/`) | `server-vX.Y.Z` | `3.0.0` | 提供 `2.1.0` | 唯一访问 MariaDB / Redis / Qdrant 的后端 |
| Python SDK (`sdk/python/`) | `sdk-vX.Y.Z` | `1.0.0` | `2` | 纯标准库 HTTP 客户端 + JSONL 离线队列 |
| Hermes 适配器 (`clients/hermes-atbrains/`) | `hermes-vX.Y.Z` | `3.0.0` | `2` | 纯 HTTP 客户端，不再包含 `core.py` 或 DB 驱动 |
| AstrBot 适配器 (`clients/astrbot-atbrains/`) | `astrbot-vX.Y.Z` | `2.0.0` | `2` | 纯 HTTP 客户端，移除 `vendor/pymysql,redis` |
| DSH 插件 (`clients/dsh-atbrains/`) | `dsh-vX.Y.Z` | `0.4.0` | `2` | 纯 Node HTTP 插件 |

## Git 分支与发布约定

- `main`：受保护主分支，只合入通过 CI 的 PR / 特性分支。
- `refactor/*` / `feat/*` / `fix/*`：按阶段或特性独立分支开发，使用 Conventional Commits（如 `feat(server): ...`、`fix(sdk): ...`）。
- 第三方依赖包（`vendor/`、`node_modules/`、`.venv/`）与运行时机密（`service-tokens.json`、`.token`、`.env`）绝不入库。
