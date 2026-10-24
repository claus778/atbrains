"""向后兼容 shim：模块已改名为 ``atbrains_mcp``。

老代码/文档里的 ``import brain_mcp`` 仍然可用；新代码请直接 import atbrains_mcp。
"""
from atbrains_mcp import *  # noqa: F401,F403
from atbrains_mcp import AtbrainsMCP, TOOLS, INSTRUCTIONS, SERVER_INFO  # noqa: F401

AtbrainsMCP = AtbrainsMCP  # 兼容旧名
