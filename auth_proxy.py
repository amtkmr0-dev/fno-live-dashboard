#!/usr/bin/env python3
"""
auth_proxy.py - Authentication reverse proxy for Quantra Terminal.

Runs on port 8080 (public). Proxies authenticated requests to ws_server.py
on port 8081 (internal). ZERO modifications to ws_server.py required.

Features:
  - Cookie-based session auth (HttpOnly, SameSite=Strict)
  - Role-based access: "admin" (full) vs "user" (restricted)
  - Admin-only pages: /admin, /divergence (configurable in auth_config.json)
  - Password hashed with SHA-256 + salt
  - Serves login.html directly for /login
  - Proxies all other routes (HTTP + WebSocket) to backend
  - Sessions expire after configurable timeout (default 24h)
  - AI chat via Perplexity API (sonar model, web search enabled)
  - Chat context includes live dashboard data for smarter answers

Deploy:
  1. Change ws_server.py to listen on 8081
  2. python3 setup_auth.py amit MyPass admin
  3. python3 setup_auth.py guest ViewPass user
  4. nohup venv/bin/python3 auth_proxy.py >> auth_proxy.log 2>&1 &

Rollback:
  pkill -f auth_proxy.py; python3 switch_port.py 8080; pkill -f ws_server; sleep 2; nohup venv/bin/python3 ws_server.py >> server.log 2>&1 &
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
DEFAULT_ADMIN_PATHS = ["/admin", "/divergence"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [AUTH] %(levelname)s %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("auth_proxy")

# ============================================================
# AUTH STATE
# ============================================================

_sessions = {}  # token -> {user, role, created, last_seen}
_config = None


def load_config():
    """Load auth config from JSON file."""
    global _config
    if not os.path.exists(CONFIG_FILE):
        log.error(f"{CONFIG_FILE} not found. Run setup_auth.py first.")
        raise SystemExit(1)
    with open(CONFIG_FILE, "r") as f:
        _config = json.load(f)
    users = _config.get("users", [])
    admin_paths = _config.get("admin_paths", DEFAULT_ADMIN_PATHS)
    log.info(f"Loaded config: {len(users)} user(s), admin paths: {admin_paths}")
    return _config


def hash_password(password, salt):
    """SHA-256 hash with salt."""
    return hashlib.sha256((salt + password).encode()).hexdigest()


def get_user_record(username):
    """Find user record by username."""
    for user in _config.get("users", []):
        if isinstance(user, dict) and user.get("username") == username:
            return user
    return None


def verify_password(username, password):
    """Check password against stored hash. Returns user record or None."""
    user = get_user_record(username)
    if not user:
        return None
    hashed = hash_password(password, user["salt"])
    if hmac.compare_digest(hashed, user["hash"]):
        return user
    return None


def create_session(username, role):
    """Create a new session token."""
    token = secrets.token_hex(32)
    _sessions[token] = {
        "user": username,
        "role": role,
        "created": time.time(),
        "last_seen": time.time()
    }
    log.info(f"Session created for {username} (role={role})")
    return token


def validate_session(token):
    """Validate session token. Returns session dict or None."""
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
    return session


def get_session_from_request(request):
    """Extract session token from cookie."""
    return request.cookies.get(SESSION_COOKIE)


def is_admin_path(path):
    """Check if path requires admin role."""
    admin_paths = _config.get("admin_paths", DEFAULT_ADMIN_PATHS)
    return path in admin_paths


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
    token = get_session_from_request(request)
    session = validate_session(token)
    if session:
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

    user_record = verify_password(username, password)
    if not user_record:
        log.warning(f"Failed login attempt for '{username}' from {request.remote}")
        return web.json_response({"error": "Invalid credentials"}, status=401)

    role = user_record.get("role", "admin")
    token = create_session(username, role)
    max_age = _config.get("session_max_age", SESSION_MAX_AGE)

    resp = web.json_response({
        "ok": True,
        "user": username,
        "role": role
    })
    resp.set_cookie(
        SESSION_COOKIE, token,
        max_age=max_age,
        httponly=True,
        samesite="Strict",
        path="/"
    )
    return resp


async def handle_verify_api(request):
    """GET /api/auth/verify - check session + return role."""
    token = get_session_from_request(request)
    session = validate_session(token)
    if session:
        admin_paths = _config.get("admin_paths", DEFAULT_ADMIN_PATHS)
        return web.json_response({
            "ok": True,
            "user": session["user"],
            "role": session["role"],
            "admin_paths": admin_paths
        })
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
# AI CHAT (Perplexity API)
# ============================================================

PERPLEXITY_URL = "https://api.perplexity.ai/chat/completions"
PERPLEXITY_MODEL = "sonar"  # fast, web-search enabled

CHAT_SYSTEM_PROMPT = """You are Quantra AI, the built-in assistant for the Quantra Terminal - an F&O (Futures & Options) analytics platform for the Indian stock market (NSE).

Your expertise:
- NIFTY and stock options (CE/PE), option chains, OI analysis, PCR, IV
- Technical analysis: RSI, MACD, EMA, support/resistance, volume
- OI buildup patterns: Long Build, Short Build, Long Unwind, Short Cover
- F&O trading strategies: intraday, positional, hedging
- Indian market terminology and conventions (lot sizes, expiry, etc.)

Guidelines:
- Be direct and actionable - like a trading desk colleague
- Use Indian market terminology (CE/PE, ATM, lot size, expiry)
- Cite specific numbers when available
- If asked about a specific stock, focus on its F&O data and setup
- Keep responses concise (2-4 paragraphs max unless asked for detail)
- Never give guaranteed predictions - always frame as probability/setup
- Mention risk management (SL levels, position sizing) when relevant
- NIFTY lot size is 65 (not 75)
- For stock-specific questions, note that premium >= Rs 15 is the minimum for tradeable options"""


def get_perplexity_key():
    """Get Perplexity API key from config."""
    return _config.get("perplexity_api_key", "")


async def handle_chat_api(request):
    """POST /api/chat - AI chat via Perplexity."""
    # Auth check
    token = get_session_from_request(request)
    session = validate_session(token)
    if not session:
        return web.json_response({"error": "Unauthorized"}, status=401)

    api_key = get_perplexity_key()
    if not api_key:
        return web.json_response({
            "reply": "Chat not configured. Admin needs to set the Perplexity API key.\n\nGo to the chat setup or ask your admin to run:\npython3 setup_auth.py --set-chat-key <your-perplexity-api-key>"
        })

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    user_message = data.get("message", "").strip()
    if not user_message:
        return web.json_response({"error": "Empty message"}, status=400)

    history = data.get("history", [])
    focused_stock = data.get("focused_stock")

    # Build messages array for Perplexity
    system_content = CHAT_SYSTEM_PROMPT
    if focused_stock:
        system_content += f"\n\nThe user is currently viewing {focused_stock} in the analysis panel. Focus your answer on this stock's F&O setup."

    messages = [{"role": "system", "content": system_content}]

    # Add recent history (last 6 messages for context, skip system)
    for msg in history[-6:]:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})

    # Ensure the last message is the current user message
    if not messages or messages[-1].get("content") != user_message:
        messages.append({"role": "user", "content": user_message})

    # Call Perplexity API
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": _config.get("chat_model", PERPLEXITY_MODEL),
        "messages": messages,
        "max_tokens": 1024,
        "temperature": 0.7,
        "stream": False
    }

    try:
        async with ClientSession() as cs:
            async with cs.post(PERPLEXITY_URL, headers=headers, json=payload, timeout=30) as resp:
                if resp.status == 401:
                    return web.json_response({"reply": "Perplexity API key is invalid. Ask admin to update it."})
                if resp.status == 429:
                    return web.json_response({"reply": "Rate limited by Perplexity. Try again in a few seconds."})
                if resp.status != 200:
                    body = await resp.text()
                    log.error(f"Perplexity API error {resp.status}: {body[:200]}")
                    return web.json_response({"reply": f"AI service error ({resp.status}). Try again."})

                result = await resp.json()
                reply = result.get("choices", [{}])[0].get("message", {}).get("content", "No response.")
                return web.json_response({"reply": reply})

    except asyncio.TimeoutError:
        return web.json_response({"reply": "AI response timed out. Try a shorter question."})
    except Exception as e:
        log.error(f"Chat error: {e}")
        return web.json_response({"reply": f"Connection error: {str(e)}"})


async def handle_chat_key_api(request):
    """POST /api/admin/chat-key - Save Perplexity API key (admin only)."""
    token = get_session_from_request(request)
    session = validate_session(token)
    if not session or session["role"] != "admin":
        return web.json_response({"error": "Admin access required"}, status=403)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    key = data.get("key", "").strip()
    if not key:
        return web.json_response({"error": "API key required"}, status=400)

    # Save to config
    _config["perplexity_api_key"] = key
    with open(CONFIG_FILE, "w") as f:
        json.dump(_config, f, indent=2)

    log.info(f"Perplexity API key updated by {session['user']}")
    return web.json_response({"ok": True})


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
    """Proxy HTTP requests to backend with role-based access control."""
    path = request.path
    public_paths = {"/login", "/api/auth/login", "/api/auth/verify", "/api/auth/logout"}

    if path not in public_paths:
        token = get_session_from_request(request)
        session = validate_session(token)

        # Not logged in at all -> redirect to login
        if not session:
            raise web.HTTPFound("/login")

        # Logged in but accessing admin-only page as regular user -> 403
        if is_admin_path(path) and session["role"] != "admin":
            log.warning(f"Access denied: {session['user']} ({session['role']}) tried {path}")
            return web.Response(
                text="<html><body style='background:#09090b;color:#fafafa;font-family:Inter,sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0'>"
                     "<div style='text-align:center'>"
                     "<h1 style='font-size:48px;margin:0;color:#ef4444'>403</h1>"
                     "<p style='color:#a1a1aa;margin-top:8px'>Access restricted to administrators</p>"
                     "<a href='/' style='color:#3b82f6;margin-top:16px;display:inline-block'>Back to Dashboard</a>"
                     "</div></body></html>",
                content_type="text/html",
                status=403
            )

    # Build backend URL
    backend_url = f"http://{BACKEND_HOST}:{BACKEND_PORT}{request.path_qs}"

    # Forward headers (strip hop-by-hop)
    headers = {}
    skip_headers = {"host", "connection", "transfer-encoding", "keep-alive"}
    for key, val in request.headers.items():
        if key.lower() not in skip_headers:
            headers[key] = val

    body = await request.read() if request.can_read_body else None

    async with ClientSession() as session:
        try:
            async with session.request(
                request.method, backend_url,
                headers=headers,
                data=body,
                allow_redirects=False
            ) as resp:
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
        await asyncio.sleep(300)
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

    # AI Chat routes (handled directly, not proxied)
    app.router.add_post("/api/chat", handle_chat_api)
    app.router.add_post("/api/admin/chat-key", handle_chat_key_api)

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
