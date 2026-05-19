#!/usr/bin/env python3
"""
Patch ws_server.py to add authentication endpoints and session middleware.

Adds:
  POST /api/auth/login   - validate credentials, return session token
  POST /api/auth/verify  - check if token is valid
  GET  /api/auth/logout   - clear session
  GET  /login             - serve login.html

Also adds auth middleware that redirects unauthenticated HTML requests to /login.
API endpoints return 401 JSON instead of redirect.

Run on GCP server: cd ~/deploy && python3 patch_auth.py

Credentials are stored in auth_config.json (auto-created with defaults).
Change password by editing that file and restarting server.
"""

import re, os, json

SERVER_FILE = "ws_server.py"
AUTH_CONFIG = "auth_config.json"

# Create default auth config if it doesn't exist
if not os.path.exists(AUTH_CONFIG):
    default_config = {
        "users": {
            "admin": {
                "password": "quanta2026",
                "role": "admin"
            }
        },
        "session_timeout_hours": 72,
        "secret_key": os.urandom(32).hex()
    }
    with open(AUTH_CONFIG, 'w') as f:
        json.dump(default_config, f, indent=2)
    print(f"Created {AUTH_CONFIG} with default credentials:")
    print(f"  Username: admin")
    print(f"  Password: quanta2026")
    print(f"  CHANGE THESE in {AUTH_CONFIG}!")
else:
    print(f"{AUTH_CONFIG} already exists, keeping existing credentials.")

html = open(SERVER_FILE, 'r').read()

# Check if already patched
if 'api/auth/login' in html:
    print("Auth endpoints already present. Skipping patch.")
    exit(0)

#  Step 1: Add imports at top 
imports_to_add = """
import hashlib
import secrets
import time as time_module
"""

# Find the last import line and add after it
import_match = re.search(r'^(import .+|from .+ import .+)$', html, re.MULTILINE)
if import_match:
    # Find the last import block
    last_import_end = 0
    for m in re.finditer(r'^(?:import .+|from .+ import .+)$', html, re.MULTILINE):
        last_import_end = m.end()
    html = html[:last_import_end] + imports_to_add + html[last_import_end:]
    print("Added auth imports.")

#  Step 2: Add auth module code before the app routes 
# Find a good insertion point - before the first route handler
auth_code = '''
# ============================================================
# AUTH MODULE
# ============================================================

_auth_sessions = {}  # token -> {user, role, created}

def _load_auth_config():
    """Load auth config from auth_config.json."""
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auth_config.json")
    try:
        with open(cfg_path, 'r') as f:
            return json.load(f)
    except Exception:
        return {"users": {"admin": {"password": "quanta2026", "role": "admin"}},
                "session_timeout_hours": 72, "secret_key": "fallback"}

def _hash_password(pw, salt):
    """Simple salted hash."""
    return hashlib.sha256((salt + pw).encode()).hexdigest()

def _create_session(username, role):
    """Create a new session token."""
    token = secrets.token_urlsafe(48)
    _auth_sessions[token] = {
        "user": username,
        "role": role,
        "created": time_module.time()
    }
    return token

def _validate_session(token):
    """Check if session token is valid and not expired."""
    if not token or token not in _auth_sessions:
        return None
    sess = _auth_sessions[token]
    cfg = _load_auth_config()
    timeout = cfg.get("session_timeout_hours", 72) * 3600
    if time_module.time() - sess["created"] > timeout:
        del _auth_sessions[token]
        return None
    return sess

def _get_token_from_request(request):
    """Extract session token from request (header or query param)."""
    # Check Authorization header
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    # Check cookie
    cookie = request.cookies.get("q_session")
    if cookie:
        return cookie
    # Check query param (for WebSocket upgrade)
    return request.query.get("token", "")

# Auth route handlers
async def handle_login(request):
    """POST /api/auth/login"""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"success": False, "error": "Invalid request"}, status=400)

    username = body.get("username", "").strip()
    password = body.get("password", "")
    cfg = _load_auth_config()
    users = cfg.get("users", {})

    if username in users and users[username].get("password") == password:
        token = _create_session(username, users[username].get("role", "user"))
        resp = web.json_response({"success": True, "token": token, "user": username})
        resp.set_cookie("q_session", token, max_age=cfg.get("session_timeout_hours", 72)*3600,
                        httponly=True, samesite="Lax")
        return resp
    else:
        return web.json_response({"success": False, "error": "Invalid credentials"}, status=401)

async def handle_verify(request):
    """POST /api/auth/verify"""
    try:
        body = await request.json()
        token = body.get("token", "")
    except Exception:
        token = _get_token_from_request(request)

    sess = _validate_session(token)
    if sess:
        return web.json_response({"valid": True, "user": sess["user"], "role": sess["role"]})
    return web.json_response({"valid": False}, status=401)

async def handle_logout(request):
    """GET /api/auth/logout"""
    token = _get_token_from_request(request)
    if token and token in _auth_sessions:
        del _auth_sessions[token]
    resp = web.json_response({"success": True})
    resp.del_cookie("q_session")
    return resp

async def serve_login_page(request):
    """GET /login"""
    login_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "login.html")
    if os.path.exists(login_path):
        return web.FileResponse(login_path)
    return web.Response(text="Login page not found", status=404)

# Auth middleware
@web.middleware
async def auth_middleware(request, handler):
    """Check authentication for all requests except login/auth endpoints."""
    path = request.path

    # Skip auth for these paths
    skip_paths = ["/login", "/api/auth/", "/theme.css", "/favicon.ico"]
    for sp in skip_paths:
        if path.startswith(sp) or path == sp:
            return await handler(request)

    # Check session
    token = _get_token_from_request(request)
    sess = _validate_session(token)

    if not sess:
        # For API requests, return 401 JSON
        if path.startswith("/api/"):
            return web.json_response({"error": "Unauthorized"}, status=401)
        # For WebSocket, let it through (WS handler will check)
        if path == "/ws":
            return await handler(request)
        # For HTML pages, redirect to login
        return web.HTTPFound("/login")

    # Store user info in request for downstream handlers
    request["user"] = sess["user"]
    request["role"] = sess["role"]
    return await handler(request)

'''

# Find where routes are registered (look for app.router.add or similar patterns)
# Insert auth code before the first route registration
route_match = re.search(r'(app\.router\.add_)', html)
if not route_match:
    # Try alternative: find before app = web.Application
    route_match = re.search(r'(app\s*=\s*web\.Application)', html)

if route_match:
    insert_pos = route_match.start()
    html = html[:insert_pos] + auth_code + "\n" + html[insert_pos:]
    print("Inserted auth module code.")
else:
    print("WARNING: Could not find insertion point for auth code.")
    print("You may need to manually insert the auth code.")

#  Step 3: Add middlewares to app creation 
# Find app = web.Application(...) and add middleware
app_match = re.search(r'app\s*=\s*web\.Application\(\s*\)', html)
if app_match:
    html = html[:app_match.start()] + "app = web.Application(middlewares=[auth_middleware])" + html[app_match.end():]
    print("Added auth_middleware to app creation.")
else:
    # Try with existing args
    app_match = re.search(r'(app\s*=\s*web\.Application\()([^)]*)\)', html)
    if app_match:
        existing_args = app_match.group(2).strip()
        if 'middlewares' not in existing_args:
            if existing_args:
                new_args = existing_args + ", middlewares=[auth_middleware]"
            else:
                new_args = "middlewares=[auth_middleware]"
            html = html[:app_match.start()] + "app = web.Application(" + new_args + ")" + html[app_match.end():]
            print("Added auth_middleware to existing app creation.")
        else:
            print("App already has middlewares= argument.")
    else:
        print("WARNING: Could not find app = web.Application() to add middleware.")

#  Step 4: Register auth routes 
# Find the last app.router.add_ line and add auth routes after it
last_route_end = 0
for m in re.finditer(r'app\.router\.add_\w+\([^)]+\)', html):
    last_route_end = m.end()

if last_route_end > 0:
    auth_routes = """
# Auth routes
app.router.add_post('/api/auth/login', handle_login)
app.router.add_post('/api/auth/verify', handle_verify)
app.router.add_get('/api/auth/logout', handle_logout)
app.router.add_get('/login', serve_login_page)
"""
    html = html[:last_route_end] + "\n" + auth_routes + html[last_route_end:]
    print("Added auth route registrations.")
else:
    print("WARNING: Could not find existing routes to add auth routes after.")

#  Step 5: Also add charset signal if not present 
if 'charset' not in html and 'on_response_prepare' not in html:
    # Find app creation and add charset signal after
    app_match = re.search(r'(app\s*=\s*web\.Application\([^)]*\))', html)
    if app_match:
        charset_code = """

# Force UTF-8 charset on HTML responses
async def charset_signal(request, response):
    ct = response.headers.get('Content-Type', '')
    if ct.startswith('text/html') and 'charset' not in ct:
        response.headers['Content-Type'] = 'text/html; charset=utf-8'
app.on_response_prepare.append(charset_signal)
"""
        pos = app_match.end()
        html = html[:pos] + charset_code + html[pos:]
        print("Added charset UTF-8 signal.")

open(SERVER_FILE, 'w').write(html)
print(f"\nPatched {SERVER_FILE} ({len(html)} bytes)")
print(f"\nDeployment steps:")
print(f"  1. Copy login.html and theme.css to ~/deploy/")
print(f"  2. Run: cd ~/deploy && python3 patch_auth.py")
print(f"  3. Restart server: pkill -f ws_server.py; sleep 2; nohup venv/bin/python3 ws_server.py >> server.log 2>&1 &")
print(f"  4. Visit http://34.173.9.221:8080/login")
print(f"  5. Login with admin / quanta2026")
print(f"  6. Change password in auth_config.json")
