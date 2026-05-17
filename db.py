#!/usr/bin/env python3
"""
db.py — SQLite database layer for Quantra Terminal.

Handles: users, sessions, user settings, paper trades (manual + auto),
auto-trade signals. Provides async-safe wrappers (SQLite ops run in executor).

Usage:
    from db import DB
    db = DB("quantra.db")
    db.init()  # creates tables if needed
    user_id = db.create_user("amit", "amit@example.com", "hashedpw", "salt", role="admin")
    user = db.get_user_by_username("amit")
"""

import hashlib
import json
import os
import secrets
import sqlite3
import time
import logging
from datetime import datetime, timedelta
from typing import Optional

log = logging.getLogger("quantra.db")

SCHEMA_VERSION = 1

CREATE_TABLES = """
-- Users
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL COLLATE NOCASE,
    email TEXT UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    display_name TEXT,
    phone TEXT,
    avatar_url TEXT,
    bio TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_login TEXT,
    is_active INTEGER NOT NULL DEFAULT 1
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
    notification_email INTEGER NOT NULL DEFAULT 0,
    notification_telegram INTEGER NOT NULL DEFAULT 0,
    telegram_chat_id TEXT,
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
"""


class DB:
    """SQLite database wrapper for Quantra Terminal."""

    def __init__(self, db_path="quantra.db"):
        self.db_path = db_path
        self._conn = None

    @property
    def conn(self):
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")
        return self._conn

    def init(self):
        """Initialize database — create tables if they don't exist."""
        self.conn.executescript(CREATE_TABLES)
        # Set schema version
        self.conn.execute(
            "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
            ("schema_version", str(SCHEMA_VERSION)),
        )
        self.conn.commit()
        log.info(f"Database initialized: {self.db_path} (schema v{SCHEMA_VERSION})")

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    # ============================================================
    # USERS
    # ============================================================

    @staticmethod
    def hash_password(password, salt=None):
        """Hash password with salt. Returns (hash, salt)."""
        if salt is None:
            salt = secrets.token_hex(16)
        hashed = hashlib.sha256((salt + password).encode()).hexdigest()
        return hashed, salt

    def create_user(self, username, email, password, role="user", display_name=None):
        """Create a new user. Returns user_id or raises on duplicate."""
        pw_hash, salt = self.hash_password(password)
        try:
            cur = self.conn.execute(
                """INSERT INTO users (username, email, password_hash, salt, role, display_name)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (username.strip(), email.strip().lower() if email else None,
                 pw_hash, salt, role, display_name or username),
            )
            user_id = cur.lastrowid
            # Create default settings
            self.conn.execute(
                "INSERT INTO user_settings (user_id) VALUES (?)", (user_id,)
            )
            self.conn.commit()
            log.info(f"User created: {username} (id={user_id}, role={role})")
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
        """Verify login credentials. Returns user dict or None."""
        row = self.conn.execute(
            "SELECT * FROM users WHERE username = ? AND is_active = 1",
            (username,),
        ).fetchone()
        if not row:
            # Try by email
            row = self.conn.execute(
                "SELECT * FROM users WHERE email = ? AND is_active = 1",
                (username.lower(),),
            ).fetchone()
        if not row:
            return None
        pw_hash, _ = self.hash_password(password, row["salt"])
        if pw_hash != row["password_hash"]:
            return None
        # Update last_login
        self.conn.execute(
            "UPDATE users SET last_login = datetime('now') WHERE id = ?",
            (row["id"],),
        )
        self.conn.commit()
        return dict(row)

    def get_user(self, user_id):
        """Get user by ID."""
        row = self.conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

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
        """Change user password. Verifies old password first."""
        row = self.conn.execute(
            "SELECT password_hash, salt FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if not row:
            return False, "User not found"
        old_hash, _ = self.hash_password(old_password, row["salt"])
        if old_hash != row["password_hash"]:
            return False, "Current password is incorrect"
        new_hash, new_salt = self.hash_password(new_password)
        self.conn.execute(
            "UPDATE users SET password_hash = ?, salt = ? WHERE id = ?",
            (new_hash, new_salt, user_id),
        )
        self.conn.commit()
        return True, "Password changed"

    def list_users(self, include_inactive=False):
        """List all users (admin use)."""
        q = "SELECT id, username, email, role, display_name, created_at, last_login, is_active FROM users"
        if not include_inactive:
            q += " WHERE is_active = 1"
        q += " ORDER BY created_at DESC"
        return [dict(r) for r in self.conn.execute(q).fetchall()]

    def count_users(self):
        """Count active users."""
        return self.conn.execute(
            "SELECT COUNT(*) FROM users WHERE is_active = 1"
        ).fetchone()[0]

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

    def update_user_settings(self, user_id, **fields):
        """Update user settings."""
        allowed = {
            "max_paper_trades_per_day", "default_lots", "default_capital",
            "auto_exit_enabled", "auto_trail_sl",
            "auto_trade_enabled", "auto_trade_max_positions", "auto_trade_max_capital",
            "preferred_sectors",
            "notification_email", "notification_telegram", "telegram_chat_id",
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
        """Create a new session. Returns session token."""
        token = secrets.token_hex(32)
        expires = (datetime.utcnow() + timedelta(seconds=max_age)).isoformat()
        self.conn.execute(
            """INSERT INTO sessions (token, user_id, expires_at, ip_address, user_agent)
               VALUES (?, ?, ?, ?, ?)""",
            (token, user_id, expires, ip, ua),
        )
        self.conn.commit()
        return token

    def validate_session(self, token):
        """Validate a session token. Returns (user_id, role, username) or None."""
        row = self.conn.execute(
            """SELECT s.user_id, s.expires_at, u.username, u.role, u.display_name, u.is_active
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

    def cleanup_expired_sessions(self):
        """Remove all expired sessions."""
        n = self.conn.execute(
            "DELETE FROM sessions WHERE expires_at < datetime('now')"
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

    def update_paper_trade(self, trade_id, user_id=None, **fields):
        """Update a paper trade (status, exit, PnL, etc.)."""
        allowed = {
            "status", "entry_premium", "exit_premium", "lots",
            "sl_premium", "sl_spot", "t1_premium", "t2_premium",
            "exit_reason", "pnl", "pnl_pct", "costs_estimated", "net_pnl",
            "entered_at", "exited_at",
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
    # ADMIN
    # ============================================================

    def get_platform_stats(self):
        """Get platform-wide stats for admin dashboard."""
        users = self.conn.execute("SELECT COUNT(*) FROM users WHERE is_active = 1").fetchone()[0]
        trades_today = self.conn.execute(
            "SELECT COUNT(*) FROM paper_trades WHERE date(created_at) = date('now')"
        ).fetchone()[0]
        active_sessions = self.conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE expires_at > datetime('now')"
        ).fetchone()[0]
        total_trades = self.conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
        auto_trades = self.conn.execute(
            "SELECT COUNT(*) FROM paper_trades WHERE trade_type = 'auto'"
        ).fetchone()[0]
        return {
            "total_users": users,
            "active_sessions": active_sessions,
            "trades_today": trades_today,
            "total_trades": total_trades,
            "auto_trades": auto_trades,
        }

    # ============================================================
    # MIGRATION FROM auth_config.json
    # ============================================================

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
                # Insert directly with existing hash/salt (don't re-hash)
                cur = self.conn.execute(
                    """INSERT INTO users (username, password_hash, salt, role, display_name)
                       VALUES (?, ?, ?, ?, ?)""",
                    (u["username"], u["hash"], u["salt"],
                     u.get("role", "admin"), u["username"]),
                )
                uid = cur.lastrowid
                self.conn.execute("INSERT INTO user_settings (user_id) VALUES (?)", (uid,))
                migrated += 1
                log.info(f"Migrated user from JSON: {u['username']} (role={u.get('role', 'admin')})")
            except sqlite3.IntegrityError:
                continue

        self.conn.commit()
        if migrated:
            log.info(f"Migrated {migrated} user(s) from {config_path}")
        return migrated
