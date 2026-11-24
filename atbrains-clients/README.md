# atbrains-clients

atbrains 共享记忆服务的纯 HTTP 客户端集合（零数据库依赖、零数据库密码），包含纯标准库 Python SDK 以及 Hermes / AstrBot / DSH 适配器插件。

> 本目录可独立分发，不依赖服务端代码；服务端项目位于 `../atbrains-server/`。

## 1. 客户端接入

| 客户端 | 目录 | 安装 / 配置方式 |
|---|---|---|
| **Python SDK** | `sdk/python/` | `pip install ./sdk/python`，通过 `AtbrainsClient(base_url, token)` 调用 `/v1` |
| **Hermes** | `clients/hermes-atbrains/` | `./install.sh hermes`，随后 `hermes atbrains-memory tokset <token> --url <url>` |
| **AstrBot** | `clients/astrbot-atbrains/` | `python3 tools/package-astrbot-client.py` 打包安装，配置 `base_url` 与 `token`（或 `python3 clients/astrbot-atbrains/main.py --tokset <token> --url <url>`） |
| **DSH** | `clients/dsh-atbrains/` | 复制至插件目录并配置 `ATBRAINS_MCP_TOKEN`（或在对话内执行 `/atbrains tokset <token> [url]`） |

## 2. 打包分发

```bash
cd atbrains-clients
python3 tools/package-hermes-client.py
python3 tools/package-astrbot-client.py
```

## 3. 运行全部测试

```bash
cd atbrains-clients
python3 -m unittest discover -s sdk/python/tests -p 'test_*.py' -v
python3 -m unittest discover -s clients/hermes-atbrains/tests -p 'test_*.py' -v
python3 -m unittest discover -s clients/astrbot-atbrains/tests -p 'test_*.py' -v
(cd clients/dsh-atbrains && node --test test/recall.mjs test/timestamp.mjs test/identity.mjs test/archive.mjs test/protocol.mjs test/wiring.mjs)
```
