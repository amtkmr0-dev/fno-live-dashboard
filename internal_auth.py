#!/usr/bin/env python3
"""
internal_auth.py — Shared-secret authentication between auth_proxy and ws_server.

Purpose
-------
The reverse proxy (auth_proxy.py, port 8080) authenticates the user, then
forwards the request to ws_server.py (port 8081). Until now ws_server trusted
``X-User-Id`` / ``X-User-Role`` headers blindly, so anyone reaching port 8081
could impersonate any user.

This module adds an HMAC-signed identity header that ws_server verifies before
honoring user/role claims. Tampering with the headers invalidates the signature.

Wire format
-----------
  X-Internal-Auth: <user_id>|<role>|<username>|<expiry_unix>|<hmac_hex>

The proxy stamps this header on every proxied request when it has a session.
ws_server verifies the signature, expiry, and uses the contained user_id/role
in place of the unsigned ``X-User-Id``/``X-User-Role`` headers.

The shared secret comes from the ``INTERNAL_AUTH_SECRET`` environment variable.
If unset, both sides fall back to a generated secret (for local single-host
dev only — printed once at startup so the user can copy it into config.env).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time

INTERNAL_AUTH_HEADER = "X-Internal-Auth"
DEFAULT_TTL_SECONDS = 60  # short-lived: header is regenerated per request


_runtime_secret: str | None = None


def get_secret() -> str:
    """Return the shared secret, generating a process-local one if needed."""
    global _runtime_secret
    env = os.environ.get("INTERNAL_AUTH_SECRET", "").strip()
    if env:
        return env
    if _runtime_secret is None:
        _runtime_secret = secrets.token_hex(32)
        # Caller is expected to log this. We don't print here to keep the
        # module side-effect-free.
    return _runtime_secret


def is_using_runtime_secret() -> bool:
    """True if no INTERNAL_AUTH_SECRET was provided and we generated one."""
    return not bool(os.environ.get("INTERNAL_AUTH_SECRET", "").strip())


def sign(user_id: int, role: str, username: str, ttl: int = DEFAULT_TTL_SECONDS) -> str:
    """Build the signed X-Internal-Auth header value."""
    expiry = int(time.time()) + int(ttl)
    payload = f"{int(user_id)}|{role}|{username}|{expiry}"
    sig = hmac.new(get_secret().encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}|{sig}"


def verify(header_value: str | None) -> dict | None:
    """
    Verify a signed header. Returns ``{"user_id", "role", "username"}`` on
    success or ``None`` on any failure (missing, malformed, bad signature,
    expired).
    """
    if not header_value:
        return None
    try:
        user_id_s, role, username, expiry_s, sig = header_value.split("|", 4)
    except ValueError:
        return None
    if not sig or not user_id_s.isdigit() or not expiry_s.isdigit():
        return None
    if int(expiry_s) < int(time.time()):
        return None
    payload = f"{user_id_s}|{role}|{username}|{expiry_s}"
    expected = hmac.new(get_secret().encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        user_id = int(user_id_s)
    except ValueError:
        return None
    return {"user_id": user_id, "role": role, "username": username}
