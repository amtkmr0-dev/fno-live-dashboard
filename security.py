#!/usr/bin/env python3
"""
security.py — Security utilities for Quantra Terminal.

Provides:
  - RateLimiter: in-memory sliding-window rate limiting per key
  - CSRFProtection: double-submit cookie CSRF tokens
  - SecurityHeaders: middleware to add security headers to all responses
  - InputValidator: strict input validation + sanitization
  - BruteForceGuard: IP-based brute force detection with exponential backoff

All designed for use with aiohttp in auth_proxy.py.
"""

import hashlib
import hmac
import html
import re
import secrets
import time
import logging
from collections import defaultdict

log = logging.getLogger("quantra.security")

# ============================================================
# RATE LIMITER (in-memory sliding window)
# ============================================================

class RateLimiter:
    """
    Sliding-window rate limiter.

    Usage:
        limiter = RateLimiter()
        limiter.configure("login", max_requests=5, window_seconds=900)  # 5 per 15m
        if not limiter.allow("login", ip_address):
            return 429  # Too Many Requests
    """

    def __init__(self):
        self._configs = {}          # name -> {max_requests, window}
        self._windows = {}          # (name, key) -> [timestamps]
        self._last_cleanup = time.time()
        self._cleanup_interval = 60  # cleanup stale entries every 60s

    def configure(self, name, max_requests, window_seconds):
        """Configure a rate limit rule."""
        self._configs[name] = {
            "max": max_requests,
            "window": window_seconds,
        }

    def allow(self, name, key):
        """Check if a request is allowed. Returns True if OK, False if limited."""
        cfg = self._configs.get(name)
        if not cfg:
            return True  # no rule = allow

        now = time.time()
        bucket_key = (name, key)
        window = cfg["window"]

        # Get or create timestamp list
        if bucket_key not in self._windows:
            self._windows[bucket_key] = []

        # Filter to current window
        timestamps = [t for t in self._windows[bucket_key] if now - t < window]
        self._windows[bucket_key] = timestamps

        if len(timestamps) >= cfg["max"]:
            return False

        timestamps.append(now)
        self._maybe_cleanup(now)
        return True

    def remaining(self, name, key):
        """Get remaining requests in the current window."""
        cfg = self._configs.get(name)
        if not cfg:
            return 999

        now = time.time()
        bucket_key = (name, key)
        timestamps = self._windows.get(bucket_key, [])
        active = [t for t in timestamps if now - t < cfg["window"]]
        return max(0, cfg["max"] - len(active))

    def retry_after(self, name, key):
        """Get seconds until next request is allowed."""
        cfg = self._configs.get(name)
        if not cfg:
            return 0

        now = time.time()
        bucket_key = (name, key)
        timestamps = self._windows.get(bucket_key, [])
        active = sorted([t for t in timestamps if now - t < cfg["window"]])

        if len(active) < cfg["max"]:
            return 0
        # Oldest entry in window — time until it expires
        return max(0, int(active[0] + cfg["window"] - now) + 1)

    def _maybe_cleanup(self, now):
        """Periodically clean up stale entries to prevent memory growth."""
        if now - self._last_cleanup < self._cleanup_interval:
            return
        self._last_cleanup = now
        stale_keys = []
        for bucket_key, timestamps in self._windows.items():
            name = bucket_key[0]
            cfg = self._configs.get(name, {})
            window = cfg.get("window", 3600)
            fresh = [t for t in timestamps if now - t < window]
            if not fresh:
                stale_keys.append(bucket_key)
            else:
                self._windows[bucket_key] = fresh
        for k in stale_keys:
            del self._windows[k]


# ============================================================
# BRUTE FORCE GUARD (IP-based with exponential backoff)
# ============================================================

class BruteForceGuard:
    """
    Tracks failed authentication attempts per IP.
    Implements exponential backoff: after threshold failures,
    lockout duration doubles with each subsequent failure.

    Usage:
        guard = BruteForceGuard(threshold=5, base_lockout=60)
        if guard.is_locked(ip):
            return 429
        if login_failed:
            guard.record_failure(ip)
        else:
            guard.record_success(ip)
    """

    def __init__(self, threshold=5, base_lockout=60, max_lockout=3600):
        """
        threshold: failures before lockout kicks in
        base_lockout: initial lockout in seconds (doubles each time)
        max_lockout: maximum lockout duration in seconds
        """
        self.threshold = threshold
        self.base_lockout = base_lockout
        self.max_lockout = max_lockout
        self._attempts = {}  # ip -> {count, last_attempt, locked_until}

    def is_locked(self, ip):
        """Check if an IP is currently locked out."""
        rec = self._attempts.get(ip)
        if not rec:
            return False
        if rec.get("locked_until", 0) > time.time():
            return True
        return False

    def lockout_remaining(self, ip):
        """Seconds remaining on lockout. 0 if not locked."""
        rec = self._attempts.get(ip)
        if not rec:
            return 0
        remaining = rec.get("locked_until", 0) - time.time()
        return max(0, int(remaining))

    def record_failure(self, ip):
        """Record a failed attempt. Triggers lockout if threshold exceeded."""
        now = time.time()
        if ip not in self._attempts:
            self._attempts[ip] = {"count": 0, "last_attempt": now, "locked_until": 0}

        rec = self._attempts[ip]
        rec["count"] += 1
        rec["last_attempt"] = now

        if rec["count"] >= self.threshold:
            # Exponential backoff: base * 2^(failures_over_threshold)
            extra = rec["count"] - self.threshold
            lockout = min(self.base_lockout * (2 ** extra), self.max_lockout)
            rec["locked_until"] = now + lockout
            log.warning(
                f"Brute force lockout: {ip} (attempts={rec['count']}, "
                f"lockout={lockout}s)"
            )

    def record_success(self, ip):
        """Clear failure record on successful login."""
        if ip in self._attempts:
            del self._attempts[ip]

    def get_attempt_count(self, ip):
        """Get current failure count for an IP."""
        rec = self._attempts.get(ip)
        return rec["count"] if rec else 0

    def cleanup_stale(self, max_age=7200):
        """Remove IPs that haven't had attempts in max_age seconds."""
        now = time.time()
        stale = [ip for ip, rec in self._attempts.items()
                 if now - rec["last_attempt"] > max_age and rec.get("locked_until", 0) < now]
        for ip in stale:
            del self._attempts[ip]


# ============================================================
# CSRF PROTECTION (double-submit cookie + header)
# ============================================================

class CSRFProtection:
    """
    Double-submit CSRF protection.

    Flow:
    1. Server sets a `quantra_csrf` cookie with a random token (non-HttpOnly so JS can read it)
    2. JS reads the cookie and sends it back as `X-CSRF-Token` header on mutations
    3. Server validates header matches cookie

    GET/HEAD/OPTIONS are exempt (safe methods).
    API routes with JSON bodies are the primary protection target.
    """

    COOKIE_NAME = "quantra_csrf"
    HEADER_NAME = "X-CSRF-Token"
    SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
    # Exempt API paths (login, register) that can't have the token yet
    EXEMPT_PATHS = {"/api/auth/login", "/api/auth/register"}

    @staticmethod
    def generate_token():
        """Generate a cryptographically secure CSRF token."""
        return secrets.token_hex(32)

    @classmethod
    def get_or_set_token(cls, request, response):
        """Ensure a CSRF cookie is set. Returns the token."""
        existing = request.cookies.get(cls.COOKIE_NAME)
        if existing and len(existing) == 64:  # valid hex token
            return existing
        token = cls.generate_token()
        response.set_cookie(
            cls.COOKIE_NAME, token,
            max_age=86400,
            httponly=False,   # JS must read this
            samesite="Strict",
            path="/",
        )
        return token

    @classmethod
    def validate(cls, request):
        """
        Validate CSRF for state-changing requests.
        Returns (ok: bool, reason: str).
        """
        if request.method in cls.SAFE_METHODS:
            return True, ""

        if request.path in cls.EXEMPT_PATHS:
            return True, ""

        # Only enforce on /api/ routes (non-proxy paths)
        if not request.path.startswith("/api/"):
            return True, ""

        cookie_token = request.cookies.get(cls.COOKIE_NAME, "")
        header_token = request.headers.get(cls.HEADER_NAME, "")

        if not cookie_token or not header_token:
            return False, "Missing CSRF token"

        if not hmac.compare_digest(cookie_token, header_token):
            return False, "CSRF token mismatch"

        return True, ""


# ============================================================
# SECURITY HEADERS
# ============================================================

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-XSS-Protection": "1; mode=block",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cache-Control": "no-store, no-cache, must-revalidate, private",
    "Pragma": "no-cache",
}

# Strict CSP for HTML pages served by auth_proxy
# Allows inline styles (needed for our single-file HTML approach) but blocks eval
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://fonts.googleapis.com https://fonts.gstatic.com https://checkout.razorpay.com https://unpkg.com https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com https://frontend-cdn.perplexity.ai; "
    "img-src 'self' data: blob: https://*.razorpay.com; "
    "connect-src 'self' ws: wss: https://api.razorpay.com; "
    "frame-src 'self' https://api.razorpay.com https://checkout.razorpay.com; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self';"
)


def apply_security_headers(response):
    """Apply security headers to an aiohttp response."""
    for header, value in SECURITY_HEADERS.items():
        response.headers[header] = value
    # Add CSP for HTML responses
    content_type = response.content_type or ""
    if "text/html" in content_type:
        response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
    return response


# ============================================================
# INPUT VALIDATOR
# ============================================================

class InputValidator:
    """Strict input validation for user-supplied data."""

    # Username: 3-30 chars, alphanumeric + underscore
    USERNAME_RE = re.compile(r'^[a-zA-Z][a-zA-Z0-9_]{2,29}$')
    # Email: basic format check
    EMAIL_RE = re.compile(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$')
    # Display name: 1-50 chars, no control characters
    DISPLAY_NAME_RE = re.compile(r'^[^\x00-\x1f]{1,50}$')
    # Phone: digits, spaces, +, -, (, ) only
    PHONE_RE = re.compile(r'^[\d\s+\-()]{6,20}$')
    # Symbol: uppercase letters + numbers, 1-20 chars
    SYMBOL_RE = re.compile(r'^[A-Z][A-Z0-9&\-]{0,19}$')

    @classmethod
    def username(cls, value):
        """Validate username. Returns (ok, cleaned_value, error)."""
        if not value or not isinstance(value, str):
            return False, "", "Username is required"
        v = value.strip()
        if not cls.USERNAME_RE.match(v):
            return False, "", "Username must be 3-30 chars, start with a letter, contain only letters/numbers/underscore"
        return True, v, ""

    @classmethod
    def email(cls, value):
        """Validate email. Returns (ok, cleaned_value, error)."""
        if not value:
            return True, None, ""  # email is optional
        v = value.strip().lower()
        if len(v) > 254:
            return False, "", "Email too long"
        if not cls.EMAIL_RE.match(v):
            return False, "", "Invalid email format"
        return True, v, ""

    @classmethod
    def password(cls, value, min_length=8):
        """
        Validate password strength.
        Requires: min_length chars, at least 1 uppercase, 1 lowercase, 1 digit.
        Returns (ok, error).
        """
        if not value or not isinstance(value, str):
            return False, "Password is required"
        if len(value) < min_length:
            return False, f"Password must be at least {min_length} characters"
        if len(value) > 128:
            return False, "Password too long (max 128 characters)"
        if not re.search(r'[a-z]', value):
            return False, "Password must contain at least one lowercase letter"
        if not re.search(r'[A-Z]', value):
            return False, "Password must contain at least one uppercase letter"
        if not re.search(r'\d', value):
            return False, "Password must contain at least one digit"
        return True, ""

    @classmethod
    def display_name(cls, value):
        """Validate display name. Returns (ok, cleaned, error)."""
        if not value:
            return True, None, ""
        v = html.escape(str(value).strip()[:50])
        if not v:
            return True, None, ""
        return True, v, ""

    @classmethod
    def phone(cls, value):
        """Validate phone number. Returns (ok, cleaned, error)."""
        if not value:
            return True, None, ""
        v = str(value).strip()[:20]
        if not cls.PHONE_RE.match(v):
            return False, "", "Invalid phone format"
        return True, v, ""

    @classmethod
    def bio(cls, value, max_length=500):
        """Validate/sanitize bio text. Returns (ok, cleaned, error)."""
        if not value:
            return True, None, ""
        v = html.escape(str(value).strip()[:max_length])
        return True, v, ""

    @classmethod
    def symbol(cls, value):
        """Validate trading symbol. Returns (ok, cleaned, error)."""
        if not value or not isinstance(value, str):
            return False, "", "Symbol is required"
        v = value.strip().upper()
        if not cls.SYMBOL_RE.match(v):
            return False, "", "Invalid symbol format"
        return True, v, ""

    @classmethod
    def sanitize_string(cls, value, max_length=200):
        """Generic string sanitization — escape HTML, truncate."""
        if not value:
            return ""
        return html.escape(str(value).strip()[:max_length])

    @classmethod
    def positive_int(cls, value, min_val=1, max_val=10000):
        """Validate a positive integer within bounds."""
        try:
            v = int(value)
            if v < min_val or v > max_val:
                return False, 0, f"Value must be between {min_val} and {max_val}"
            return True, v, ""
        except (TypeError, ValueError):
            return False, 0, "Must be a valid integer"

    @classmethod
    def positive_float(cls, value, min_val=0, max_val=100000000):
        """Validate a positive float within bounds."""
        try:
            v = float(value)
            if v < min_val or v > max_val:
                return False, 0, f"Value must be between {min_val} and {max_val}"
            return True, v, ""
        except (TypeError, ValueError):
            return False, 0, "Must be a valid number"


# ============================================================
# AUDIT LOGGER
# ============================================================

class AuditLogger:
    """
    Security event logging for audit trail.
    Writes to a dedicated audit log and optionally to the database.
    """

    def __init__(self, db=None):
        self.db = db
        self._log = logging.getLogger("quantra.audit")
        # Set up a separate file handler for audit events
        handler = logging.FileHandler("audit.log")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s\t%(levelname)s\t%(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        ))
        self._log.addHandler(handler)
        self._log.setLevel(logging.INFO)

    def log(self, event_type, ip=None, username=None, user_id=None, details=None):
        """Log a security event."""
        parts = [f"event={event_type}"]
        if ip:
            parts.append(f"ip={ip}")
        if username:
            parts.append(f"user={username}")
        if user_id:
            parts.append(f"uid={user_id}")
        if details:
            parts.append(f"details={details}")

        msg = " | ".join(parts)
        self._log.info(msg)

        # Also log to database if available
        if self.db:
            try:
                self.db.log_audit_event(event_type, ip, username, user_id, details)
            except Exception:
                pass  # Don't let audit DB errors break the request

    def login_success(self, ip, username, user_id):
        self.log("LOGIN_SUCCESS", ip=ip, username=username, user_id=user_id)

    def login_failed(self, ip, username, details=None):
        self.log("LOGIN_FAILED", ip=ip, username=username, details=details)

    def login_locked(self, ip, username=None):
        self.log("LOGIN_LOCKED", ip=ip, username=username, details="Account locked due to repeated failures")

    def registration(self, ip, username, user_id):
        self.log("REGISTRATION", ip=ip, username=username, user_id=user_id)

    def password_change(self, ip, username, user_id):
        self.log("PASSWORD_CHANGE", ip=ip, username=username, user_id=user_id)

    def logout_all(self, ip, username, user_id):
        self.log("LOGOUT_ALL", ip=ip, username=username, user_id=user_id)

    def admin_action(self, ip, username, user_id, action):
        self.log("ADMIN_ACTION", ip=ip, username=username, user_id=user_id, details=action)

    def rate_limited(self, ip, rule_name):
        self.log("RATE_LIMITED", ip=ip, details=f"rule={rule_name}")

    def csrf_violation(self, ip, path):
        self.log("CSRF_VIOLATION", ip=ip, details=f"path={path}")

    def suspicious_input(self, ip, field, value_preview):
        self.log("SUSPICIOUS_INPUT", ip=ip, details=f"field={field} value={value_preview[:50]}")
