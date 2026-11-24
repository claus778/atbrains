# web/ —— 控制台预览图

网页控制台的**源代码在仓库里**:`../plugin/console/`

```
plugin/console/server.py    后端(HTTP + token 鉴权,数据走 ../probe.py)
plugin/console/index.html   前端(单文件,无外部依赖)
```

代码改完用仓库根目录的 `./install.sh` 安装到 `~/.hermes/plugins/atbrains`
(Hermes 只从插件目录加载;安装副本勿直接改)。

**启动/停止**(插件 CLI,不是系统服务):

```bash
hermes atbrains serve --background   # 启动
hermes atbrains open                 # 打印地址与口令
hermes atbrains stop                 # 停止
```

预览图(2026-10-15,控制台 v1.1.0):

- `preview.png`        首页:卡片总览 + 记忆面板(过滤/分页/行内停用·恢复)
- `preview-recent.png` 动态页:Redis 短期层实时视图(热点记忆 + 最近对话流)
