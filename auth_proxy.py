#!/usr/bin/env python3
"""
auth_proxy.py - Secure authentication reverse proxy for Quantra Terminal.

Runs on port 8080 (public). Proxies authenticated requests to ws_server.py
on port 8081 (internal). ZERO modifications to ws_server.py required.

Security:
  - PBKDF2-HMAC-SHA256 password hashing (600K iterations) via db.py
  - Rate limiting: login (5/15m), register (3/hr), API (120/min), chat (20/min)
  - Brute force protection: IP-based exponential backoff lockout
  - Account lockout: 5 failures → 15-min DB-level lock
  - CSRF: double-submit cookie on all state-changing API calls
  - Security headers: CSP, X-Frame-Options, HSTS, no-sniff, etc.
  - Request size limits: 100KB default, 500KB for chat
  - Audit logging: all auth events to audit.log + DB
  - Session: HttpOnly + SameSite=Strict cookies, DB-backed, IP-tracked
  - Input validation: strict regex + HTML escaping on all user inputs

Features:
  - SQLite-backed users, sessions, settings, paper trades (via db.py)
  - Cookie-based session auth (HttpOnly, SameSite=Strict)
  - Role-based access: "admin" (full) vs "user" (restricted)
  - User registration + profile management
  - Per-user paper trades (manual + auto)
  - AI chat via Perplexity / NVIDIA NIM (multi-provider)
  - Chat context includes live dashboard data + deep Upstox analysis
"""

import asyncio
import json
import os
import secrets
import time
import logging

from aiohttp import web, ClientSession, WSMsgType

try:
    from db import DB
    DB_AVAILABLE = True
except ImportError:
    DB_AVAILABLE = False

try:
    from security import (
        RateLimiter, BruteForceGuard, CSRFProtection,
        apply_security_headers, AuditLogger, InputValidator,
    )
    SECURITY_AVAILABLE = True
except ImportError:
    SECURITY_AVAILABLE = False

try:
    from chat_analysis import run_analysis, get_upstox_token
    ANALYSIS_AVAILABLE = True
except ImportError:
    ANALYSIS_AVAILABLE = False

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
MAX_REQUEST_SIZE = 100 * 1024       # 100KB default
MAX_CHAT_REQUEST_SIZE = 500 * 1024  # 500KB for chat (includes context)
MAX_CONCURRENT_SESSIONS = 5         # per user

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [AUTH] %(levelname)s %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("auth_proxy")

# ============================================================
# AUTH STATE
# ============================================================

_db = None       # DB instance (SQLite)
_config = None   # JSON config (AI settings, admin paths, etc.)
_rate_limiter = None
_brute_guard = None
_audit = None


def load_config():
    """Load JSON config for AI settings + initialize SQLite database + security."""
    global _config, _db, _rate_limiter, _brute_guard, _audit

    # Load JSON config (still needed for AI keys, admin paths)
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            _config = json.load(f)
        log.info(f"Loaded {CONFIG_FILE}: AI provider={_config.get('active_ai_provider', 'perplexity')}")
    else:
        _config = {"admin_paths": DEFAULT_ADMIN_PATHS, "session_max_age": SESSION_MAX_AGE}
        log.warning(f"{CONFIG_FILE} not found — using defaults (no AI keys)")

    # Initialize SQLite database
    if DB_AVAILABLE:
        _db = DB("quantra.db")
        _db.init()
        migrated = _db.migrate_from_json(CONFIG_FILE)
        if migrated:
            log.info(f"Migrated {migrated} user(s) from {CONFIG_FILE} to SQLite")
        user_count = _db.count_users()
        log.info(f"Database ready: {user_count} user(s)")
        if user_count == 0:
            log.warning("No users in database. Create one via /register or setup_auth.py")
    else:
        log.error("db.py not found — database features disabled.")

    # Initialize security modules
    if SECURITY_AVAILABLE:
        _rate_limiter = RateLimiter()
        _rate_limiter.configure("login", max_requests=5, window_seconds=900)     # 5 per 15m
        _rate_limiter.configure("register", max_requests=3, window_seconds=3600)  # 3 per hour
        _rate_limiter.configure("api", max_requests=120, window_seconds=60)       # 120 per min
        _rate_limiter.configure("chat", max_requests=20, window_seconds=60)       # 20 per min
        _rate_limiter.configure("password", max_requests=3, window_seconds=900)   # 3 per 15m

        _brute_guard = BruteForceGuard(threshold=5, base_lockout=60, max_lockout=3600)
        _audit = AuditLogger(db=_db)
        log.info("Security modules initialized: rate limiter, brute force guard, audit logger")
    else:
        log.warning("security.py not found — security features degraded")


def get_client_ip(request):
    """Get real client IP, accounting for reverse proxies."""
    # Trust X-Forwarded-For if behind nginx/cloudflare
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("X-Real-IP", "")
    if real_ip:
        return real_ip.strip()
    return request.remote or "unknown"


def get_session_from_request(request):
    """Extract session token from cookie."""
    return request.cookies.get(SESSION_COOKIE)


def validate_session(token):
    """Validate session token via DB. Returns session dict or None."""
    if not token or not _db:
        return None
    return _db.validate_session(token)


def is_admin_path(path):
    """Check if path requires admin role."""
    admin_paths = _config.get("admin_paths", DEFAULT_ADMIN_PATHS)
    return path in admin_paths


def check_rate_limit(rule, key, request):
    """Check rate limit. Returns None if OK, or a 429 response."""
    if not _rate_limiter:
        return None
    if not _rate_limiter.allow(rule, key):
        retry = _rate_limiter.retry_after(rule, key)
        ip = get_client_ip(request)
        if _audit:
            _audit.rate_limited(ip, rule)
        log.warning(f"Rate limited: {rule} key={key} ip={ip} retry_after={retry}s")
        resp = web.json_response(
            {"error": "Too many requests. Try again later.", "retry_after": retry},
            status=429,
        )
        resp.headers["Retry-After"] = str(retry)
        return resp
    return None


async def check_request_size(request, max_size=MAX_REQUEST_SIZE):
    """Reject requests that exceed size limit."""
    content_length = request.content_length
    if content_length and content_length > max_size:
        return web.json_response(
            {"error": f"Request too large (max {max_size // 1024}KB)"},
            status=413,
        )
    return None


# ============================================================
# MIDDLEWARE
# ============================================================

@web.middleware
async def security_middleware(request, handler):
    """
    Global middleware:
    1. Security headers on all responses
    2. CSRF validation on state-changing API requests
    3. General API rate limiting
    """
    ip = get_client_ip(request)

    # CSRF check for state-changing /api/ requests
    if SECURITY_AVAILABLE and request.path.startswith("/api/") and request.method not in ("GET", "HEAD", "OPTIONS"):
        ok, reason = CSRFProtection.validate(request)
        if not ok:
            if _audit:
                _audit.csrf_violation(ip, request.path)
            log.warning(f"CSRF violation: {ip} {request.method} {request.path} — {reason}")
            return web.json_response({"error": "CSRF validation failed"}, status=403)

    # General API rate limiting (per IP for unauthenticated, per user for authenticated)
    if request.path.startswith("/api/") and request.path not in ("/api/auth/login", "/api/auth/register"):
        token = get_session_from_request(request)
        session = validate_session(token)
        rate_key = f"user:{session['user_id']}" if session else f"ip:{ip}"
        rate_resp = check_rate_limit("api", rate_key, request)
        if rate_resp:
            return rate_resp

    # Call the actual handler
    response = await handler(request)

    # Apply security headers
    if SECURITY_AVAILABLE:
        apply_security_headers(response)

    # Ensure CSRF cookie is set on HTML page responses
    if SECURITY_AVAILABLE and response.content_type and "text/html" in response.content_type:
        CSRFProtection.get_or_set_token(request, response)

    return response


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
    if not _db:
        return web.json_response({"error": "Database not available"}, status=503)

    ip = get_client_ip(request)

    # Brute force check (IP-level)
    if _brute_guard and _brute_guard.is_locked(ip):
        remaining = _brute_guard.lockout_remaining(ip)
        if _audit:
            _audit.login_locked(ip)
        return web.json_response(
            {"error": f"Too many failed attempts. Try again in {remaining} seconds.", "retry_after": remaining},
            status=429,
        )

    # Rate limit (IP-level)
    rate_resp = check_rate_limit("login", f"ip:{ip}", request)
    if rate_resp:
        return rate_resp

    # Request size check
    size_resp = await check_request_size(request)
    if size_resp:
        return size_resp

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    username = data.get("username", "").strip()
    password = data.get("password", "")

    if not username or not password:
        return web.json_response({"error": "Username and password required"}, status=400)

    # Validate input format (prevent injection-style inputs)
    if len(username) > 100 or len(password) > 128:
        return web.json_response({"error": "Input too long"}, status=400)

    user_record = _db.verify_password(username, password)
    if not user_record:
        # Record failure
        if _brute_guard:
            _brute_guard.record_failure(ip)
        if _audit:
            _audit.login_failed(ip, username, details=f"attempts={_brute_guard.get_attempt_count(ip) if _brute_guard else '?'}")
        log.warning(f"Failed login: '{username}' from {ip}")
        return web.json_response({"error": "Invalid credentials"}, status=401)

    # Success
    if _brute_guard:
        _brute_guard.record_success(ip)

    role = user_record.get("role", "user")
    user_id = user_record["id"]
    max_age = _config.get("session_max_age", SESSION_MAX_AGE)

    # Enforce concurrent session limit
    active_sessions = _db.count_user_sessions(user_id)
    if active_sessions >= MAX_CONCURRENT_SESSIONS:
        # Delete oldest sessions to make room
        _db.delete_user_sessions(user_id)
        log.info(f"Cleared {active_sessions} old sessions for {username} (concurrent limit)")

    token = _db.create_session(
        user_id, max_age=max_age,
        ip=ip,
        ua=request.headers.get("User-Agent", "")[:200],
    )

    if _audit:
        _audit.login_success(ip, username, user_id)
    log.info(f"Login: {username} (role={role}) from {ip}")

    resp = web.json_response({
        "ok": True,
        "user": username,
        "role": role,
        "display_name": user_record.get("display_name", username),
    })
    resp.set_cookie(
        SESSION_COOKIE, token,
        max_age=max_age,
        httponly=True,
        samesite="Strict",
        path="/",
        secure=False,  # Set True when behind HTTPS
    )
    # Set CSRF cookie on login
    if SECURITY_AVAILABLE:
        CSRFProtection.get_or_set_token(request, resp)
    return resp


async def handle_verify_api(request):
    """GET /api/auth/verify - check session + return role."""
    token = get_session_from_request(request)
    session = validate_session(token)
    if session:
        admin_paths = _config.get("admin_paths", DEFAULT_ADMIN_PATHS)
        return web.json_response({
            "ok": True,
            "user": session["username"],
            "role": session["role"],
            "display_name": session.get("display_name", session["username"]),
            "user_id": session["user_id"],
            "admin_paths": admin_paths,
        })
    return web.json_response({"ok": False}, status=401)


async def handle_logout_api(request):
    """GET /api/auth/logout - destroy session and redirect to login."""
    token = get_session_from_request(request)
    if token and _db:
        session = _db.validate_session(token)
        if session:
            log.info(f"Logout: {session['username']}")
        _db.delete_session(token)

    resp = web.HTTPFound("/login")
    resp.del_cookie(SESSION_COOKIE, path="/")
    if SECURITY_AVAILABLE:
        resp.del_cookie(CSRFProtection.COOKIE_NAME, path="/")
    return resp


# ============================================================
# REGISTRATION
# ============================================================

async def handle_register_page(request):
    """Serve register.html."""
    token = get_session_from_request(request)
    session = validate_session(token)
    if session:
        raise web.HTTPFound("/")

    reg_file = os.path.join(os.path.dirname(__file__) or ".", "register.html")
    if not os.path.exists(reg_file):
        return web.Response(text="Registration page not found", status=500)
    with open(reg_file, "r") as f:
        html = f.read()
    return web.Response(text=html, content_type="text/html")


async def handle_register_api(request):
    """POST /api/auth/register - create new user account with full validation."""
    if not _db:
        return web.json_response({"error": "Database not available"}, status=503)

    ip = get_client_ip(request)

    # Rate limit (IP-level)
    rate_resp = check_rate_limit("register", f"ip:{ip}", request)
    if rate_resp:
        return rate_resp

    # Brute force check (reuse login guard — repeated reg attempts are suspicious)
    if _brute_guard and _brute_guard.is_locked(ip):
        remaining = _brute_guard.lockout_remaining(ip)
        return web.json_response(
            {"error": f"Too many attempts. Try again in {remaining} seconds."},
            status=429,
        )

    # Request size check
    size_resp = await check_request_size(request)
    if size_resp:
        return size_resp

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    # === STRICT INPUT VALIDATION ===
    if SECURITY_AVAILABLE:
        ok, username, err = InputValidator.username(data.get("username", ""))
        if not ok:
            return web.json_response({"error": err}, status=400)

        ok, email, err = InputValidator.email(data.get("email", ""))
        if not ok:
            return web.json_response({"error": err}, status=400)

        password = data.get("password", "")
        ok, err = InputValidator.password(password, min_length=8)
        if not ok:
            return web.json_response({"error": err}, status=400)

        confirm = data.get("confirm_password", "")
        if password != confirm:
            return web.json_response({"error": "Passwords do not match"}, status=400)

        ok, display_name, err = InputValidator.display_name(data.get("display_name", ""))
        if not ok:
            return web.json_response({"error": err}, status=400)
    else:
        # Fallback validation without security module
        username = data.get("username", "").strip()
        email = data.get("email", "").strip().lower() or None
        password = data.get("password", "")
        confirm = data.get("confirm_password", "")
        display_name = data.get("display_name", "").strip() or None

        if not username or len(username) < 3:
            return web.json_response({"error": "Username must be at least 3 characters"}, status=400)
        if len(password) < 8:
            return web.json_response({"error": "Password must be at least 8 characters"}, status=400)
        if password != confirm:
            return web.json_response({"error": "Passwords do not match"}, status=400)

    try:
        user_id = _db.create_user(
            username=username,
            email=email,
            password=password,
            role="user",
            display_name=display_name,
        )
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=409)
    except Exception as e:
        log.error(f"Registration error: {e}")
        return web.json_response({"error": "Registration failed"}, status=500)

    if _audit:
        _audit.registration(ip, username, user_id)
    log.info(f"New user registered: {username} (id={user_id}) from {ip}")
    return web.json_response({"ok": True, "user_id": user_id, "username": username})


# ============================================================
# USER PROFILE & SETTINGS
# ============================================================

async def handle_profile_page(request):
    """Serve profile.html (auth required)."""
    token = get_session_from_request(request)
    session = validate_session(token)
    if not session:
        raise web.HTTPFound("/login")

    prof_file = os.path.join(os.path.dirname(__file__) or ".", "profile.html")
    if not os.path.exists(prof_file):
        return web.Response(text="Profile page not found", status=500)
    with open(prof_file, "r") as f:
        html = f.read()
    return web.Response(text=html, content_type="text/html")


async def handle_user_profile_get(request):
    """GET /api/user/profile - return user profile + settings."""
    token = get_session_from_request(request)
    session = validate_session(token)
    if not session:
        return web.json_response({"error": "Unauthorized"}, status=401)

    user = _db.get_user(session["user_id"])
    if not user:
        return web.json_response({"error": "User not found"}, status=404)

    settings = _db.get_user_settings(session["user_id"]) or {}

    return web.json_response({
        "ok": True,
        "user": {
            "id": user["id"],
            "username": user["username"],
            "email": user.get("email"),
            "display_name": user.get("display_name"),
            "phone": user.get("phone"),
            "bio": user.get("bio"),
            "role": user["role"],
            "created_at": user.get("created_at"),
            "last_login": user.get("last_login"),
        },
        "settings": {
            "max_paper_trades_per_day": settings.get("max_paper_trades_per_day", 3),
            "default_lots": settings.get("default_lots", 1),
            "default_capital": settings.get("default_capital", 50000),
            "auto_exit_enabled": bool(settings.get("auto_exit_enabled", 1)),
            "auto_trail_sl": bool(settings.get("auto_trail_sl", 1)),
        },
        "auto_settings": {
            "auto_trade_enabled": bool(settings.get("auto_trade_enabled", 0)),
            "auto_trade_max_positions": settings.get("auto_trade_max_positions", 2),
            "auto_trade_max_capital": settings.get("auto_trade_max_capital", 50000),
            "preferred_sectors": settings.get("preferred_sectors", []),
        },
    })


async def handle_user_profile_post(request):
    """POST /api/user/profile - update display name, phone, bio."""
    token = get_session_from_request(request)
    session = validate_session(token)
    if not session:
        return web.json_response({"error": "Unauthorized"}, status=401)

    size_resp = await check_request_size(request)
    if size_resp:
        return size_resp

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    allowed = {}
    if SECURITY_AVAILABLE:
        if "display_name" in data:
            ok, val, err = InputValidator.display_name(data["display_name"])
            if ok and val:
                allowed["display_name"] = val
        if "phone" in data:
            ok, val, err = InputValidator.phone(data["phone"])
            if ok:
                allowed["phone"] = val
        if "bio" in data:
            ok, val, err = InputValidator.bio(data["bio"])
            if ok:
                allowed["bio"] = val
        if "email" in data:
            ok, val, err = InputValidator.email(data["email"])
            if ok and val:
                allowed["email"] = val
    else:
        if "display_name" in data:
            allowed["display_name"] = str(data["display_name"]).strip()[:50]
        if "phone" in data:
            allowed["phone"] = str(data["phone"]).strip()[:20]
        if "bio" in data:
            allowed["bio"] = str(data["bio"]).strip()[:500]
        if "email" in data:
            allowed["email"] = str(data["email"]).strip().lower()[:254]

    if not allowed:
        return web.json_response({"error": "No valid fields to update"}, status=400)

    _db.update_user_profile(session["user_id"], **allowed)
    log.info(f"Profile updated: {session['username']} fields={list(allowed.keys())}")
    return web.json_response({"ok": True, "updated": list(allowed.keys())})


async def handle_user_settings_post(request):
    """POST /api/user/settings - update trading settings."""
    token = get_session_from_request(request)
    session = validate_session(token)
    if not session:
        return web.json_response({"error": "Unauthorized"}, status=401)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    updates = {}
    if "max_paper_trades_per_day" in data:
        v = int(data["max_paper_trades_per_day"])
        updates["max_paper_trades_per_day"] = max(1, min(20, v))
    if "default_lots" in data:
        v = int(data["default_lots"])
        updates["default_lots"] = max(1, min(50, v))
    if "default_capital" in data:
        v = float(data["default_capital"])
        updates["default_capital"] = max(10000, min(10000000, v))
    if "auto_exit_enabled" in data:
        updates["auto_exit_enabled"] = 1 if data["auto_exit_enabled"] else 0
    if "auto_trail_sl" in data:
        updates["auto_trail_sl"] = 1 if data["auto_trail_sl"] else 0

    if not updates:
        return web.json_response({"error": "No valid settings to update"}, status=400)

    _db.update_user_settings(session["user_id"], **updates)
    log.info(f"Settings updated: {session['username']} {updates}")
    return web.json_response({"ok": True, "updated": list(updates.keys())})


async def handle_user_auto_settings_post(request):
    """POST /api/user/auto-settings - update auto-trade settings."""
    token = get_session_from_request(request)
    session = validate_session(token)
    if not session:
        return web.json_response({"error": "Unauthorized"}, status=401)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    updates = {}
    if "auto_trade_enabled" in data:
        updates["auto_trade_enabled"] = 1 if data["auto_trade_enabled"] else 0
    if "auto_trade_max_positions" in data:
        v = int(data["auto_trade_max_positions"])
        updates["auto_trade_max_positions"] = max(1, min(10, v))
    if "auto_trade_max_capital" in data:
        v = float(data["auto_trade_max_capital"])
        updates["auto_trade_max_capital"] = max(10000, min(10000000, v))
    if "preferred_sectors" in data:
        if isinstance(data["preferred_sectors"], list):
            # Sanitize sector names
            safe = [InputValidator.sanitize_string(s, 30) if SECURITY_AVAILABLE else str(s)[:30]
                    for s in data["preferred_sectors"][:20]]
            updates["preferred_sectors"] = safe

    if not updates:
        return web.json_response({"error": "No valid settings to update"}, status=400)

    _db.update_user_settings(session["user_id"], **updates)
    log.info(f"Auto-settings updated: {session['username']} {updates}")
    return web.json_response({"ok": True, "updated": list(updates.keys())})


async def handle_user_change_password(request):
    """POST /api/user/change-password - change user password with validation."""
    token = get_session_from_request(request)
    session = validate_session(token)
    if not session:
        return web.json_response({"error": "Unauthorized"}, status=401)

    ip = get_client_ip(request)

    # Rate limit password changes
    rate_resp = check_rate_limit("password", f"user:{session['user_id']}", request)
    if rate_resp:
        return rate_resp

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    current_pw = data.get("current_password", "")
    new_pw = data.get("new_password", "")
    confirm_pw = data.get("confirm_password", "")

    if not current_pw or not new_pw:
        return web.json_response({"error": "Current and new passwords required"}, status=400)

    # Validate new password strength
    if SECURITY_AVAILABLE:
        ok, err = InputValidator.password(new_pw, min_length=8)
        if not ok:
            return web.json_response({"error": err}, status=400)
    elif len(new_pw) < 8:
        return web.json_response({"error": "New password must be at least 8 characters"}, status=400)

    if new_pw != confirm_pw:
        return web.json_response({"error": "New passwords do not match"}, status=400)

    ok, msg = _db.change_password(session["user_id"], current_pw, new_pw)
    if not ok:
        return web.json_response({"error": msg}, status=400)

    # Invalidate all other sessions (force re-login with new password)
    _db.delete_user_sessions(session["user_id"])

    # Create a new session for the current user
    new_token = _db.create_session(
        session["user_id"],
        max_age=_config.get("session_max_age", SESSION_MAX_AGE),
        ip=ip,
        ua=request.headers.get("User-Agent", "")[:200],
    )

    if _audit:
        _audit.password_change(ip, session["username"], session["user_id"])
    log.info(f"Password changed: {session['username']}")

    resp = web.json_response({"ok": True, "message": "Password changed. All other sessions terminated."})
    resp.set_cookie(
        SESSION_COOKIE, new_token,
        max_age=_config.get("session_max_age", SESSION_MAX_AGE),
        httponly=True,
        samesite="Strict",
        path="/",
    )
    return resp


async def handle_user_stats(request):
    """GET /api/user/stats - trade performance stats."""
    token = get_session_from_request(request)
    session = validate_session(token)
    if not session:
        return web.json_response({"error": "Unauthorized"}, status=401)

    stats_all = _db.get_trade_stats(session["user_id"])
    stats_week = _db.get_trade_stats(session["user_id"], days=7)
    stats_month = _db.get_trade_stats(session["user_id"], days=30)
    today_count = _db.count_user_trades_today(session["user_id"])

    return web.json_response({
        "ok": True,
        "all_time": stats_all,
        "last_7_days": stats_week,
        "last_30_days": stats_month,
        "trades_today": today_count,
    })


async def handle_user_logout_all(request):
    """POST /api/user/logout-all - destroy all sessions for this user."""
    token = get_session_from_request(request)
    session = validate_session(token)
    if not session:
        return web.json_response({"error": "Unauthorized"}, status=401)

    ip = get_client_ip(request)
    _db.delete_user_sessions(session["user_id"])
    if _audit:
        _audit.logout_all(ip, session["username"], session["user_id"])
    log.info(f"Logout all devices: {session['username']}")

    resp = web.json_response({"ok": True, "message": "All sessions terminated"})
    resp.del_cookie(SESSION_COOKIE, path="/")
    return resp


# ============================================================
# PAPER TRADES API
# ============================================================

async def handle_paper_trades_list(request):
    """GET /api/user/trades - list user's paper trades."""
    token = get_session_from_request(request)
    session = validate_session(token)
    if not session:
        return web.json_response({"error": "Unauthorized"}, status=401)

    status = request.query.get("status")
    trade_type = request.query.get("type")
    limit = min(int(request.query.get("limit", 50)), 200)
    offset = max(int(request.query.get("offset", 0)), 0)

    trades = _db.get_paper_trades(
        session["user_id"], status=status, trade_type=trade_type,
        limit=limit, offset=offset,
    )
    return web.json_response({"ok": True, "trades": trades, "count": len(trades)})


async def handle_paper_trade_create(request):
    """POST /api/user/trades - create a new paper trade."""
    token = get_session_from_request(request)
    session = validate_session(token)
    if not session:
        return web.json_response({"error": "Unauthorized"}, status=401)

    size_resp = await check_request_size(request)
    if size_resp:
        return size_resp

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    # Validate symbol
    if SECURITY_AVAILABLE:
        ok, symbol, err = InputValidator.symbol(data.get("symbol", ""))
        if not ok:
            return web.json_response({"error": err}, status=400)
    else:
        symbol = data.get("symbol", "").strip().upper()
        if not symbol:
            return web.json_response({"error": "Symbol required"}, status=400)

    direction = data.get("direction", "").strip().upper()
    if direction not in ("BULLISH", "BEARISH", "CE", "PE"):
        return web.json_response({"error": "direction must be BULLISH/BEARISH/CE/PE"}, status=400)

    # Check daily limit
    settings = _db.get_user_settings(session["user_id"]) or {}
    max_trades = settings.get("max_paper_trades_per_day", 3)
    today_count = _db.count_user_trades_today(session["user_id"])
    if today_count >= max_trades:
        return web.json_response({"error": f"Daily trade limit reached ({max_trades})"}, status=429)

    trade_id = _db.create_paper_trade(
        user_id=session["user_id"],
        symbol=symbol,
        direction=direction,
        trade_type=data.get("trade_type", "manual"),
        strike=data.get("strike"),
        expiry=data.get("expiry"),
        entry_premium=data.get("entry_premium"),
        lots=data.get("lots", settings.get("default_lots", 1)),
        lot_size=data.get("lot_size"),
        sl_premium=data.get("sl_premium"),
        sl_spot=data.get("sl_spot"),
        t1_premium=data.get("t1_premium"),
        t2_premium=data.get("t2_premium"),
        status=data.get("status", "PENDING"),
        entry_reason=InputValidator.sanitize_string(data.get("entry_reason", ""), 500) if SECURITY_AVAILABLE else str(data.get("entry_reason", ""))[:500],
    )

    log.info(f"Paper trade created: {session['username']} {symbol} {direction} (id={trade_id})")
    return web.json_response({"ok": True, "trade_id": trade_id})


async def handle_paper_trade_update(request):
    """POST /api/user/trades/{id} - update a paper trade (status, exit, PnL)."""
    token = get_session_from_request(request)
    session = validate_session(token)
    if not session:
        return web.json_response({"error": "Unauthorized"}, status=401)

    try:
        trade_id = int(request.match_info["id"])
    except (ValueError, KeyError):
        return web.json_response({"error": "Invalid trade ID"}, status=400)

    # Verify ownership
    trade = _db.get_paper_trade(trade_id, user_id=session["user_id"])
    if not trade:
        return web.json_response({"error": "Trade not found"}, status=404)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    _db.update_paper_trade(trade_id, user_id=session["user_id"], **data)
    log.info(f"Paper trade updated: {session['username']} trade_id={trade_id} fields={list(data.keys())}")
    return web.json_response({"ok": True, "trade_id": trade_id})


# ============================================================
# AI CHAT (Multi-Provider: Perplexity + NVIDIA NIM)
# ============================================================

AI_PROVIDERS = {
    "perplexity": {
        "name": "Perplexity",
        "url": "https://api.perplexity.ai/chat/completions",
        "default_model": "sonar",
        "models": ["sonar", "sonar-pro", "sonar-reasoning"],
        "config_key_field": "perplexity_api_key",
    },
    "nvidia": {
        "name": "NVIDIA NIM",
        "url": "https://integrate.api.nvidia.com/v1/chat/completions",
        "default_model": "meta/llama-3.3-70b-instruct",
        "models": [
            "meta/llama-3.3-70b-instruct",
            "nvidia/llama-3.3-nemotron-super-49b-v1",
            "deepseek-ai/deepseek-r1",
        ],
        "config_key_field": "nvidia_api_key",
    },
}

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


def get_ai_config():
    """Return (provider_id, api_url, api_key, model) for the active AI provider."""
    active = _config.get("active_ai_provider", "perplexity")
    if active not in AI_PROVIDERS:
        active = "perplexity"
    prov = AI_PROVIDERS[active]
    api_key = _config.get(prov["config_key_field"], "")
    model = _config.get(f"{active}_model", prov["default_model"])
    return active, prov["url"], api_key, model


async def handle_chat_api(request):
    """POST /api/chat - AI chat with rate limiting and size checks."""
    token = get_session_from_request(request)
    session = validate_session(token)
    if not session:
        return web.json_response({"error": "Unauthorized"}, status=401)

    # Chat-specific rate limit
    rate_resp = check_rate_limit("chat", f"user:{session['user_id']}", request)
    if rate_resp:
        return rate_resp

    # Larger size limit for chat (includes dashboard context)
    size_resp = await check_request_size(request, MAX_CHAT_REQUEST_SIZE)
    if size_resp:
        return size_resp

    provider_id, api_url, api_key, model = get_ai_config()
    provider_name = AI_PROVIDERS[provider_id]["name"]

    if not api_key:
        return web.json_response({
            "reply": f"Chat not configured. Admin needs to set the {provider_name} API key in Admin > AI Settings."
        })

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    user_message = data.get("message", "").strip()
    if not user_message:
        return web.json_response({"error": "Empty message"}, status=400)
    # Truncate extremely long messages
    if len(user_message) > 4000:
        user_message = user_message[:4000]

    history = data.get("history", [])
    focused_stock = data.get("focused_stock")
    dash_context = data.get("context", {})

    # Build messages array
    system_content = CHAT_SYSTEM_PROMPT

    # Inject live dashboard data as context
    context_parts = []
    if dash_context.get("market_summary"):
        ms = dash_context["market_summary"]
        context_parts.append(
            f"LIVE MARKET SNAPSHOT: {ms.get('total_stocks',0)} F&O stocks tracked. "
            f"Advancing: {ms.get('advancing',0)}, Declining: {ms.get('declining',0)}, "
            f"Unchanged: {ms.get('unchanged',0)}. Breadth ratio: {ms.get('breadth_ratio',0)}"
        )

    if dash_context.get("trade_ready_picks"):
        picks = dash_context["trade_ready_picks"]
        lines = []
        for p in picks[:8]:
            lines.append(
                f"  {p['symbol']} ({p.get('direction','?')}) "
                f"LTP={p.get('ltp','?')} Chg={p.get('chg_pct','?')}% "
                f"Score={p.get('score','?')} Buildup={p.get('buildup','?')} "
                f"PCR={p.get('pcr','?')} IV={p.get('atm_iv','?')} "
                f"CE={p.get('atm_ce','?')} PE={p.get('atm_pe','?')} "
                f"VolSurge={p.get('vol_surge','?')}x Sector={p.get('sector','?')}"
            )
        context_parts.append(
            f"TRADE-READY STOCKS ({dash_context.get('trade_ready_count',len(picks))} total, top {len(lines)}):\n"
            + "\n".join(lines)
        )

    if dash_context.get("top_movers"):
        movers = dash_context["top_movers"]
        ml = [f"  {m['symbol']} {m.get('chg_pct',0):+.2f}% LTP={m.get('ltp','?')} {m.get('buildup','')}" for m in movers]
        context_parts.append("TOP MOVERS:\n" + "\n".join(ml))

    if dash_context.get("focused_stock_data"):
        fd = dash_context["focused_stock_data"]
        context_parts.append(
            f"FOCUSED STOCK: {fd.get('symbol')} (Sector: {fd.get('sector')}, "
            f"N50: {fd.get('is_n50')}, Lot: {fd.get('lot')})\n"
            f"  Price: LTP={fd.get('ltp')} O={fd.get('open')} H={fd.get('high')} L={fd.get('low')} "
            f"Chg={fd.get('chg_pct','?')}% Gap={fd.get('gap_pct','?')}% Range={fd.get('range_pct','?')}%\n"
            f"  Volume: {fd.get('vol','?')} VolSurge={fd.get('vol_surge','?')}x\n"
            f"  OI: CE_OI_Chg={fd.get('ce_oi_chg','?')} PE_OI_Chg={fd.get('pe_oi_chg','?')} "
            f"Net_OI={fd.get('net_oi','?')} Buildup={fd.get('buildup','?')}\n"
            f"  Options: PCR={fd.get('pcr','?')} PCR_Sig={fd.get('pcr_sig','?')} "
            f"ATM_Strike={fd.get('atm_strike','?')} ATM_IV={fd.get('atm_iv','?')}%\n"
            f"  Premiums: CE={fd.get('atm_ce','?')} PE={fd.get('atm_pe','?')} "
            f"Prem_OK={fd.get('prem_ok','?')}\n"
            f"  Max Pain: {fd.get('max_pain','?')} (Dist={fd.get('mp_dist','?')}%)\n"
            f"  Score={fd.get('score','?')} Direction={fd.get('direction','?')} "
            f"TradeReady={fd.get('is_trade_ready',False)} PoisedScore={fd.get('poised_score','?')}"
        )

    if context_parts:
        system_content += "\n\n--- LIVE DASHBOARD DATA (real-time, use these numbers in your analysis) ---\n"
        system_content += "\n\n".join(context_parts)
    elif focused_stock:
        system_content += f"\n\nThe user is currently viewing {focused_stock} in the analysis panel. Focus your answer on this stock's F&O setup."

    # Deep analysis via Upstox API (chain, technicals, OI)
    analysis_text = None
    if ANALYSIS_AVAILABLE:
        try:
            upstox_token = get_upstox_token()
            if upstox_token:
                analysis_text, query_type = await run_analysis(
                    user_message,
                    focused_stock=data.get("focused_stock"),
                    dashboard_context=dash_context,
                    token=upstox_token,
                )
                if analysis_text:
                    system_content += f"\n\n--- DEEP ANALYSIS FROM LIVE UPSTOX API ---\n{analysis_text}"
                    log.info(f"Deep analysis injected ({query_type}), {len(analysis_text)} chars")
        except Exception as e:
            log.warning(f"Deep analysis failed (non-fatal): {e}")

    messages = [{"role": "system", "content": system_content}]

    for msg in history[-6:]:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content[:4000]})

    if not messages or messages[-1].get("content") != user_message:
        messages.append({"role": "user", "content": user_message})

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    max_tok = 2048 if analysis_text else 1024
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tok,
        "temperature": 0.7,
        "stream": False,
    }

    try:
        req_timeout = 60 if analysis_text else 45
        async with ClientSession() as cs:
            async with cs.post(api_url, headers=headers, json=payload, timeout=req_timeout) as resp:
                if resp.status == 401:
                    return web.json_response({"reply": f"{provider_name} API key is invalid or expired. Ask admin to update it in Admin > AI Settings."})
                if resp.status == 429:
                    return web.json_response({"reply": f"Rate limited by {provider_name}. Try again in a few seconds."})
                if resp.status != 200:
                    body = await resp.text()
                    log.error(f"{provider_name} API error {resp.status}: {body[:200]}")
                    return web.json_response({"reply": f"AI service error ({resp.status}) from {provider_name}. Try again."})

                result = await resp.json()
                reply = result.get("choices", [{}])[0].get("message", {}).get("content", "No response.")
                return web.json_response({"reply": reply, "provider": provider_id})

    except asyncio.TimeoutError:
        return web.json_response({"reply": f"{provider_name} response timed out. Try a shorter question."})
    except Exception as e:
        log.error(f"Chat error ({provider_name}): {e}")
        return web.json_response({"reply": f"Connection error with {provider_name}: {str(e)}"})


# ============================================================
# ADMIN AI SETTINGS
# ============================================================

async def handle_ai_settings_get(request):
    """GET /api/admin/ai-settings - Return current AI config (admin only)."""
    token = get_session_from_request(request)
    session = validate_session(token)
    if not session or session["role"] != "admin":
        return web.json_response({"error": "Admin access required"}, status=403)

    active = _config.get("active_ai_provider", "perplexity")
    result = {
        "active_provider": active,
        "providers": {},
    }
    for pid, prov in AI_PROVIDERS.items():
        raw_key = _config.get(prov["config_key_field"], "")
        masked = ""
        if raw_key:
            masked = raw_key[:6] + "..." + raw_key[-4:] if len(raw_key) > 12 else "****"
        result["providers"][pid] = {
            "name": prov["name"],
            "has_key": bool(raw_key),
            "key_masked": masked,
            "model": _config.get(f"{pid}_model", prov["default_model"]),
            "available_models": prov["models"],
        }
    return web.json_response(result)


async def handle_ai_settings_post(request):
    """POST /api/admin/ai-settings - Update AI config (admin only)."""
    token = get_session_from_request(request)
    session = validate_session(token)
    if not session or session["role"] != "admin":
        return web.json_response({"error": "Admin access required"}, status=403)

    ip = get_client_ip(request)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    updated = []

    if "active_provider" in data:
        ap = data["active_provider"]
        if ap in AI_PROVIDERS:
            _config["active_ai_provider"] = ap
            updated.append(f"active_provider={ap}")
        else:
            return web.json_response({"error": f"Unknown provider: {ap}"}, status=400)

    for pid, prov in AI_PROVIDERS.items():
        key_field = f"{pid}_key"
        if key_field in data:
            new_key = data[key_field].strip()
            if new_key:
                _config[prov["config_key_field"]] = new_key
                updated.append(f"{pid}_key")

        model_field = f"{pid}_model"
        if model_field in data:
            new_model = data[model_field].strip()
            if new_model:
                _config[f"{pid}_model"] = new_model
                updated.append(f"{pid}_model={new_model}")

    if not updated:
        return web.json_response({"error": "No valid fields to update"}, status=400)

    with open(CONFIG_FILE, "w") as f:
        json.dump(_config, f, indent=2)

    if _audit:
        _audit.admin_action(ip, session["username"], session["user_id"], f"ai_settings: {', '.join(updated)}")
    log.info(f"AI settings updated by {session['username']}: {', '.join(updated)}")
    return web.json_response({"ok": True, "updated": updated})


async def handle_ai_test(request):
    """POST /api/admin/ai-test - Test AI provider connection."""
    token = get_session_from_request(request)
    session = validate_session(token)
    if not session or session["role"] != "admin":
        return web.json_response({"error": "Admin access required"}, status=403)

    try:
        data = await request.json()
    except Exception:
        data = {}

    test_provider = data.get("provider", _config.get("active_ai_provider", "perplexity"))
    if test_provider not in AI_PROVIDERS:
        return web.json_response({"error": f"Unknown provider: {test_provider}"}, status=400)

    prov = AI_PROVIDERS[test_provider]
    api_key = _config.get(prov["config_key_field"], "")
    model = _config.get(f"{test_provider}_model", prov["default_model"])

    if not api_key:
        return web.json_response({"ok": False, "error": f"No API key set for {prov['name']}"})

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Say OK in one word."},
        ],
        "max_tokens": 10,
        "temperature": 0,
        "stream": False,
    }

    try:
        async with ClientSession() as cs:
            async with cs.post(prov["url"], headers=headers, json=payload, timeout=15) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    reply = result.get("choices", [{}])[0].get("message", {}).get("content", "")
                    return web.json_response({"ok": True, "provider": test_provider, "model": model, "reply": reply})
                else:
                    body = await resp.text()
                    return web.json_response({"ok": False, "status": resp.status, "error": body[:300]})
    except asyncio.TimeoutError:
        return web.json_response({"ok": False, "error": "Connection timed out"})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)})


# ============================================================
# ADMIN USER MANAGEMENT
# ============================================================

async def handle_admin_users(request):
    """GET /api/admin/users - list all users + platform stats (admin only)."""
    token = get_session_from_request(request)
    session = validate_session(token)
    if not session or session["role"] != "admin":
        return web.json_response({"error": "Admin access required"}, status=403)

    users = _db.list_users(include_inactive=True)
    stats = _db.get_platform_stats()
    return web.json_response({"ok": True, "users": users, "stats": stats})


async def handle_admin_unlock_user(request):
    """POST /api/admin/unlock-user - unlock a locked-out user (admin only)."""
    token = get_session_from_request(request)
    session = validate_session(token)
    if not session or session["role"] != "admin":
        return web.json_response({"error": "Admin access required"}, status=403)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    user_id = data.get("user_id")
    if not user_id:
        return web.json_response({"error": "user_id required"}, status=400)

    _db.unlock_user(int(user_id))
    ip = get_client_ip(request)
    if _audit:
        _audit.admin_action(ip, session["username"], session["user_id"], f"unlock_user: {user_id}")
    log.info(f"Admin {session['username']} unlocked user_id={user_id}")
    return web.json_response({"ok": True})


async def handle_admin_deactivate_user(request):
    """POST /api/admin/deactivate-user - soft-delete a user (admin only)."""
    token = get_session_from_request(request)
    session = validate_session(token)
    if not session or session["role"] != "admin":
        return web.json_response({"error": "Admin access required"}, status=403)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    user_id = data.get("user_id")
    if not user_id:
        return web.json_response({"error": "user_id required"}, status=400)
    if int(user_id) == session["user_id"]:
        return web.json_response({"error": "Cannot deactivate yourself"}, status=400)

    _db.deactivate_user(int(user_id))
    ip = get_client_ip(request)
    if _audit:
        _audit.admin_action(ip, session["username"], session["user_id"], f"deactivate_user: {user_id}")
    log.info(f"Admin {session['username']} deactivated user_id={user_id}")
    return web.json_response({"ok": True})


async def handle_admin_audit_log(request):
    """GET /api/admin/audit - get recent security audit events (admin only)."""
    token = get_session_from_request(request)
    session = validate_session(token)
    if not session or session["role"] != "admin":
        return web.json_response({"error": "Admin access required"}, status=403)

    limit = min(int(request.query.get("limit", 50)), 200)
    event_type = request.query.get("type")
    username = request.query.get("username")

    events = _db.get_recent_audit_events(limit=limit, event_type=event_type, username=username)
    return web.json_response({"ok": True, "events": events, "count": len(events)})


# ============================================================
# AUTO-TRADE ENGINE
# ============================================================

_auto_trader = None

def get_auto_trader():
    """Lazy-init AutoTrader instance."""
    global _auto_trader
    if _auto_trader is None:
        try:
            from auto_trader import AutoTrader
            _auto_trader = AutoTrader(_db)
            log.info("AutoTrader engine loaded successfully")
        except ImportError:
            log.warning("auto_trader.py not found — auto-trade endpoint disabled")
        except Exception as e:
            log.error(f"Failed to load AutoTrader: {e}")
    return _auto_trader


async def handle_auto_trade_run(request):
    """POST /api/admin/auto-trade/run - trigger an auto-trade scan (admin only)."""
    token = get_session_from_request(request)
    session = validate_session(token)
    if not session or session["role"] != "admin":
        return web.json_response({"error": "Admin access required"}, status=403)

    trader = get_auto_trader()
    if trader is None:
        return web.json_response({"error": "AutoTrader engine not available"}, status=503)

    # Parse optional parameters
    try:
        data = await request.json()
    except Exception:
        data = {}

    target_user_id = data.get("user_id", session["user_id"])
    nifty_direction = data.get("nifty_direction")
    nifty_score = data.get("nifty_score")
    candidates = data.get("candidates")
    force = data.get("force", False)  # bypass trading window check

    try:
        result = trader.run_scan(
            user_id=target_user_id,
            candidates=candidates,
            nifty_direction=nifty_direction,
            nifty_score=nifty_score,
            force=force,
        )
        log.info(
            f"Auto-trade scan by {session['username']}: "
            f"direction={result.get('nifty_direction')} "
            f"trades={len(result.get('trades_entered', []))} "
            f"skip={result.get('skip_reason')}"
        )

        if SECURITY_AVAILABLE:
            _audit.admin_action(
                request.remote, session["username"],
                f"auto_trade_scan: {result.get('nifty_direction')}, "
                f"entered={len(result.get('trades_entered', []))}"
            )

        return web.json_response({"ok": True, **result})
    except Exception as e:
        log.error(f"Auto-trade scan error: {e}", exc_info=True)
        return web.json_response({"error": str(e)}, status=500)


async def handle_auto_trade_status(request):
    """GET /api/admin/auto-trade/status - get auto-trade engine status."""
    token = get_session_from_request(request)
    session = validate_session(token)
    if not session:
        return web.json_response({"error": "Unauthorized"}, status=401)

    trader = get_auto_trader()
    if trader is None:
        return web.json_response({
            "ok": True, "available": False,
            "reason": "AutoTrader engine not loaded",
        })

    settings = _db.get_user_settings(session["user_id"]) or {}
    today_auto = _db.count_user_trades_today(session["user_id"], trade_type="auto")

    return web.json_response({
        "ok": True,
        "available": True,
        "in_trading_window": trader.is_trading_window(),
        "auto_trade_enabled": bool(settings.get("auto_trade_enabled", 0)),
        "max_positions": settings.get("auto_trade_max_positions", 2),
        "max_capital": settings.get("auto_trade_max_capital", 50000),
        "today_auto_trades": today_auto,
        "daily_limit": 2,
    })


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

    async with ClientSession() as cs_session:
        try:
            async with cs_session.ws_connect(backend_url) as ws_backend:
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
    public_paths = {"/login", "/register", "/api/auth/login", "/api/auth/register", "/api/auth/verify", "/api/auth/logout"}

    if path not in public_paths:
        token = get_session_from_request(request)
        session = validate_session(token)

        if not session:
            raise web.HTTPFound("/login")

        if is_admin_path(path) and session["role"] != "admin":
            log.warning(f"Access denied: {session['username']} ({session['role']}) tried {path}")
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

    backend_url = f"http://{BACKEND_HOST}:{BACKEND_PORT}{request.path_qs}"

    headers = {}
    skip_headers = {"host", "connection", "transfer-encoding", "keep-alive"}
    for key, val in request.headers.items():
        if key.lower() not in skip_headers:
            headers[key] = val

    body = await request.read() if request.can_read_body else None

    async with ClientSession() as cs_session:
        try:
            async with cs_session.request(
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
    """Periodic cleanup: sessions, brute force records, audit log."""
    while True:
        await asyncio.sleep(300)
        try:
            if _db:
                n = _db.cleanup_expired_sessions()
                if n:
                    log.info(f"Cleaned up {n} expired sessions")
                # Clean old audit entries quarterly
                _db.cleanup_old_audit(days=90)
            if _brute_guard:
                _brute_guard.cleanup_stale()
        except Exception as e:
            log.warning(f"Cleanup error: {e}")


async def auto_scan_timer(app):
    """
    Periodic auto-trade scanner. Runs every 5 minutes during market hours.
    Scans all users with auto_trade_enabled, fetches live Upstox data,
    and feeds candidates into the AutoTrader engine.
    """
    AUTO_SCAN_INTERVAL = 300  # 5 minutes
    # Wait 60 seconds after startup before first scan
    await asyncio.sleep(60)

    log.info("Auto-scan timer started (every %d seconds)", AUTO_SCAN_INTERVAL)

    while True:
        try:
            trader = get_auto_trader()
            if trader is None:
                log.debug("Auto-scan: AutoTrader not available, sleeping")
                await asyncio.sleep(AUTO_SCAN_INTERVAL)
                continue

            # Check trading window
            if not trader.is_trading_window():
                log.debug("Auto-scan: outside trading hours, sleeping")
                await asyncio.sleep(AUTO_SCAN_INTERVAL)
                continue

            # Find all users with auto_trade_enabled
            if not _db:
                await asyncio.sleep(AUTO_SCAN_INTERVAL)
                continue

            try:
                users_with_auto = _db.conn.execute(
                    "SELECT u.id, u.username FROM users u "
                    "JOIN user_settings s ON s.user_id = u.id "
                    "WHERE s.auto_trade_enabled = 1 AND u.is_active = 1"
                ).fetchall()
            except Exception as exc:
                log.error("Auto-scan: failed to query auto-trade users: %s", exc)
                await asyncio.sleep(AUTO_SCAN_INTERVAL)
                continue

            if not users_with_auto:
                log.debug("Auto-scan: no users with auto_trade_enabled")
                await asyncio.sleep(AUTO_SCAN_INTERVAL)
                continue

            # Fetch NIFTY direction (once for all users)
            nifty_direction = "NEUTRAL"
            nifty_score = 50.0
            try:
                from auto_trader import LiveDataBridge
                bridge = LiveDataBridge()
                nifty_direction, nifty_score = await bridge.fetch_nifty_direction()
                log.info(
                    "Auto-scan: NIFTY %s (%.0f) — scanning %d users",
                    nifty_direction, nifty_score, len(users_with_auto),
                )
            except Exception as exc:
                log.error("Auto-scan: NIFTY direction fetch failed: %s", exc)

            # Fetch candidates (once — same candidates for all users)
            candidates = []
            try:
                bridge = LiveDataBridge() if 'bridge' not in dir() else bridge
                candidates = await bridge.scan_whitelisted_stocks(
                    nifty_direction, max_stocks=10,
                )
                log.info("Auto-scan: %d candidates found", len(candidates))
            except Exception as exc:
                log.error("Auto-scan: stock scan failed: %s", exc)

            # Run scan for each user
            for row in users_with_auto:
                uid = row[0]
                uname = row[1]
                try:
                    result = trader.run_scan(
                        user_id=uid,
                        candidates=candidates if candidates else None,
                        nifty_direction=nifty_direction,
                        nifty_score=nifty_score,
                    )
                    trades_ct = len(result.get("trades_entered", []))
                    if trades_ct > 0:
                        log.info(
                            "Auto-scan: user %s — %d trade(s) entered",
                            uname, trades_ct,
                        )
                    else:
                        log.debug(
                            "Auto-scan: user %s — skip: %s",
                            uname, result.get("skip_reason", "no trades"),
                        )
                except Exception as exc:
                    log.error("Auto-scan: error for user %s: %s", uname, exc)

        except asyncio.CancelledError:
            log.info("Auto-scan timer cancelled")
            return
        except Exception as exc:
            log.error("Auto-scan timer error: %s", exc, exc_info=True)

        await asyncio.sleep(AUTO_SCAN_INTERVAL)


async def on_startup(app):
    app["cleanup_task"] = asyncio.create_task(session_cleanup_task(app))
    app["auto_scan_task"] = asyncio.create_task(auto_scan_timer(app))
    log.info(f"Auth proxy started on :{PROXY_PORT}, backend at {BACKEND_HOST}:{BACKEND_PORT}")
    if SECURITY_AVAILABLE:
        log.info("Security: rate limiting, brute force guard, CSRF, security headers — ALL ACTIVE")
    else:
        log.warning("Security: DEGRADED — security.py not found")
    log.info("Auto-scan timer: ACTIVE (every 5 min during market hours)")


async def on_shutdown(app):
    app["cleanup_task"].cancel()
    if "auto_scan_task" in app:
        app["auto_scan_task"].cancel()
    if _db:
        _db.close()
    log.info("Auth proxy shutting down")


def create_app():
    load_config()

    app = web.Application(
        middlewares=[security_middleware],
        client_max_size=MAX_CHAT_REQUEST_SIZE,  # Global request size limit
    )

    # Auth routes
    app.router.add_get("/login", handle_login_page)
    app.router.add_post("/api/auth/login", handle_login_api)
    app.router.add_get("/api/auth/verify", handle_verify_api)
    app.router.add_get("/api/auth/logout", handle_logout_api)

    # Registration
    app.router.add_get("/register", handle_register_page)
    app.router.add_post("/api/auth/register", handle_register_api)

    # User profile & settings
    app.router.add_get("/profile", handle_profile_page)
    app.router.add_get("/api/user/profile", handle_user_profile_get)
    app.router.add_post("/api/user/profile", handle_user_profile_post)
    app.router.add_post("/api/user/settings", handle_user_settings_post)
    app.router.add_post("/api/user/auto-settings", handle_user_auto_settings_post)
    app.router.add_post("/api/user/change-password", handle_user_change_password)
    app.router.add_get("/api/user/stats", handle_user_stats)
    app.router.add_post("/api/user/logout-all", handle_user_logout_all)

    # Paper trades API
    app.router.add_get("/api/user/trades", handle_paper_trades_list)
    app.router.add_post("/api/user/trades", handle_paper_trade_create)
    app.router.add_post("/api/user/trades/{id}", handle_paper_trade_update)

    # AI Chat
    app.router.add_post("/api/chat", handle_chat_api)
    app.router.add_get("/api/admin/ai-settings", handle_ai_settings_get)
    app.router.add_post("/api/admin/ai-settings", handle_ai_settings_post)
    app.router.add_post("/api/admin/ai-test", handle_ai_test)

    # Admin user management
    app.router.add_get("/api/admin/users", handle_admin_users)
    app.router.add_post("/api/admin/unlock-user", handle_admin_unlock_user)
    app.router.add_post("/api/admin/deactivate-user", handle_admin_deactivate_user)
    app.router.add_get("/api/admin/audit", handle_admin_audit_log)
    app.router.add_post("/api/admin/auto-trade/run", handle_auto_trade_run)
    app.router.add_get("/api/admin/auto-trade/status", handle_auto_trade_status)

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
