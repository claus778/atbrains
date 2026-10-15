"""atbrains —— brain 记忆系统的监控与控制(Hermes 插件)。

设计要点(与"系统服务"形态的区别):
  · 没有 systemd 单元:控制台由 `hermes atbrains serve` 按需启动,`stop` 停掉;
  · 命令行/工具/网页三处共用 probe.py 这一个数据层,结果不会漂移;
  · 不声明 pip_dependencies —— 只依赖 stdlib + 兄弟插件 brain 的 vendor 库,
    避免进入 PM 依赖构建(国内镜像缺元数据会导致插件被禁用)。

注册内容:
  · 工具 atbrains_status / atbrains_control(agent 可直接调用)
  · 命令行 hermes atbrains <status|serve|stop|open|drain|doctor|memories|turns|search|remember|forget>
"""
from __future__ import annotations

from . import cli, tools

EMOJI = "🧠"


def register(ctx) -> None:  # noqa: ANN001
    """插件入口:Hermes 加载本插件时调用。"""
    for schema, handler in ((tools.STATUS_SCHEMA, tools.atbrains_status),
                            (tools.CONTROL_SCHEMA, tools.atbrains_control)):
        ctx.register_tool(
            name=schema["name"],
            toolset="atbrains",
            schema=schema,
            handler=handler,
            description=schema["description"],
            emoji=EMOJI,
        )

    ctx.register_cli_command(
        name="atbrains",
        help="记忆系统(brain)监控与控制台",
        setup_fn=cli.setup_parser,
        handler_fn=cli.handle,
        description=("监控与控制 brain 记忆系统(Redis 短期 + MariaDB 长期 + Qdrant 向量)。"
                     "`hermes atbrains status` 一屏总览;`serve` 按需启动网页控制台(非系统服务);"
                     "`drain` 手动回填向量;`search` 混合检索;`remember`/`forget` 增删记忆。"),
    )
