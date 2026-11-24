"""atbrains-server —— 通用多 Agent 共享记忆服务端（MariaDB + Redis + Qdrant）。"""
from __future__ import annotations

__version__ = "3.0.0"
PROTOCOL_VERSION = "2.1.0"

from .config import (  # noqa: F401
    DEFAULT_CONFIG,
    KEY_PREFIX,
    PROVIDER_NAME,
    SECRET_ENV_VARS,
    config_home_path,
    resolve_config,
    resolve_data_dir,
    resolve_server_http_config,
)
from .context import RequestContext, ScopedStore  # noqa: F401
from .store import AtbrainsStore  # noqa: F401
from .util import BrainError, BrainForbidden, BrainHTTPError  # noqa: F401

__all__ = [
    "__version__",
    "PROTOCOL_VERSION",
    "AtbrainsStore",
    "RequestContext",
    "ScopedStore",
    "BrainError",
    "BrainForbidden",
    "BrainHTTPError",
    "DEFAULT_CONFIG",
    "KEY_PREFIX",
    "PROVIDER_NAME",
    "SECRET_ENV_VARS",
    "config_home_path",
    "resolve_config",
    "resolve_data_dir",
    "resolve_server_http_config",
]
