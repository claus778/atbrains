"""ops 脚本共用引导：优先使用已安装的 `atbrains_server`，否则回退仓库 `server/`。"""
from __future__ import annotations

import sys
from pathlib import Path


def load_server():
    try:
        from atbrains_server.config import resolve_config
        from atbrains_server.store import AtbrainsStore
        return AtbrainsStore, resolve_config
    except ImportError:
        server_dir = Path(__file__).resolve().parent.parent / "server"
        if str(server_dir) not in sys.path:
            sys.path.insert(0, str(server_dir))
        from atbrains_server.config import resolve_config  # noqa: E402
        from atbrains_server.store import AtbrainsStore  # noqa: E402
        return AtbrainsStore, resolve_config
