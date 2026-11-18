"""atbrains-server —— 通用多 Agent 共享记忆服务端（MariaDB + Redis + Qdrant）。"""
from __future__ import annotations

__version__ = "3.0.0"
PROTOCOL_VERSION = "2.1.0"

from .config import (  # noqa: F401
    DEFAULT_CONFIG,
    KEY_PREFIX,
    PROVIDER_NAME,
    SECRET_ENV_VARS,
    hermes_home_path,
    resolve_config,
    resolve_data_dir,
    resolve_server_http_config,
)
from .store import AtbrainsStore  # noqa: F401
from .util import BrainError, BrainHTTPError  # noqa: F401

__all__ = [
    "__version__",
    "PROTOCOL_VERSION",
    "AtbrainsStore",
    "BrainError",
    "BrainHTTPError",
    "DEFAULT_CONFIG",
    "KEY_PREFIX",
    "PROVIDER_NAME",
    "SECRET_ENV_VARS",
    "hermes_home_path",
    "resolve_config",
    "resolve_data_dir",
    "resolve_server_http_config",
]
