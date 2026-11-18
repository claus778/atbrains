"""出站 HTTP：私网直连、向量服务凭据。"""
from __future__ import annotations

import json
import logging
from pathlib import Path
import re
import time
from typing import Any, Optional

from ..config import default_qdrant_config_candidates
from ..util import BrainHTTPError

logger = logging.getLogger("atbrains")

_NO_PROXY_OPENER = None
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024


def _is_private_host(host: str) -> bool:
    host = str(host or "").strip().strip("[]").lower()
    if host in ("127.0.0.1", "localhost", "::1", "0.0.0.0"):
        return True
    parts = host.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        a, b = int(parts[0]), int(parts[1])
        if a == 10 or (a == 192 and b == 168) or (a == 172 and 16 <= b <= 31):
            return True
    return False


class NetMixin:
    """出站 HTTP：私网直连与向量服务凭据。"""

    def _vector_secrets(self) -> dict[str, str]:
        """向量层凭据：优先 cfg/环境变量（qdrant_api_key / embed_api_key），回落 Qdrant yaml。"""
        now = time.time()
        if self._vsec is not None and now - self._vsec_ts < 300:
            return self._vsec
        qkey = str(self.cfg.get("qdrant_api_key") or "").strip()
        ekey = str(self.cfg.get("embed_api_key") or "").strip()
        if not (qkey and ekey):
            raw_path = str(self.cfg.get("qdrant_config_file") or "").strip()
            candidates = [Path(raw_path).expanduser()] if raw_path else list(default_qdrant_config_candidates())
            yaml_key = ""
            for p in candidates:
                if not p.is_file():
                    continue
                try:
                    text = p.read_text(encoding="utf-8", errors="replace")
                    section = ""
                    for raw in text.splitlines():
                        line = raw.split("#", 1)[0].rstrip()
                        if not line.strip():
                            continue
                        if len(line) - len(line.lstrip()) == 0:
                            section = line.strip().rstrip(":")
                        elif section == "service":
                            m = re.match(r"\s*api_key\s*:\s*(.+?)\s*$", line)
                            if m:
                                val = m.group(1).strip().strip('"').strip("'")
                                if val and val.lower() not in ("null", "~"):
                                    yaml_key = val
                                    break
                except Exception as exc:  # noqa: BLE001
                    logger.debug("atbrains: 读取 qdrant 配置文件 %s 失败: %s", p, exc)
                if yaml_key:
                    break
            qkey = qkey or yaml_key
            ekey = ekey or yaml_key
        out: dict[str, str] = {}
        if qkey:
            out["qdrant"] = qkey
        if ekey:
            out["embed"] = ekey
        self._vsec, self._vsec_ts = out, now
        return out

    def _http_json(self, method: str, url: str, *, headers: Optional[dict] = None,
                   payload: Optional[dict] = None, timeout: float = 15,
                   no_proxy: Optional[bool] = None) -> tuple[int, dict]:
        import urllib.error
        import urllib.parse
        import urllib.request
        global _NO_PROXY_OPENER
        if no_proxy is None:
            host = urllib.parse.urlsplit(url).hostname or ""
            no_proxy = _is_private_host(host)
        opener = None
        if no_proxy:
            if _NO_PROXY_OPENER is None:
                _NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            opener = _NO_PROXY_OPENER
        ua = str(self.cfg.get("http_user_agent") or "atbrains-server/3.0")
        hdrs = {"Accept": "application/json", "User-Agent": ua}
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with (opener.open(req, timeout=timeout) if opener is not None
                  else urllib.request.urlopen(req, timeout=timeout)) as resp:
                raw = resp.read(_MAX_RESPONSE_BYTES + 1)
                if len(raw) > _MAX_RESPONSE_BYTES:
                    raise ValueError(f"upstream response exceeds {_MAX_RESPONSE_BYTES} bytes")
                body = raw.decode("utf-8", "replace")
                return int(resp.status), (json.loads(body) if body.strip() else {})
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:300]
            if int(exc.code) == 401:
                self._vsec, self._vsec_ts = None, 0.0
            raise BrainHTTPError(int(exc.code), url, body) from exc
