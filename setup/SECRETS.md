# 机密存放位置(本文件只写「在哪」,不写「是什么」)

> 本目录所有文件已自动脱敏(密钥 → `***REDACTED***`)。
> 真实值**仍然只在原来的位置**,本文件是索引。

## 一览

| 机密 | 存放位置 | 用途 / 备注 |
|---|---|---|
| `BRAIN_REDIS_PASSWORD` | `~/.hermes/.env` | brain 短期层;Redis 以 `--requirepass` 启动 |
| `BRAIN_MARIADB_PASSWORD` | `~/.hermes/.env` | brain 长期层;MariaDB 用户 `agent` |
| 嵌入服务 API Key(硅基流动) | `~/.hermes/.env` | 向量化 `Qwen/Qwen3-Embedding-4B`;控制台里以 key 来源字段可见是否配置 |
| Qdrant 访问口令 | `/root/qdrant/qdrant-config/production.yaml` 的 `service.api_key` | **只读**挂进容器 `/qdrant/config/production.yaml`;容器 env 中无此 key |
| MariaDB root / agent 口令 | `docker inspect mariadb` 的 `MARIADB_ROOT_PASSWORD` / `MARIADB_PASSWORD` | 建容器时注入,持久在容器配置里 |
| Redis 口令(容器侧) | `docker inspect redis` 的启动参数(`--requirepass`) | 与 `.env` 里的必须一致 |
| 各 agent 库账号(omp/dsh/artex) | `/root/agent-db/credentials.txt`(原始台账) | 本目录的 `credentials-LEDGER-scrubbed.txt` 是**脱敏副本** |
| 控制台访问 token | `~/.hermes/plugins/atbrains/console/.token`(权限 600) | 首次启动自动生成。插件形态;旧路径 `/root/atbrains/web/.token` 已随源码迁移作废 |
| 腾讯云 / GitHub / 邮件等 | `~/.hermes/.env` 及各自配置文件 | 与本记忆系统无关 |

## 写 .env 的正确姿势

`.env` 里的值**带引号**。用 shell 的 `source` 读取会让值变形(实测 84→82 字节),
换来一个误导性的 `Access denied` —— 看着像密码错,其实是**读法错**。

```bash
# ✅ 推荐:用 Hermes 自己的写入器
#    hermes_cli.config.save_env_value(...)
# ✅ 或者:Python 解析(保留原样)
python3 -c "
for l in open('/root/.hermes/.env', encoding='utf-8', errors='replace'):
    if l.startswith('BRAIN_MARIADB_PASSWORD'):
        print(l.split('=',1)[1].strip().strip('\"').strip(chr(39))); break"
```

## 打包/外发前的自检

把本目录拷走之前,建议再扫一次有没有密钥残留:

```bash
python3 - <<'PY'
import os, re, subprocess
# 收集活配置里的密钥值
sec = set()
for l in open('/root/.hermes/.env', encoding='utf-8', errors='replace'):
    if '=' in l and re.match(r'\s*[A-Z_]*(PASSWORD|KEY|TOKEN|SECRET)', l):
        sec.add(l.split('=', 1)[1].strip().strip('"').strip("'"))
for c in ('mariadb', 'redis', 'qdrant'):
    out = subprocess.run(['docker','inspect',c,'--format','{{range .Config.Env}}{{println .}}{{end}}'],
                         capture_output=True, text=True).stdout
    for l in out.splitlines():
        if re.search(r'(PASSWORD|API_KEY|TOKEN|SECRET)=', l, re.I):
            sec.add(l.split('=', 1)[1])
sec = {s for s in sec if len(s) >= 8}

bad = [os.path.join(d, f) for d, _, fs in os.walk('/root/atbrains') for f in fs
       for s in sec if s in open(os.path.join(d, f), encoding='utf-8', errors='replace').read()]
print('❌ 残留:' , bad) if bad else print('✅ 干净,无密钥残留')
PY
```

## 额外提醒

- **别把 `.token` 写进任何文档或聊天记录**;它等价于控制台的钥匙。
- 控制台默认监听 `0.0.0.0:8787`。若不打算开放安全组,请走 SSH 隧道:
  `ssh -p 7622 -L 8787:127.0.0.1:8787 root@<服务器>`
- Qdrant 的 `6333/6334` 目前**未在腾讯云安全组放行** —— 这是有意为之,保持现状。
- `credentials-LEDGER-scrubbed.txt` 里的口令位置已被 `***REDACTED***` 取代,
  **不要**把它当作可用的凭据清单;需要真实值请查原始台账。
