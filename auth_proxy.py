#!/usr/bin/env python3
"""
auth_proxy.py - Authentication reverse proxy for Quantra Terminal.

Runs on port 8080 (public). Proxies authenticated requests to ws_server.py
on port 8081 (internal). ZERO modifications to ws_server.py required.

Features:
  - Cookie-based session auth (HttpOnly, SameSite=Strict)
  - Password hashed with SHA-256 + salt
  - Serves login.html directly for /login
  - Proxies all other routes (HTTP + WebSocket) to backend
  - Sessions expire after configurable timeout (default 24h)
  - Multiple users supported via auth_config.json

Deploy:
  1. Change ws_server.py to listen on 8081 (one-line change)
  2. Start auth_proxy.py on 8080:
     nohup venv/bin/python3 auth_proxy.py >> auth_proxy.log 2>&1 &

Rollback:
  1. Kill auth_proxy.py
  2. Change ws_server.py back to 8080
  3. Restart ws_server.py
"""

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import time
import logging

from aiohttp import web, ClientSession, WSMsgType

# ============================================================
# CONFIG
# ============================================================

PROXY_PORT = 8080
BACKEND_HOST = "127.0.0.1"
BACKEND_PORT = 8081
CONFIG_FILE = "auth_config.json"
SESSION_COOKIE = "quantra_session"
SESSION_MAX_AGE = 86400  # 24 hours

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [AUTH] %(levelname)s %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("auth_proxy")

# ============================================================
# AUTH STATE
# ============================================================

_sessions = {}  # token -> {user, created, last_seen}
_config = None


def load_config():
    """Load auth config from JSON file."""
    global _config
    if not os.path.exists(CONFIG_FILE):
        log.error(f"{CONFIG_FILE} not found. Run setup_auth.py first.")
        raise SystemExit(1)
    with open(CONFIG_FILE, "r") as f:
        _config = json.load(f)
    log.info(f"Loaded config: {len(_config.get('users', []))} user(s)")
    return _config


def hash_password(password, salt):
    """SHA-256 hash with salt."""
    return hashlib.sha256((salt + password).encode()).hexdigest()


def verify_password(username, password):
    """Check password against stored hash."""
    for user in _config.get("users", []):
        if user["username"] == username:
            hashed = hash_password(password, user["salt"])
            return hmac.compare_digest(hashed, user["hash"])
    return False


def create_session(username):
    """Create a new session token."""
    token = secrets.token_hex(32)
    _sessions[token] = {
        "user": username,
        "created": time.time(),
        "last_seen": time.time()
    }
    log.info(f"Session created for {username}")
    return token


def validate_session(token):
    """Validate session token. Returns username or None."""
    if not token or token not in _sessions:
        return None
    session = _sessions[token]
    age = time.time() - session["created"]
    max_age = _config.get("session_max_age", SESSION_MAX_AGE)
    if age > max_age:
        del _sessions[token]
        log.info(f"Session expired for {session['user']}")
        return None
    session["last_seen"] = time.time()
    return session["user"]


def get_session_from_request(request):
    """Extract session token from cookie."""
    return request.cookies.get(SESSION_COOKIE)


def cleanup_sessions():
    """Remove expired sessions."""
    max_age = _config.get("session_max_age", SESSION_MAX_AGE)
    now = time.time()
    expired = [t for t, s in _sessions.items() if now - s["created"] > max_age]
    for t in expired:
        del _sessions[t]
    if expired:
        log.info(f"Cleaned up {len(expired)} expired sessions")


# ============================================================
# AUTH ENDPOINTS
# ============================================================

async def handle_login_page(request):
    """Serve login.html."""
    # If already authenticated, redirect to dashboard
    token = get_session_from_request(request)
    if validate_session(token):
        raise web.HTTPFound("/")

    login_file = os.path.join(os.path.dirname(__file__) or ".", "login.html")
    if not os.path.exists(login_file):
        return web.Response(text="Login page not found", status=500)
    with open(login_file, "r") as f:
        html = f.read()
    return web.Response(text=html, content_type="text/html")


async def handle_login_api(request):
    """POST /api/auth/login - authenticate and set session cookie."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    username = data.get("username", "").strip()
    password = data.get("password", "")

    if not username or not password:
        return web.json_response({"error": "Username and password required"}, status=400)

    if not verify_password(username, password):
        log.warning(f"Failed login attempt for '{username}' from {request.remote}")
        return web.json_response({"error": "Invalid credentials"}, status=401)

    token = create_session(username)
    max_age = _config.get("session_max_age", SESSION_MAX_AGE)

    resp = web.json_response({"ok": True, "user": username})
    resp.set_cookie(
        SESSION_COOKIE, token,
        max_age=max_age,
        httponly=True,
        samesite="Strict",
        path="/"
    )
    return resp


async def handle_verify_api(request):
    """GET /api/auth/verify - check if current session is valid."""
    token = get_session_from_request(request)
    user = validate_session(token)
    if user:
        return web.json_response({"ok": True, "user": user})
    return web.json_response({"ok": False}, status=401)


async def handle_logout_api(request):
    """GET /api/auth/logout - destroy session and redirect to login."""
    token = get_session_from_request(request)
    if token and token in _sessions:
        user = _sessions[token]["user"]
        del _sessions[token]
        log.info(f"Logout: {user}")

    resp = web.HTTPFound("/login")
    resp.del_cookie(SESSION_COOKIE, path="/")
    return resp


# ============================================================
# REVERSE PROXY
# ============================================================

async def proxy_websocket(request):
    """Proxy WebSocket connections to backend."""
    token = get_session_from_request(request)
    if not validate_session(token):
        return web.Response(text="Unauthorized", status=401)

    ws_client = web.WebSocketResponse()
    await ws_client.prepare(request)

    backend_url = f"ws://{BACKEND_HOST}:{BACKEND_PORT}{request.path_qs}"

    async with ClientSession() as session:
        try:
            async with session.ws_connect(backend_url) as ws_backend:
                async def forward_to_client():
                    async for msg in ws_backend:
                        if msg.type == WSMsgType.TEXT:
                            await ws_client.send_str(msg.data)
                        elif msg.type == WSMsgType.BINARY:
                            await ws_client.send_bytes(msg.data)
                        elif msg.type == WSMsgType.CLOSE:
                            await ws_client.close()
                            break
                        elif msg.type == WSMsgType.ERROR:
                            break

                async def forward_to_backend():
                    async for msg in ws_client:
                        if msg.type == WSMsgType.TEXT:
                            await ws_backend.send_str(msg.data)
                        elif msg.type == WSMsgType.BINARY:
                            await ws_backend.send_bytes(msg.data)
                        elif msg.type == WSMsgType.CLOSE:
                            await ws_backend.close()
                            break
                        elif msg.type == WSMsgType.ERROR:
                            break

                await asyncio.gather(
                    forward_to_client(),
                    forward_to_backend(),
                    return_exceptions=True
                )
        except Exception as e:
            log.error(f"WebSocket proxy error: {e}")
            if not ws_client.closed:
                await ws_client.close()

    return ws_client


async def proxy_http(request):
    """Proxy HTTP requests to backend."""
    # Auth check — skip for static assets and login routes
    path = request.path
    public_paths = {"/login", "/api/auth/login", "/api/auth/verify", "/api/auth/logout"}
    if path not in public_paths:
        token = get_session_from_request(request)
        if not validate_session(token):
            raise web.HTTPFound("/login")

    # Build backend URL
    backend_url = f"http://{BACKEND_HOST}:{BACKEND_PORT}{request.path_qs}"

    # Forward headers (strip hop-by-hop)
    headers = {}
    skip_headers = {"host", "connection", "transfer-encoding", "keep-alive"}
    for key, val in request.headers.items():
        if key.lower() not in skip_headers:
            headers[key] = val

    # Read request body
    body = await request.read() if request.can_read_body else None

    async with ClientSession() as session:
        try:
            async with session.request(
                request.method, backend_url,
                headers=headers,
                data=body,
                allow_redirects=False
            ) as resp:
                # Build response
                response_headers = {}
                skip_resp_headers = {"content-encoding", "transfer-encoding", "connection"}
                for key, val in resp.headers.items():
                    if key.lower() not in skip_resp_headers:
                        response_headers[key] = val

                response_body = await resp.read()
                return web.Response(
                    status=resp.status,
                    headers=response_headers,
                    body=response_body
                )
        except Exception as e:
            log.error(f"Backend proxy error: {e}")
            return web.Response(
                text=f"Backend unavailable: {e}",
                status=502
            )


# ============================================================
# APP SETUP
# ============================================================

async def session_cleanup_task(app):
    """Periodic session cleanup."""
    while True:
        await asyncio.sleep(300)  # Every 5 minutes
        cleanup_sessions()


async def on_startup(app):
    app["cleanup_task"] = asyncio.create_task(session_cleanup_task(app))
    log.info(f"Auth proxy started on :{PROXY_PORT}, backend at {BACKEND_HOST}:{BACKEND_PORT}")


async def on_shutdown(app):
    app["cleanup_task"].cancel()
    log.info("Auth proxy shutting down")


def create_app():
    load_config()

    app = web.Application()

    # Auth routes (handled directly, not proxied)
    app.router.add_get("/login", handle_login_page)
    app.router.add_post("/api/auth/login", handle_login_api)
    app.router.add_get("/api/auth/verify", handle_verify_api)
    app.router.add_get("/api/auth/logout", handle_logout_api)

    # WebSocket proxy
    app.router.add_get("/ws", proxy_websocket)

    # Catch-all HTTP proxy (must be last)
    app.router.add_route("*", "/{path:.*}", proxy_http)

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    return app


if __name__ == "__main__":
    app = create_app()
    web.run_app(app, host="0.0.0.0", port=PROXY_PORT)
