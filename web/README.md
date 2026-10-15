# web/ —— 控制台截图(源码已迁走)

网页控制台的**源代码不在这里**,在 Hermes 插件目录(插件只能从那里被加载):

```
~/.hermes/plugins/atbrains/console/server.py    后端
~/.hermes/plugins/atbrains/console/index.html   前端
```

资料库根目录的 `plugin` 是指向 `~/.hermes/plugins/atbrains` 的软链接,所以从这里也能直接看到源码。

**启动/停止**(插件 CLI,不是系统服务):

```bash
hermes atbrains serve --background   # 启动
hermes atbrains open                 # 打印地址与口令
hermes atbrains stop                 # 停止
```

`preview.png` 是控制台的实际渲染截图(2026-10-15)。
