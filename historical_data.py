"""
historical_data.py — Pulls daily OHLC for FNO stocks from Upstox v2
historical-candle endpoint and caches the results in SQLite.

Used by the OI Thesis report (and future Edge Score) to overlay
price-action context onto the data-side signals.

CLI:
    python3 historical_data.py fetch RELIANCE,TCS,INFY     # specific symbols
    python3 historical_data.py fetch_today_flags           # symbols flagged today
    python3 historical_data.py show RELIANCE 30            # show recent rows

Token is resolved the same way ws_server does it:
    UPSTOX_ACCESS_TOKEN env var > config.env file.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import data_recorder

log = logging.getLogger("historical_data")

UPSTOX_BASE = "https://api.upstox.com"
DEFAULT_LOOKBACK_DAYS = 90
RATE_LIMIT_DELAY_SEC = 0.4  # between calls — Upstox v2 ≈ 50 req/sec, but be polite

IST = timezone(timedelta(hours=5, minutes=30))

# ---------------------------------------------------------------------------
# Token resolution (mirrors ws_server.resolve_token)
# ---------------------------------------------------------------------------
def _resolve_token() -> Optional[str]:
    env = os.environ.get("UPSTOX_ACCESS_TOKEN")
    if env:
        return env
    # config.env in repo root
    cfg = Path(__file__).parent / "config.env"
    if not cfg.exists():
        return None
    try:
        with open(cfg, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == "UPSTOX_ACCESS_TOKEN":
                    v = v.strip().strip('"').strip("'")
                    return v if v else None
    except Exception as exc:
        log.warning("config.env read failed: %s", exc)
    return None


# ---------------------------------------------------------------------------
# Symbol → instrument_key resolution
# ---------------------------------------------------------------------------
def _ikeys_for(symbols: Sequence[str], db_path: Optional[Path] = None) -> Dict[str, str]:
    """
    Look up Upstox instrument_keys for the given symbols. We get them straight
    from the chain_snapshot (or stock_daily) tables — they were stored when
    ws_server first booted, so the cache is already there.
    """
    out: Dict[str, str] = {}
    # The chain_snapshot table doesn't carry ikey directly. Use the in-memory
    # cache via /api/state, which DOES include it.
    try:
        with urllib.request.urlopen("http://127.0.0.1:8081/api/state", timeout=5) as r:
            state = json.loads(r.read().decode())["stocks"]
        for sym in symbols:
            d = state.get(sym)
            if d and d.get("ikey"):
                out[sym] = d["ikey"]
    except Exception as exc:
        log.warning("ikey lookup via /api/state failed: %s", exc)
    return out


# ---------------------------------------------------------------------------
# SQLite cache
# ---------------------------------------------------------------------------
_OHLC_SCHEMA = """
CREATE TABLE IF NOT EXISTS stock_daily_ohlc (
    symbol        TEXT NOT NULL,
    trading_date  TEXT NOT NULL,
    open          REAL,
    high          REAL,
    low           REAL,
    close         REAL,
    volume        INTEGER,
    oi            INTEGER,
    fetched_at    TEXT NOT NULL,
    PRIMARY KEY (symbol, trading_date)
);
CREATE INDEX IF NOT EXISTS idx_ohlc_sym ON stock_daily_ohlc(symbol);
"""


def init_ohlc_table(db_path: Optional[Path] = None) -> None:
    with closing(data_recorder._connect(db_path)) as conn:
        conn.executescript(_OHLC_SCHEMA)


# ---------------------------------------------------------------------------
# Upstox fetch
# ---------------------------------------------------------------------------
def fetch_daily_candles(
    instrument_key: str,
    days: int = DEFAULT_LOOKBACK_DAYS,
    token: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Fetch daily candles for a single instrument from Upstox v2.
    Returns list of dicts: [{date, open, high, low, close, volume, oi}, ...]
    sorted oldest -> newest.
    """
    token = token or _resolve_token()
    if not token:
        raise RuntimeError("UPSTOX_ACCESS_TOKEN not configured")

    today = datetime.now(IST).date()
    from_date = (today - timedelta(days=days)).isoformat()
    to_date = today.isoformat()
    ikey_enc = urllib.parse.quote(instrument_key, safe="")
    url = f"{UPSTOX_BASE}/v2/historical-candle/{ikey_enc}/day/{to_date}/{from_date}"

    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        # Cloudflare's bot mitigation blocks default Python-urllib UA with
        # error 1010. Send a generic browser-ish UA so the request looks
        # legitimate to the edge layer.
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Safari/605.1.15",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="ignore")[:300]
        raise RuntimeError(f"Upstox HTTP {e.code} for {instrument_key}: {body}")

    if data.get("status") != "success":
        raise RuntimeError(f"Upstox status not success for {instrument_key}: {str(data)[:200]}")

    candles = (data.get("data") or {}).get("candles") or []
    # Format: [timestamp, open, high, low, close, volume, oi]
    out = []
    for c in candles:
        if len(c) < 6:
            continue
        ts = c[0]
        # ts is ISO with timezone, e.g. '2026-05-19T00:00:00+05:30'
        date_part = ts[:10] if isinstance(ts, str) else None
        if not date_part:
            continue
        out.append({
            "date":   date_part,
            "open":   c[1],
            "high":   c[2],
            "low":    c[3],
            "close":  c[4],
            "volume": int(c[5]) if c[5] is not None else 0,
            "oi":     int(c[6]) if len(c) > 6 and c[6] is not None else None,
        })
    out.sort(key=lambda r: r["date"])
    return out


def cache_candles(symbol: str, candles: List[Dict[str, Any]], db_path: Optional[Path] = None) -> int:
    """Upsert candles into stock_daily_ohlc. Returns rows written."""
    if not candles:
        return 0
    init_ohlc_table(db_path)
    fetched_at = datetime.now(IST).isoformat(timespec="seconds")
    rows = [
        (symbol, c["date"], c["open"], c["high"], c["low"], c["close"], c["volume"], c.get("oi"), fetched_at)
        for c in candles
    ]
    with closing(data_recorder._connect(db_path)) as conn:
        conn.execute("BEGIN;")
        conn.executemany("""
            INSERT OR REPLACE INTO stock_daily_ohlc
                (symbol, trading_date, open, high, low, close, volume, oi, fetched_at)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, rows)
        conn.execute("COMMIT;")
    return len(rows)


def load_cached(symbol: str, days: int = 90, db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Read cached OHLC. Returns rows oldest -> newest."""
    init_ohlc_table(db_path)
    cutoff = (datetime.now(IST).date() - timedelta(days=days)).isoformat()
    with closing(data_recorder._connect(db_path)) as conn:
        rows = conn.execute("""
            SELECT trading_date, open, high, low, close, volume, oi
            FROM stock_daily_ohlc
            WHERE symbol = ? AND trading_date >= ?
            ORDER BY trading_date
        """, (symbol, cutoff)).fetchall()
    return [{
        "date": r["trading_date"], "open": r["open"], "high": r["high"],
        "low": r["low"], "close": r["close"], "volume": r["volume"], "oi": r["oi"],
    } for r in rows]


def fetch_and_cache_universe(
    symbols: Sequence[str],
    days: int = DEFAULT_LOOKBACK_DAYS,
    token: Optional[str] = None,
    skip_if_cached_today: bool = True,
) -> Dict[str, Dict[str, Any]]:
    """
    Fetch + cache OHLC for every given symbol. Skips fetch if today's
    candle already exists in cache (so re-runs are cheap).

    Returns: {symbol: {"rows": int, "skipped": bool, "error": str|None}}
    """
    init_ohlc_table()
    ikeys = _ikeys_for(symbols)
    today = datetime.now(IST).date().isoformat()
    out: Dict[str, Dict[str, Any]] = {}
    token = token or _resolve_token()

    for sym in symbols:
        ikey = ikeys.get(sym)
        if not ikey:
            out[sym] = {"rows": 0, "skipped": False, "error": "no instrument_key from /api/state"}
            continue

        if skip_if_cached_today:
            cached = load_cached(sym, days=2)
            if cached and cached[-1]["date"] == today:
                out[sym] = {"rows": len(cached), "skipped": True, "error": None}
                continue

        try:
            candles = fetch_daily_candles(ikey, days=days, token=token)
            written = cache_candles(sym, candles)
            out[sym] = {"rows": written, "skipped": False, "error": None}
            log.info("fetched %s: %d candles cached", sym, written)
        except Exception as exc:
            out[sym] = {"rows": 0, "skipped": False, "error": str(exc)}
            log.warning("fetch failed %s: %s", sym, exc)
        time.sleep(RATE_LIMIT_DELAY_SEC)

    return out


def todays_flag_symbols(db_path: Optional[Path] = None) -> List[str]:
    """Return the symbols flagged in oi_thesis_flag for the latest flag_date."""
    with closing(data_recorder._connect(db_path)) as conn:
        row = conn.execute("SELECT MAX(flag_date) FROM oi_thesis_flag").fetchone()
        if not row or not row[0]:
            return []
        latest = row[0]
        rows = conn.execute(
            "SELECT DISTINCT symbol FROM oi_thesis_flag WHERE flag_date = ?",
            (latest,),
        ).fetchall()
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"

    if cmd == "fetch":
        if len(sys.argv) < 3:
            print("usage: fetch SYM1,SYM2,...")
            sys.exit(2)
        syms = [s.strip().upper() for s in sys.argv[2].split(",") if s.strip()]
        result = fetch_and_cache_universe(syms)
        for s, r in result.items():
            tag = "skip" if r["skipped"] else ("err " if r["error"] else "ok  ")
            print(f"  {tag} {s:<14} rows={r['rows']:>4}{'  '+r['error'] if r['error'] else ''}")
    elif cmd == "fetch_today_flags":
        syms = todays_flag_symbols()
        if not syms:
            print("No flags found in oi_thesis_flag yet.")
            sys.exit(0)
        print(f"Fetching {len(syms)} flagged symbols...")
        result = fetch_and_cache_universe(syms)
        ok = sum(1 for r in result.values() if not r["error"])
        skipped = sum(1 for r in result.values() if r["skipped"])
        err = sum(1 for r in result.values() if r["error"])
        print(f"OK: ok={ok}  skipped={skipped}  err={err}")
        for s, r in result.items():
            if r["error"]:
                print(f"  ERROR {s}: {r['error']}")
    elif cmd == "show":
        sym = sys.argv[2].upper() if len(sys.argv) > 2 else ""
        days = int(sys.argv[3]) if len(sys.argv) > 3 else 30
        rows = load_cached(sym, days=days)
        if not rows:
            print(f"No cached OHLC for {sym}.")
            sys.exit(0)
        print(f"{sym} — {len(rows)} cached daily candles")
        for r in rows[-30:]:
            print(f"  {r['date']}  O {r['open']:>9.2f}  H {r['high']:>9.2f}  L {r['low']:>9.2f}  C {r['close']:>9.2f}  V {r['volume']:>12,}")
    else:
        print(__doc__)
        sys.exit(1)
