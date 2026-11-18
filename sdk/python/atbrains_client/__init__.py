"""atbrains-client —— 通用多 Agent 记忆库纯标准库 Python SDK。"""
from __future__ import annotations

__version__ = "1.0.0"

from .client import AtbrainsClient, AtbrainsClientError
from .config import ClientConfig, normalize_base_url, resolve_client_config
from .schemas import TOOL_ALIASES, TOOLS, openai_tool_schemas
from .spool import OfflineSpool

__all__ = [
    "__version__",
    "AtbrainsClient",
    "AtbrainsClientError",
    "ClientConfig",
    "OfflineSpool",
    "TOOL_ALIASES",
    "TOOLS",
    "normalize_base_url",
    "openai_tool_schemas",
    "resolve_client_config",
]
