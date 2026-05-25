#!/usr/bin/env python3
"""
db.py — SQLite database layer for Quantra Terminal.

Security features:
  - PBKDF2-HMAC-SHA256 password hashing (600K iterations, OWASP 2024 rec)
  - Account lockout tracking (failed_attempts + locked_until columns)
  - Login audit trail (login_audit table)
  - Session IP binding
  - Schema versioning with auto-migration

Handles: users, sessions, user settings, paper trades (manual + auto),
auto-trade signals, login audit, security events.

Usage:
    from db import DB
    db = DB("quantra.db")
    db.init()  # creates tables + runs migrations
"""

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
import logging
from datetime import datetime, timedelta
from typing import Optional

log = logging.getLogger("quantra.db")

SCHEMA_VERSION = 6  # v5: email verification + plan fields; v6: billing

# ============================================================
# PASSWORD HASHING — PBKDF2-HMAC-SHA256
# ============================================================
# OWASP 2024 recommends 600K iterations for PBKDF2-SHA256.
# This is stdlib-only (no bcrypt/argon2 dependency).

PBKDF2_ITERATIONS = 600_000
PBKDF2_HASH_LEN = 32  # 256 bits
HASH_ALGORITHM = "pbkdf2"  # stored in DB to detect legacy SHA-256 hashes


def hash_password_pbkdf2(password, salt=None):
    """
    Hash a password using PBKDF2-HMAC-SHA256.
    Returns (hash_hex, salt_hex, algorithm_tag).
    """
    if salt is None:
        salt = secrets.token_hex(32)  # 256-bit salt
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        PBKDF2_ITERATIONS,
        dklen=PBKDF2_HASH_LEN,
    )
    return dk.hex(), salt, HASH_ALGORITHM


def verify_password_hash(password, stored_hash, salt, algorithm=None):
    """
    Verify a password against a stored hash.
    Supports both legacy SHA-256 and new PBKDF2.
    Returns True if match.
    """
    if algorithm == "pbkdf2":
        dk = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt.encode("utf-8"),
            PBKDF2_ITERATIONS,
            dklen=PBKDF2_HASH_LEN,
        )
        return hmac.compare_digest(dk.hex(), stored_hash)
    else:
        # Legacy SHA-256 (from setup_auth.py / JSON migration)
        legacy_hash = hashlib.sha256((salt + password).encode()).hexdigest()
        return hmac.compare_digest(legacy_hash, stored_hash)


# ============================================================
# SCHEMA
# ============================================================

CREATE_TABLES = """
-- Users
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL COLLATE NOCASE,
    email TEXT UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    hash_algorithm TEXT NOT NULL DEFAULT 'sha256',
    role TEXT NOT NULL DEFAULT 'user',
    display_name TEXT,
    phone TEXT,
    avatar_url TEXT,
    bio TEXT,
    failed_login_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_login TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    email_verified INTEGER NOT NULL DEFAULT 0,
    email_otp TEXT,
    email_otp_expires_at TEXT,
    plan TEXT NOT NULL DEFAULT 'free',
    razorpay_customer_id TEXT,
    razorpay_subscription_id TEXT,
    subscription_status TEXT NOT NULL DEFAULT 'inactive',
    subscription_expires_at TEXT
);

-- User settings (1:1 with users)
CREATE TABLE IF NOT EXISTS user_settings (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    max_paper_trades_per_day INTEGER NOT NULL DEFAULT 3,
    default_lots INTEGER NOT NULL DEFAULT 1,
    default_capital REAL NOT NULL DEFAULT 50000,
    auto_exit_enabled INTEGER NOT NULL DEFAULT 1,
    auto_trail_sl INTEGER NOT NULL DEFAULT 1,
    auto_trade_enabled INTEGER NOT NULL DEFAULT 0,
    auto_trade_max_positions INTEGER NOT NULL DEFAULT 2,
    auto_trade_max_capital REAL NOT NULL DEFAULT 50000,
    preferred_sectors TEXT DEFAULT '[]',
    notification_telegram INTEGER NOT NULL DEFAULT 0,
    telegram_chat_id TEXT,
    theme TEXT DEFAULT 'bloomberg-pro',
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Sessions (replaces in-memory dict)
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL,
    ip_address TEXT,
    user_agent TEXT
);

-- Paper trades (per-user, manual + auto)
CREATE TABLE IF NOT EXISTS paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    trade_type TEXT NOT NULL DEFAULT 'manual',
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    strike REAL,
    expiry TEXT,
    entry_premium REAL,
    exit_premium REAL,
    lots INTEGER NOT NULL DEFAULT 1,
    lot_size INTEGER,
    sl_premium REAL,
    sl_spot REAL,
    t1_premium REAL,
    t2_premium REAL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    entry_reason TEXT,
    exit_reason TEXT,
    pnl REAL,
    pnl_pct REAL,
    costs_estimated REAL,
    net_pnl REAL,
    auto_signal_id INTEGER REFERENCES auto_signals(id),
    entered_at TEXT,
    exited_at TEXT,
    option_type TEXT,
    spot_at_entry REAL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Auto-trade signals (NIFTY direction -> sector -> stock analysis log)
CREATE TABLE IF NOT EXISTS auto_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    nifty_direction TEXT,
    nifty_score REAL,
    sector TEXT,
    sector_momentum REAL,
    symbol TEXT,
    analysis_summary TEXT,
    analysis_json TEXT,
    confidence REAL,
    action_taken TEXT NOT NULL DEFAULT 'logged',
    trade_id INTEGER REFERENCES paper_trades(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Login audit trail
CREATE TABLE IF NOT EXISTS login_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    ip_address TEXT,
    username TEXT,
    user_id INTEGER,
    details TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_paper_trades_user ON paper_trades(user_id);
CREATE INDEX IF NOT EXISTS idx_paper_trades_status ON paper_trades(user_id, status);
CREATE INDEX IF NOT EXISTS idx_paper_trades_date ON paper_trades(created_at);
CREATE INDEX IF NOT EXISTS idx_auto_signals_user ON auto_signals(user_id);
CREATE INDEX IF NOT EXISTS idx_auto_signals_date ON auto_signals(created_at);
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
CREATE INDEX IF NOT EXISTS idx_login_audit_date ON login_audit(created_at);
CREATE INDEX IF NOT EXISTS idx_login_audit_ip ON login_audit(ip_address);
CREATE INDEX IF NOT EXISTS idx_login_audit_user ON login_audit(username);

-- Per-user watchlist (server-side, synced across devices)
CREATE TABLE IF NOT EXISTS user_watchlist (
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    symbol     TEXT NOT NULL COLLATE NOCASE,
    added_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, symbol)
);
CREATE INDEX IF NOT EXISTS idx_watchlist_user ON user_watchlist(user_id);
"""


class DB:
    """SQLite database wrapper for Quantra Terminal."""

    # Account lockout config
    MAX_FAILED_ATTEMPTS = 5       # lock after 5 failures
    LOCKOUT_DURATION_SEC = 900    # 15 minutes

    def __init__(self, db_path="quantra.db"):
        self.db_path = db_path
        import threading
        self._local = threading.local()

    @property
    def conn(self):
        if not hasattr(self._local, 'conn'):
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            self._local.conn = conn
        return self._local.conn

    def init(self):
        """Initialize database — create tables, run migrations."""
        self.conn.executescript(CREATE_TABLES)
        self._run_migrations()
        # Set schema version
        self.conn.execute(
            "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
            ("schema_version", str(SCHEMA_VERSION)),
        )
        self.conn.commit()
        log.info(f"Database initialized: {self.db_path} (schema v{SCHEMA_VERSION})")

    def _run_migrations(self):
        """Run schema migrations for existing databases."""
        # Check current schema version
        try:
            row = self.conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            current_version = int(row["value"]) if row else 0
        except Exception:
            current_version = 0

        if current_version < 2:
            self._migrate_v1_to_v2()
        if current_version < 3:
            self._migrate_v2_to_v3()
        if current_version < 4:
            self._migrate_v3_to_v4()
        if current_version < 5:
            self._migrate_v4_to_v5()
        if current_version < 6:
            self._migrate_v5_to_v6()

    def _migrate_v1_to_v2(self):
        """
        v1 → v2 migration:
        - Add hash_algorithm column to users
        - Add failed_login_attempts, locked_until columns to users
        - Create login_audit table (handled by CREATE_TABLES)
        """
        log.info("Running migration v1 → v2...")
        try:
            # Add columns if they don't exist (SQLite doesn't have IF NOT EXISTS for ALTER)
            cols = {r[1] for r in self.conn.execute("PRAGMA table_info(users)").fetchall()}

            if "hash_algorithm" not in cols:
                self.conn.execute("ALTER TABLE users ADD COLUMN hash_algorithm TEXT NOT NULL DEFAULT 'sha256'")
                log.info("  Added hash_algorithm column")

            if "failed_login_attempts" not in cols:
                self.conn.execute("ALTER TABLE users ADD COLUMN failed_login_attempts INTEGER NOT NULL DEFAULT 0")
                log.info("  Added failed_login_attempts column")

            if "locked_until" not in cols:
                self.conn.execute("ALTER TABLE users ADD COLUMN locked_until TEXT")
                log.info("  Added locked_until column")

            self.conn.commit()
            log.info("Migration v1 → v2 complete")
        except Exception as e:
            log.warning(f"Migration v1→v2 error (may already be applied): {e}")

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    # ============================================================
    # USERS
    # ============================================================

    def create_user(self, username, email, password, role="user", display_name=None):
        """Create a new user with PBKDF2 hashing. Returns user_id."""
        pw_hash, salt, algo = hash_password_pbkdf2(password)
        try:
            cur = self.conn.execute(
                """INSERT INTO users (username, email, password_hash, salt, hash_algorithm, role, display_name)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (username.strip(), email.strip().lower() if email else None,
                 pw_hash, salt, algo, role, display_name or username),
            )
            user_id = cur.lastrowid
            # Create default settings
            self.conn.execute(
                "INSERT INTO user_settings (user_id) VALUES (?)", (user_id,)
            )
            self.conn.commit()
            log.info(f"User created: {username} (id={user_id}, role={role}, hash=PBKDF2)")
            return user_id
        except sqlite3.IntegrityError as e:
            self.conn.rollback()
            err = str(e).lower()
            if "username" in err:
                raise ValueError(f"Username '{username}' already taken")
            elif "email" in err:
                raise ValueError(f"Email '{email}' already registered")
            raise

    def verify_password(self, username, password):
        """
        Verify login credentials with account lockout and auto-upgrade.
        Returns user dict or None.
        """
        # Find user by username or email
        row = self.conn.execute(
            "SELECT * FROM users WHERE username = ? AND is_active = 1",
            (username,),
        ).fetchone()
        if not row:
            row = self.conn.execute(
                "SELECT * FROM users WHERE email = ? AND is_active = 1",
                (username.lower(),),
            ).fetchone()
        if not row:
            return None

        # Check account lockout
        if row["locked_until"]:
            try:
                lock_time = datetime.fromisoformat(row["locked_until"])
                if lock_time > datetime.utcnow():
                    remaining = int((lock_time - datetime.utcnow()).total_seconds())
                    log.warning(f"Login blocked — account locked: {row['username']} ({remaining}s remaining)")
                    return None
                else:
                    # Lockout expired — reset
                    self.conn.execute(
                        "UPDATE users SET failed_login_attempts = 0, locked_until = NULL WHERE id = ?",
                        (row["id"],),
                    )
            except ValueError:
                pass  # malformed date, ignore

        # Verify password (handles both legacy SHA-256 and PBKDF2)
        algo = row["hash_algorithm"] if "hash_algorithm" in row.keys() else "sha256"
        if not verify_password_hash(password, row["password_hash"], row["salt"], algo):
            # Record failed attempt
            new_count = (row["failed_login_attempts"] or 0) + 1
            locked_until = None
            if new_count >= self.MAX_FAILED_ATTEMPTS:
                locked_until = (datetime.utcnow() + timedelta(seconds=self.LOCKOUT_DURATION_SEC)).isoformat()
                log.warning(f"Account locked: {row['username']} after {new_count} failed attempts")
            self.conn.execute(
                "UPDATE users SET failed_login_attempts = ?, locked_until = ? WHERE id = ?",
                (new_count, locked_until, row["id"]),
            )
            self.conn.commit()
            return None

        # Success — reset lockout counters
        self.conn.execute(
            "UPDATE users SET failed_login_attempts = 0, locked_until = NULL, last_login = datetime('now') WHERE id = ?",
            (row["id"],),
        )
        self.conn.commit()

        # Auto-upgrade legacy SHA-256 → PBKDF2 on successful login
        if algo != "pbkdf2":
            new_hash, new_salt, new_algo = hash_password_pbkdf2(password)
            self.conn.execute(
                "UPDATE users SET password_hash = ?, salt = ?, hash_algorithm = ? WHERE id = ?",
                (new_hash, new_salt, new_algo, row["id"]),
            )
            self.conn.commit()
            log.info(f"Password hash upgraded to PBKDF2 for user: {row['username']}")

        return dict(row)

    def get_user(self, user_id):
        """Get user by ID."""
        row = self.conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    def set_email_otp(self, user_id, otp, expires_at):
        """Store an email OTP for verification."""
        self.conn.execute(
            "UPDATE users SET email_otp = ?, email_otp_expires_at = ? WHERE id = ?",
            (otp, expires_at, user_id),
        )
        self.conn.commit()

    def verify_email_otp(self, user_id, otp):
        """Verify OTP. Returns True on success, False otherwise."""
        row = self.conn.execute(
            "SELECT email_otp, email_otp_expires_at FROM users WHERE id = ?",
            (user_id,)
        ).fetchone()
        if not row or not row["email_otp"]:
            return False, "No OTP found"
        if row["email_otp"] != otp:
            return False, "Invalid OTP"
        from datetime import datetime
        try:
            exp = datetime.fromisoformat(row["email_otp_expires_at"])
        except ValueError:
            return False, "Invalid expiry date format"
        if exp < datetime.utcnow():
            return False, "OTP expired"
        # Mark verified, clear OTP
        self.conn.execute(
            "UPDATE users SET email_verified = 1, email_otp = NULL, email_otp_expires_at = NULL WHERE id = ?",
            (user_id,)
        )
        self.conn.commit()
        return True, "OK"

    def get_user_plan(self, user_id):
        """Get the user's current plan: 'free' or 'pro'."""
        row = self.conn.execute(
            "SELECT plan, subscription_status, subscription_expires_at FROM users WHERE id = ?",
            (user_id,)
        ).fetchone()
        if not row:
            return "free"
        # Check if pro subscription is still active
        if row["plan"] == "pro" and row["subscription_status"] == "active":
            if row["subscription_expires_at"]:
                from datetime import datetime
                try:
                    exp = datetime.fromisoformat(row["subscription_expires_at"])
                except ValueError:
                    return "free"
                if exp < datetime.utcnow():
                    # Expired — downgrade
                    self.conn.execute(
                        "UPDATE users SET plan = 'free', subscription_status = 'expired' WHERE id = ?",
                        (user_id,)
                    )
                    self.conn.commit()
                    return "free"
            return "pro"
        return "free"

    def update_subscription(self, user_id, **fields):
        """Update billing/subscription fields for a user."""
        allowed = {"plan", "razorpay_customer_id", "razorpay_subscription_id",
                   "subscription_status", "subscription_expires_at"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return False
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        self.conn.execute(
            f"UPDATE users SET {set_clause} WHERE id = ?",
            list(updates.values()) + [user_id]
        )
        self.conn.commit()
        return True

    def get_user_by_username(self, username):
        """Get user by username."""
        row = self.conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
        return dict(row) if row else None

    def update_user_profile(self, user_id, **fields):
        """Update user profile fields (display_name, phone, bio, avatar_url)."""
        allowed = {"display_name", "phone", "bio", "avatar_url", "email"}
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not updates:
            return False
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [user_id]
        self.conn.execute(f"UPDATE users SET {set_clause} WHERE id = ?", values)
        self.conn.commit()
        return True

    def change_password(self, user_id, old_password, new_password):
        """Change user password — verifies old, hashes new with PBKDF2."""
        row = self.conn.execute(
            "SELECT password_hash, salt, hash_algorithm FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if not row:
            return False, "User not found"

        algo = row["hash_algorithm"] if "hash_algorithm" in row.keys() else "sha256"
        if not verify_password_hash(old_password, row["password_hash"], row["salt"], algo):
            return False, "Current password is incorrect"

        new_hash, new_salt, new_algo = hash_password_pbkdf2(new_password)
        self.conn.execute(
            "UPDATE users SET password_hash = ?, salt = ?, hash_algorithm = ? WHERE id = ?",
            (new_hash, new_salt, new_algo, user_id),
        )
        self.conn.commit()
        return True, "Password changed"

    def list_users(self, include_inactive=False):
        """List all users (admin use)."""
        q = "SELECT id, username, email, role, display_name, created_at, last_login, is_active, failed_login_attempts, locked_until FROM users"
        if not include_inactive:
            q += " WHERE is_active = 1"
        q += " ORDER BY created_at DESC"
        return [dict(r) for r in self.conn.execute(q).fetchall()]

    def count_users(self):
        """Count active users."""
        return self.conn.execute(
            "SELECT COUNT(*) FROM users WHERE is_active = 1"
        ).fetchone()[0]

    def deactivate_user(self, user_id):
        """Soft-delete a user (admin action)."""
        self.conn.execute("UPDATE users SET is_active = 0 WHERE id = ?", (user_id,))
        # Kill all their sessions
        self.conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        self.conn.commit()

    def unlock_user(self, user_id):
        """Manually unlock a locked-out user (admin action)."""
        self.conn.execute(
            "UPDATE users SET failed_login_attempts = 0, locked_until = NULL WHERE id = ?",
            (user_id,),
        )
        self.conn.commit()

    # ============================================================
    # USER SETTINGS
    # ============================================================

    def get_user_settings(self, user_id):
        """Get user settings."""
        row = self.conn.execute(
            "SELECT * FROM user_settings WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row:
            d = dict(row)
            # Parse JSON fields
            try:
                d["preferred_sectors"] = json.loads(d.get("preferred_sectors") or "[]")
            except (json.JSONDecodeError, TypeError):
                d["preferred_sectors"] = []
            return d
        return None

    def get_all_auto_trade_settings(self):
        """Get settings for all users who have auto-trading enabled."""
        rows = self.conn.execute(
            "SELECT * FROM user_settings WHERE auto_trade_enabled = 1"
        ).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            try:
                d["preferred_sectors"] = json.loads(d.get("preferred_sectors") or "[]")
            except (json.JSONDecodeError, TypeError):
                d["preferred_sectors"] = []
            result.append(d)
        return result

    def update_user_settings(self, user_id, **fields):
        """Update user settings."""
        allowed = {
            "max_paper_trades_per_day", "default_lots", "default_capital",
            "auto_exit_enabled", "auto_trail_sl",
            "auto_trade_enabled", "auto_trade_max_positions", "auto_trade_max_capital",
            "preferred_sectors",
            "notification_email", "notification_telegram", "telegram_chat_id",
            "column_config", "theme"
        }
        updates = {}
        for k, v in fields.items():
            if k in allowed:
                if k == "preferred_sectors" and isinstance(v, list):
                    updates[k] = json.dumps(v)
                else:
                    updates[k] = v
        if not updates:
            return False
        updates["updated_at"] = datetime.utcnow().isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [user_id]
        self.conn.execute(f"UPDATE user_settings SET {set_clause} WHERE user_id = ?", values)
        self.conn.commit()
        return True

    # ============================================================
    # SESSIONS
    # ============================================================

    def create_session(self, user_id, max_age=86400, ip=None, ua=None):
        """Create a new session with a cryptographically secure token."""
        token = secrets.token_hex(32)
        expires = (datetime.utcnow() + timedelta(seconds=max_age)).isoformat()
        self.conn.execute(
            """INSERT INTO sessions (token, user_id, expires_at, ip_address, user_agent)
               VALUES (?, ?, ?, ?, ?)""",
            (token, user_id, expires, ip, (ua or "")[:200]),
        )
        self.conn.commit()
        return token

    def validate_session(self, token):
        """Validate a session token. Returns dict with user_id, username, role or None."""
        row = self.conn.execute(
            """SELECT s.user_id, s.expires_at, s.ip_address,
                      u.username, u.role, u.display_name, u.is_active
               FROM sessions s JOIN users u ON s.user_id = u.id
               WHERE s.token = ?""",
            (token,),
        ).fetchone()
        if not row:
            return None
        if not row["is_active"]:
            self.delete_session(token)
            return None
        # Check expiry
        if row["expires_at"] < datetime.utcnow().isoformat():
            self.delete_session(token)
            return None
        # Update last_seen
        self.conn.execute(
            "UPDATE sessions SET last_seen = datetime('now') WHERE token = ?",
            (token,),
        )
        self.conn.commit()
        return {
            "user_id": row["user_id"],
            "username": row["username"],
            "role": row["role"],
            "display_name": row["display_name"],
        }

    def delete_session(self, token):
        """Delete a session (logout)."""
        self.conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        self.conn.commit()

    def delete_user_sessions(self, user_id):
        """Delete all sessions for a user."""
        self.conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        self.conn.commit()

    def count_user_sessions(self, user_id):
        """Count active sessions for a user."""
        return self.conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE user_id = ? AND datetime(expires_at) > datetime('now')",
            (user_id,),
        ).fetchone()[0]

    def cleanup_expired_sessions(self):
        """Remove all expired sessions."""
        n = self.conn.execute(
            "DELETE FROM sessions WHERE datetime(expires_at) < datetime('now')"
        ).rowcount
        self.conn.commit()
        return n

    # ============================================================
    # PAPER TRADES
    # ============================================================

    def create_paper_trade(self, user_id, symbol, direction, **kwargs):
        """Create a new paper trade. Returns trade_id."""
        fields = {
            "user_id": user_id,
            "symbol": symbol.upper(),
            "direction": direction.upper(),
            "trade_type": kwargs.get("trade_type", "manual"),
            "strike": kwargs.get("strike"),
            "expiry": kwargs.get("expiry"),
            "entry_premium": kwargs.get("entry_premium"),
            "lots": kwargs.get("lots", 1),
            "lot_size": kwargs.get("lot_size"),
            "sl_premium": kwargs.get("sl_premium"),
            "sl_spot": kwargs.get("sl_spot"),
            "t1_premium": kwargs.get("t1_premium"),
            "t2_premium": kwargs.get("t2_premium"),
            "status": kwargs.get("status", "PENDING"),
            "entry_reason": kwargs.get("entry_reason"),
            "auto_signal_id": kwargs.get("auto_signal_id"),
            "option_type": kwargs.get("option_type"),
            "spot_at_entry": kwargs.get("spot_at_entry"),
        }
        if kwargs.get("status") == "ENTERED":
            fields["entered_at"] = datetime.utcnow().isoformat()

        cols = ", ".join(fields.keys())
        placeholders = ", ".join("?" for _ in fields)
        cur = self.conn.execute(
            f"INSERT INTO paper_trades ({cols}) VALUES ({placeholders})",
            list(fields.values()),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_paper_trades(self, user_id, status=None, trade_type=None, limit=50, offset=0):
        """Get paper trades for a user, optionally filtered."""
        q = "SELECT * FROM paper_trades WHERE user_id = ?"
        params = [user_id]
        if status:
            q += " AND status = ?"
            params.append(status)
        if trade_type:
            q += " AND trade_type = ?"
            params.append(trade_type)
        q += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        return [dict(r) for r in self.conn.execute(q, params).fetchall()]

    def get_paper_trade(self, trade_id, user_id=None):
        """Get a single trade, optionally verifying user ownership."""
        q = "SELECT * FROM paper_trades WHERE id = ?"
        params = [trade_id]
        if user_id:
            q += " AND user_id = ?"
            params.append(user_id)
        row = self.conn.execute(q, params).fetchone()
        return dict(row) if row else None

    def get_all_open_paper_trades(self):
        """Get all open or pending paper trades across all users (used at WebSocket startup)."""
        q = "SELECT * FROM paper_trades WHERE status IN ('PENDING', 'ENTERED')"
        return [dict(r) for r in self.conn.execute(q).fetchall()]

    def update_paper_trade(self, trade_id, user_id=None, **fields):
        """Update a paper trade (status, exit, PnL, etc.)."""
        allowed = {
            "status", "entry_premium", "exit_premium", "lots",
            "sl_premium", "sl_spot", "t1_premium", "t2_premium",
            "exit_reason", "pnl", "pnl_pct", "costs_estimated", "net_pnl",
            "entered_at", "exited_at", "option_type", "spot_at_entry",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return False
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values())
        q = f"UPDATE paper_trades SET {set_clause} WHERE id = ?"
        values.append(trade_id)
        if user_id:
            q += " AND user_id = ?"
            values.append(user_id)
        self.conn.execute(q, values)
        self.conn.commit()
        return True

    def get_trade_stats(self, user_id, days=None):
        """Get paper trade stats for a user."""
        base_q = "SELECT * FROM paper_trades WHERE user_id = ? AND status = 'EXITED'"
        params = [user_id]
        if days:
            base_q += " AND exited_at >= datetime('now', ?)"
            params.append(f"-{days} days")

        trades = [dict(r) for r in self.conn.execute(base_q, params).fetchall()]
        if not trades:
            return {"total": 0, "wins": 0, "losses": 0, "win_rate": 0,
                    "total_pnl": 0, "avg_pnl": 0, "best": 0, "worst": 0}

        wins = [t for t in trades if (t.get("pnl") or 0) > 0]
        losses = [t for t in trades if (t.get("pnl") or 0) <= 0]
        pnls = [t.get("pnl") or 0 for t in trades]

        return {
            "total": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else 0,
            "total_pnl": round(sum(pnls), 2),
            "avg_pnl": round(sum(pnls) / len(pnls), 2) if pnls else 0,
            "best": round(max(pnls), 2) if pnls else 0,
            "worst": round(min(pnls), 2) if pnls else 0,
            "manual_count": len([t for t in trades if t["trade_type"] == "manual"]),
            "auto_count": len([t for t in trades if t["trade_type"] == "auto"]),
        }

    def count_user_trades_today(self, user_id, trade_type=None):
        """Count trades created today for rate limiting."""
        q = "SELECT COUNT(*) FROM paper_trades WHERE user_id = ? AND date(created_at) = date('now')"
        params = [user_id]
        if trade_type:
            q += " AND trade_type = ?"
            params.append(trade_type)
        return self.conn.execute(q, params).fetchone()[0]

    # ============================================================
    # AUTO SIGNALS
    # ============================================================

    def log_auto_signal(self, user_id, **kwargs):
        """Log an auto-trade signal evaluation."""
        fields = {
            "user_id": user_id,
            "nifty_direction": kwargs.get("nifty_direction"),
            "nifty_score": kwargs.get("nifty_score"),
            "sector": kwargs.get("sector"),
            "sector_momentum": kwargs.get("sector_momentum"),
            "symbol": kwargs.get("symbol"),
            "analysis_summary": kwargs.get("analysis_summary"),
            "analysis_json": json.dumps(kwargs.get("analysis_data", {})),
            "confidence": kwargs.get("confidence"),
            "action_taken": kwargs.get("action_taken", "logged"),
            "trade_id": kwargs.get("trade_id"),
        }
        cols = ", ".join(fields.keys())
        placeholders = ", ".join("?" for _ in fields)
        cur = self.conn.execute(
            f"INSERT INTO auto_signals ({cols}) VALUES ({placeholders})",
            list(fields.values()),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_auto_signals(self, user_id, limit=20):
        """Get recent auto signals for a user."""
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM auto_signals WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()]

    # ============================================================
    # LOGIN AUDIT
    # ============================================================

    def log_audit_event(self, event_type, ip=None, username=None, user_id=None, details=None):
        """Log a security/audit event."""
        self.conn.execute(
            """INSERT INTO login_audit (event_type, ip_address, username, user_id, details)
               VALUES (?, ?, ?, ?, ?)""",
            (event_type, ip, username, user_id, details),
        )
        self.conn.commit()

    def get_recent_audit_events(self, limit=50, event_type=None, username=None):
        """Get recent audit events (admin use)."""
        q = "SELECT * FROM login_audit WHERE 1=1"
        params = []
        if event_type:
            q += " AND event_type = ?"
            params.append(event_type)
        if username:
            q += " AND username = ?"
            params.append(username)
        q += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self.conn.execute(q, params).fetchall()]

    def count_failed_logins(self, ip=None, username=None, minutes=15):
        """Count recent failed login attempts (for rate limit decisions)."""
        q = "SELECT COUNT(*) FROM login_audit WHERE event_type = 'LOGIN_FAILED' AND created_at >= datetime('now', ?)"
        params = [f"-{minutes} minutes"]
        if ip:
            q += " AND ip_address = ?"
            params.append(ip)
        if username:
            q += " AND username = ?"
            params.append(username)
        return self.conn.execute(q, params).fetchone()[0]

    def cleanup_old_audit(self, days=90):
        """Clean up audit entries older than N days."""
        n = self.conn.execute(
            "DELETE FROM login_audit WHERE created_at < datetime('now', ?)",
            (f"-{days} days",),
        ).rowcount
        self.conn.commit()
        return n

    # ============================================================
    # ADMIN
    # ============================================================

    def get_platform_stats(self):
        """Get platform-wide stats for admin dashboard."""
        users = self.conn.execute("SELECT COUNT(*) FROM users WHERE is_active = 1").fetchone()[0]
        trades_today = self.conn.execute(
            "SELECT COUNT(*) FROM paper_trades WHERE date(created_at) = date('now')"
        ).fetchone()[0]
        active_sessions = self.conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE datetime(expires_at) > datetime('now')"
        ).fetchone()[0]
        total_trades = self.conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
        auto_trades = self.conn.execute(
            "SELECT COUNT(*) FROM paper_trades WHERE trade_type = 'auto'"
        ).fetchone()[0]
        locked_accounts = self.conn.execute(
            "SELECT COUNT(*) FROM users WHERE datetime(locked_until) > datetime('now')"
        ).fetchone()[0]
        failed_logins_24h = self.conn.execute(
            "SELECT COUNT(*) FROM login_audit WHERE event_type = 'LOGIN_FAILED' AND created_at >= datetime('now', '-1 day')"
        ).fetchone()[0]
        pro_users = self.conn.execute("SELECT COUNT(*) FROM users WHERE plan = 'pro' AND subscription_status = 'active'").fetchone()[0]
        return {
            "total_users": users,
            "active_sessions": active_sessions,
            "trades_today": trades_today,
            "total_trades": total_trades,
            "auto_trades": auto_trades,
            "locked_accounts": locked_accounts,
            "failed_logins_24h": failed_logins_24h,
            "pro_users": pro_users,
        }

    def _migrate_v2_to_v3(self):
        """v2 → v3: add user_watchlist table."""
        log.info("Running migration v2 → v3...")
        try:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS user_watchlist (
                    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    symbol     TEXT NOT NULL COLLATE NOCASE,
                    added_at   TEXT NOT NULL DEFAULT (datetime('now')),
                    PRIMARY KEY (user_id, symbol)
                )
            """)
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_watchlist_user ON user_watchlist(user_id)"
            )
            # Also add column_config to user_settings if missing
            cols = {r[1] for r in self.conn.execute("PRAGMA table_info(user_settings)").fetchall()}
            if "column_config" not in cols:
                self.conn.execute("ALTER TABLE user_settings ADD COLUMN column_config TEXT")
                log.info("  Added column_config to user_settings")
                
            if "theme" not in cols:
                self.conn.execute("ALTER TABLE user_settings ADD COLUMN theme TEXT DEFAULT 'bloomberg-pro'")
                log.info("  Added theme to user_settings")
            self.conn.commit()
            log.info("Migration v2 → v3 complete")
        except Exception as e:
            log.warning(f"Migration v2→v3 error (may already be applied): {e}")

    def _migrate_v3_to_v4(self):
        """v3 → v4: Add option_type and spot_at_entry columns to paper_trades."""
        log.info("Running migration v3 → v4...")
        try:
            cols = {r[1] for r in self.conn.execute("PRAGMA table_info(paper_trades)").fetchall()}
            if "option_type" not in cols:
                self.conn.execute("ALTER TABLE paper_trades ADD COLUMN option_type TEXT")
                log.info("  Added option_type column to paper_trades")
            if "spot_at_entry" not in cols:
                self.conn.execute("ALTER TABLE paper_trades ADD COLUMN spot_at_entry REAL")
                log.info("  Added spot_at_entry column to paper_trades")
            self.conn.commit()
            log.info("Migration v3 → v4 complete")
        except Exception as e:
            log.warning(f"Migration v3→v4 error: {e}")

    def _migrate_v4_to_v5(self):
        """v4 → v5: Add email verification + plan fields to users."""
        log.info("Running migration v4 → v5...")
        try:
            cols = {r[1] for r in self.conn.execute("PRAGMA table_info(users)").fetchall()}
            additions = [
                ("email_verified", "INTEGER NOT NULL DEFAULT 0"),
                ("email_otp", "TEXT"),
                ("email_otp_expires_at", "TEXT"),
                ("plan", "TEXT NOT NULL DEFAULT 'free'"),
            ]
            for col, typedef in additions:
                if col not in cols:
                    self.conn.execute(f"ALTER TABLE users ADD COLUMN {col} {typedef}")
                    log.info(f"  Added {col} to users")
            self.conn.commit()
            log.info("Migration v4 → v5 complete")
        except Exception as e:
            log.warning(f"Migration v4→v5 error: {e}")

    def _migrate_v5_to_v6(self):
        """v5 → v6: Add Razorpay/subscription fields to users."""
        log.info("Running migration v5 → v6...")
        try:
            cols = {r[1] for r in self.conn.execute("PRAGMA table_info(users)").fetchall()}
            additions = [
                ("razorpay_customer_id", "TEXT"),
                ("razorpay_subscription_id", "TEXT"),
                ("subscription_status", "TEXT NOT NULL DEFAULT 'inactive'"),
                ("subscription_expires_at", "TEXT"),
            ]
            for col, typedef in additions:
                if col not in cols:
                    self.conn.execute(f"ALTER TABLE users ADD COLUMN {col} {typedef}")
                    log.info(f"  Added {col} to users")
            self.conn.commit()
            log.info("Migration v5 → v6 complete")
        except Exception as e:
            log.warning(f"Migration v5→v6 error: {e}")

    # ============================================================
    # WATCHLIST
    # ============================================================

    def get_watchlist(self, user_id):
        """Return list of symbols in user's watchlist, ordered by added_at desc."""
        rows = self.conn.execute(
            "SELECT symbol, added_at FROM user_watchlist WHERE user_id = ? ORDER BY added_at DESC",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def set_watchlist(self, user_id, symbols):
        """Replace the user's entire watchlist with the given list of symbols."""
        symbols = [s.upper().strip() for s in symbols if s and s.strip()][:100]  # max 100
        with self.conn:
            self.conn.execute("DELETE FROM user_watchlist WHERE user_id = ?", (user_id,))
            if symbols:
                self.conn.executemany(
                    "INSERT OR IGNORE INTO user_watchlist (user_id, symbol) VALUES (?, ?)",
                    [(user_id, s) for s in symbols],
                )

    def add_to_watchlist(self, user_id, symbol):
        """Add a symbol to the user's watchlist. Idempotent."""
        symbol = symbol.upper().strip()
        # Enforce max 100 symbols
        count = self.conn.execute(
            "SELECT COUNT(*) FROM user_watchlist WHERE user_id = ?", (user_id,)
        ).fetchone()[0]
        if count >= 100:
            raise ValueError("Watchlist limit reached (100 symbols)")
        self.conn.execute(
            "INSERT OR IGNORE INTO user_watchlist (user_id, symbol) VALUES (?, ?)",
            (user_id, symbol),
        )
        self.conn.commit()

    def remove_from_watchlist(self, user_id, symbol):
        """Remove a symbol from the user's watchlist."""
        self.conn.execute(
            "DELETE FROM user_watchlist WHERE user_id = ? AND symbol = ?",
            (user_id, symbol.upper().strip()),
        )
        self.conn.commit()

    def migrate_from_json(self, config_path="auth_config.json"):
        """Migrate existing users from auth_config.json into the database."""
        if not os.path.exists(config_path):
            return 0
        try:
            with open(config_path) as f:
                cfg = json.load(f)
        except (json.JSONDecodeError, IOError):
            return 0

        migrated = 0
        for u in cfg.get("users", []):
            if not isinstance(u, dict) or "username" not in u:
                continue
            # Check if already exists
            existing = self.get_user_by_username(u["username"])
            if existing:
                continue
            try:
                # Insert with legacy hash/salt — marked as sha256 for auto-upgrade on next login
                cur = self.conn.execute(
                    """INSERT INTO users (username, password_hash, salt, hash_algorithm, role, display_name)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (u["username"], u["hash"], u["salt"], "sha256",
                     u.get("role", "admin"), u["username"]),
                )
                uid = cur.lastrowid
                self.conn.execute("INSERT INTO user_settings (user_id) VALUES (?)", (uid,))
                migrated += 1
                log.info(f"Migrated user from JSON: {u['username']} (role={u.get('role', 'admin')}, hash=SHA256→will auto-upgrade)")
            except sqlite3.IntegrityError:
                continue

        self.conn.commit()
        if migrated:
            log.info(f"Migrated {migrated} user(s) from {config_path}")
        return migrated
