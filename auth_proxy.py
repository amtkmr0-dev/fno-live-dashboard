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

try:
    from chat_analysis import run_analysis, get_upstox_token
    ANALYSIS_AVAILABLE = True
except ImportError:
    ANALYSIS_AVAILABLE = False
    log = logging.getLogger("auth_proxy")

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
    """POST /api/chat - AI chat via active provider (Perplexity or NVIDIA)."""
    # Auth check
    token = get_session_from_request(request)
    session = validate_session(token)
    if not session:
        return web.json_response({"error": "Unauthorized"}, status=401)

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

    history = data.get("history", [])
    focused_stock = data.get("focused_stock")
    dash_context = data.get("context", {})

    # Build messages array (OpenAI-compatible for both providers)
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

    # Add recent history (last 6 messages for context, skip system)
    for msg in history[-6:]:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})

    # Ensure the last message is the current user message
    if not messages or messages[-1].get("content") != user_message:
        messages.append({"role": "user", "content": user_message})

    # Call provider API (both use OpenAI-compatible format)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    # More tokens when deep analysis is present
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

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    updated = []

    # Active provider
    if "active_provider" in data:
        ap = data["active_provider"]
        if ap in AI_PROVIDERS:
            _config["active_ai_provider"] = ap
            updated.append(f"active_provider={ap}")
        else:
            return web.json_response({"error": f"Unknown provider: {ap}"}, status=400)

    # Provider-specific keys and models
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

    # Persist to disk
    with open(CONFIG_FILE, "w") as f:
        json.dump(_config, f, indent=2)

    log.info(f"AI settings updated by {session['user']}: {', '.join(updated)}")
    return web.json_response({"ok": True, "updated": updated})


async def handle_ai_test(request):
    """POST /api/admin/ai-test - Test the active (or specified) provider connection."""
    token = get_session_from_request(request)
    session = validate_session(token)
    if not session or session["role"] != "admin":
        return web.json_response({"error": "Admin access required"}, status=403)

    try:
        data = await request.json()
    except Exception:
        data = {}

    # Allow testing a specific provider, or default to active
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
    app.router.add_get("/api/admin/ai-settings", handle_ai_settings_get)
    app.router.add_post("/api/admin/ai-settings", handle_ai_settings_post)
    app.router.add_post("/api/admin/ai-test", handle_ai_test)

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
