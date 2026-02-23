#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cookie sessions + password hashing (standard library only).
"""

from __future__ import annotations
import hashlib, hmac, os, secrets
from typing import Dict, Any, Optional, Tuple

from .utils import read_json, write_json, now_iso, require_safe_id

def _pbkdf2(password: str, salt: bytes, rounds: int = 120_000) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)

def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = _pbkdf2(password, salt)
    return "pbkdf2_sha256$120000$" + salt.hex() + "$" + dk.hex()

def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds_s, salt_hex, dk_hex = stored.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        rounds = int(rounds_s)
        salt = bytes.fromhex(salt_hex)
        dk = bytes.fromhex(dk_hex)
        test = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
        return hmac.compare_digest(test, dk)
    except Exception:
        return False

def new_session(sessions_path: str, username: str, role: str, tenant_id: Optional[str]) -> str:
    username = require_safe_id(username, "username")
    sid = secrets.token_urlsafe(24)
    sessions = read_json(sessions_path, {})
    sessions[sid] = {
        "username": username,
        "role": role,
        "tenant_id": tenant_id,
        "created_at": now_iso(),
        "last_seen": now_iso(),
    }
    write_json(sessions_path, sessions)
    return sid

def get_session(sessions_path: str, sid: str) -> Optional[Dict[str, Any]]:
    if not sid:
        return None
    sessions = read_json(sessions_path, {})
    s = sessions.get(sid)
    if not isinstance(s, dict):
        return None
    s["last_seen"] = now_iso()
    sessions[sid] = s
    write_json(sessions_path, sessions)
    return s

def delete_session(sessions_path: str, sid: str) -> None:
    sessions = read_json(sessions_path, {})
    if sid in sessions:
        sessions.pop(sid, None)
        write_json(sessions_path, sessions)

def load_users(users_path: str) -> Dict[str, Any]:
    return read_json(users_path, {"users": []})

def find_user(users_path: str, username: str) -> Optional[Dict[str, Any]]:
    udb = load_users(users_path)
    for u in udb.get("users", []):
        if u.get("username") == username:
            return u
    return None

def upsert_user(users_path: str, user: Dict[str, Any]) -> None:
    udb = load_users(users_path)
    users = udb.get("users", [])
    out = []
    found = False
    for u in users:
        if u.get("username") == user.get("username"):
            out.append(user); found = True
        else:
            out.append(u)
    if not found:
        out.append(user)
    udb["users"] = out
    write_json(users_path, udb)
