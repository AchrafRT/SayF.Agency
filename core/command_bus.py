#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Command Bus: write deterministic command files into tenant inbox.
"""

from __future__ import annotations
import json, os, uuid
from typing import Dict, Any, Tuple

from .utils import ensure_dir, atomic_write_bytes, now_iso, require_safe_id

def write_command(tenant_dir: str, cmd: str, payload: Dict[str, Any]) -> str:
    cmd = require_safe_id(cmd, "cmd")
    cid = uuid.uuid4().hex[:12].upper()
    inbox = os.path.join(tenant_dir, "inbox")
    ensure_dir(inbox)
    cmd_path = os.path.join(inbox, f"CMD_{now_iso().replace(':','').replace('-','')}_{cid}.json")
    body = {
        "cmd": cmd,
        "created_at": now_iso(),
        "payload": payload,
    }
    atomic_write_bytes(cmd_path, json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8"))
    return cmd_path
