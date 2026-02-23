#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Zero-dependency utilities: atomic JSON, safe paths, tiny templating.
"""

from __future__ import annotations
import json, os, re, tempfile
from typing import Any, Dict

SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")

def ensure_dir(p: str) -> None:
    os.makedirs(p, exist_ok=True)

def atomic_write_bytes(path: str, data: bytes) -> None:
    d = os.path.dirname(path)
    ensure_dir(d)
    fd, tmp = tempfile.mkstemp(prefix=".tmp_", dir=d)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass

def read_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def write_json(path: str, obj: Any) -> None:
    atomic_write_bytes(path, json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8"))

def safe_join(base_dir: str, *parts: str) -> str:
    """
    Prevent directory traversal; returns normalized absolute path inside base_dir.
    """
    base_abs = os.path.abspath(base_dir)
    p = os.path.abspath(os.path.join(base_abs, *parts))
    if not (p == base_abs or p.startswith(base_abs + os.sep)):
        raise ValueError("unsafe path")
    return p

def render_template(text: str, context: Dict[str, Any]) -> str:
    # Very small template: replace {{key}} with string value.
    for k, v in context.items():
        text = text.replace("{{" + k + "}}", str(v))
    return text

def clamp(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[:n] + "…"

def now_iso() -> str:
    import datetime
    return datetime.datetime.now().replace(microsecond=0).isoformat()

def require_safe_id(x: str, what: str = "id") -> str:
    if not x or not SAFE_ID_RE.match(x):
        raise ValueError(f"invalid {what}")
    return x
