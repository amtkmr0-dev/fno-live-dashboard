"""
oi_thesis_tracker.py
====================
Systematic capture + verification of the OI-divergence thesis on Indian F&O.

Rule under test
---------------
  BULLISH flag: at end-of-day (last chain refresh of the day),
                CE total OI change < 0  AND  PE total OI change > 0
                AND total_oi >= LIQUIDITY_FLOOR.
  BEARISH flag: CE total OI change > 0  AND  PE total OI change < 0.

Win definition (locked-in tonight, configurable here)
-----------------------------------------------------
  Day-T flag is checked on day T+1 (next trading day):
    BULLISH win if (peak high T+1 - close T) / close T >= +PEAK_PCT_WIN
                   AND close T+1 >= close T  (didn't close below entry)
    BEARISH win if (close T - low T+1)  / close T >= +PEAK_PCT_WIN
                   AND close T+1 <= close T

  We also stratify a stricter "1% peak" metric for headline reporting.

Workflow
--------
  capture <date>   Read EOD snapshot for <date>, write top-N flags per side.
  verify  <date>   For all flags whose flag_date == <date>, find next trading
                   day's OHLC and compute outcomes.
  report  [n_days] Print rolling stats for last n_days (default 30).
  daily            Convenience: verify(yesterday's flags) -> capture(today).

CLI
---
  python3 oi_thesis_tracker.py daily          # the canonical cron entry
  python3 oi_thesis_tracker.py capture today  # manual capture
  python3 oi_thesis_tracker.py verify 2026-05-19
  python3 oi_thesis_tracker.py report 30

Schema notes
------------
Two tables in the same DB as data_recorder (data/quantra_history.db) so we
can JOIN against the chain_snapshot history when computing outcomes:
  oi_thesis_flag    — every flag we ever raised
  oi_thesis_outcome — resolution per flag, 1 row per flag once T+1 data exists

Both keyed on (flag_date, symbol, side) so re-runs are idempotent.
"""
from __future__ import annotations

import logging
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# Co-locate with the recorder DB
import data_recorder

log = logging.getLogger("oi_thesis")

# ---- Tunable parameters -----------------------------------------------------
LIQUIDITY_FLOOR_OI = 100_000   # min total_oi to be considered tradable
TOP_N_PER_SIDE     = 10        # number of flags per side per day (headline)
PEAK_PCT_WIN       = 0.75      # % move from T close to T+1 peak required for win
PEAK_PCT_STRICT    = 1.0       # stricter target for stratified reporting
# ---------------------------------------------------------------------------

IST = timezone(timedelta(hours=5, minutes=30))

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS oi_thesis_flag (
    flag_date     TEXT NOT NULL,
    rule_id       TEXT NOT NULL DEFAULT 'oi_div_v1',  -- which rule fired this flag
    symbol        TEXT NOT NULL,
    side          TEXT NOT NULL,           -- 'bull' | 'bear'
    rank          INTEGER NOT NULL,        -- 1..N within side that day
    spot_at_flag  REAL,
    ce_oi_chg     INTEGER,
    pe_oi_chg     INTEGER,
    net_thesis    INTEGER,                 -- |PE-CE| for bull, |CE-PE| for bear
    pcr_at_flag   REAL,
    surge_at_flag REAL,
    total_oi      INTEGER,
    sector        TEXT,
    flag_snap_ts  TEXT,                    -- the snap_ts row used as EOD source
    captured_at   TEXT NOT NULL,           -- ISO ts when capture() ran
    PRIMARY KEY (flag_date, rule_id, symbol, side)
);
CREATE INDEX IF NOT EXISTS idx_thesis_flag_date ON oi_thesis_flag(flag_date);
CREATE INDEX IF NOT EXISTS idx_thesis_flag_rule ON oi_thesis_flag(rule_id, flag_date);

CREATE TABLE IF NOT EXISTS oi_thesis_outcome (
    flag_date          TEXT NOT NULL,
    rule_id            TEXT NOT NULL DEFAULT 'oi_div_v1',
    symbol             TEXT NOT NULL,
    side               TEXT NOT NULL,
    resolution_date    TEXT NOT NULL,
    spot_at_flag       REAL,
    spot_close_next    REAL,
    spot_high_next     REAL,
    spot_low_next      REAL,
    chg_pct_close      REAL,        -- (close_next - flag) / flag * 100, signed
    chg_pct_peak       REAL,        -- bull: high_next; bear: low_next, signed in dir of thesis
    win_peak_loose     INTEGER,     -- peak >= PEAK_PCT_WIN AND close didn't cross
    win_peak_strict    INTEGER,     -- same but PEAK_PCT_STRICT
    win_close_only     INTEGER,     -- close direction agrees, ignore peak
    resolved_at        TEXT NOT NULL,
    PRIMARY KEY (flag_date, rule_id, symbol, side)
);
CREATE INDEX IF NOT EXISTS idx_thesis_outcome_date ON oi_thesis_outcome(flag_date);
CREATE INDEX IF NOT EXISTS idx_thesis_outcome_rule ON oi_thesis_outcome(rule_id, flag_date);
"""

# Default rule ID we capture under. New rules added later get their own IDs.
DEFAULT_RULE_ID = "oi_div_v1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Reuse data_recorder's connection helper."""
    return data_recorder._connect(db_path)


def init_db(db_path: Optional[Path] = None) -> None:
    """Create thesis tracking tables if missing."""
    with closing(_connect(db_path)) as conn:
        conn.executescript(_SCHEMA_SQL)


def _now_ist() -> datetime:
    return datetime.now(tz=IST)


def _today_str() -> str:
    return _now_ist().date().isoformat()


def _last_snap_for_date(conn: sqlite3.Connection, trading_date: str) -> Optional[str]:
    """Return the latest snap_ts on `trading_date`, or None if no snapshots."""
    row = conn.execute(
        "SELECT MAX(snap_ts) FROM chain_snapshot WHERE trading_date = ?",
        (trading_date,),
    ).fetchone()
    return row[0] if row and row[0] else None


def _next_trading_date_with_data(conn: sqlite3.Connection, after: str) -> Optional[str]:
    """Find the earliest trading_date in chain_snapshot strictly after `after`
    that represents real market activity — not just idle pre/post-market refreshes.

    A "real" trading day requires at least one snapshot whose hour ∈ [9, 16] IST.
    Dates with only off-hours refreshes (00:00-09:00 or 16:00-24:00) are skipped.
    """
    rows = conn.execute(
        """SELECT trading_date, snap_ts FROM chain_snapshot
            WHERE trading_date > ? ORDER BY trading_date, snap_ts""",
        (after,),
    ).fetchall()
    if not rows:
        return None
    by_date = {}
    for r in rows:
        by_date.setdefault(r["trading_date"], []).append(r["snap_ts"])
    for date in sorted(by_date.keys()):
        for ts in by_date[date]:
            try:
                hr = int(ts[11:13])
                if 9 <= hr <= 16:
                    return date
            except Exception:
                continue
    return None


def _ohlc_for(conn: sqlite3.Connection, trading_date: str, symbol: str):
    """Return (open, high, low, close) on `trading_date` for `symbol` from
    chain_snapshot rows. We pick the day's high/low across all snaps and
    use first snap's spot_ltp as open, last snap's as close."""
    rows = conn.execute(
        """SELECT snap_ts, spot_ltp, spot_high, spot_low
             FROM chain_snapshot
            WHERE trading_date = ? AND symbol = ?
            ORDER BY snap_ts""",
        (trading_date, symbol),
    ).fetchall()
    if not rows:
        return None
    spot_open  = rows[0]["spot_ltp"]
    spot_close = rows[-1]["spot_ltp"]
    # day high = max across (snap-recorded high) and ltp samples; same for low
    day_high = max((r["spot_high"]  or r["spot_ltp"] or 0) for r in rows)
    day_low  = min((r["spot_low"]   or r["spot_ltp"] or float("inf")) for r in rows)
    if day_low == float("inf"):
        day_low = spot_open
    return (spot_open, day_high, day_low, spot_close)


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------
def capture(flag_date: Optional[str] = None, db_path: Optional[Path] = None,
            rule_id: str = DEFAULT_RULE_ID) -> dict:
    """Read the EOD snapshot for `flag_date` (default: today) and write top-N
    flags per side to oi_thesis_flag.

    Returns counts. Idempotent — re-running for same (flag_date, rule_id) REPLACES rows.
    """
    flag_date = flag_date or _today_str()
    init_db(db_path)

    with closing(_connect(db_path)) as conn:
        snap_ts = _last_snap_for_date(conn, flag_date)
        if not snap_ts:
            log.warning("capture: no chain snapshots for %s", flag_date)
            return {"flag_date": flag_date, "bull": 0, "bear": 0}

        # Pull all stocks from the EOD snapshot of that date
        rows = conn.execute("""
            SELECT cs.symbol, cs.spot_ltp, cs.ce_oi_chg, cs.pe_oi_chg,
                   cs.pcr, cs.vol_surge, cs.total_oi
              FROM chain_snapshot cs
             WHERE cs.snap_ts = ?
        """, (snap_ts,)).fetchall()

        bulls = []
        bears = []
        for r in rows:
            ce = r["ce_oi_chg"] or 0
            pe = r["pe_oi_chg"] or 0
            toi = r["total_oi"] or 0
            if toi < LIQUIDITY_FLOOR_OI:
                continue
            if ce < 0 and pe > 0:
                bulls.append((r, pe - ce))      # higher = stronger bull
            elif ce > 0 and pe < 0:
                bears.append((r, ce - pe))      # higher = stronger bear

        bulls.sort(key=lambda x: x[1], reverse=True)
        bears.sort(key=lambda x: x[1], reverse=True)

        # Pull sector from the most recent stock_daily or chain_snapshot — best effort.
        # If we don't have it, leave NULL.
        captured_at = _now_ist().isoformat(timespec="seconds")

        def to_row(r, rank, side, net_thesis):
            return (
                flag_date, rule_id, r["symbol"], side, rank,
                r["spot_ltp"], int(r["ce_oi_chg"] or 0), int(r["pe_oi_chg"] or 0),
                int(net_thesis), r["pcr"], r["vol_surge"],
                int(r["total_oi"] or 0), None, snap_ts, captured_at,
            )

        bull_rows = [to_row(r, i + 1, "bull", n) for i, (r, n) in enumerate(bulls[:TOP_N_PER_SIDE])]
        bear_rows = [to_row(r, i + 1, "bear", n) for i, (r, n) in enumerate(bears[:TOP_N_PER_SIDE])]

        if bull_rows or bear_rows:
            conn.execute("BEGIN;")
            # Wipe and re-insert this date's flags so re-runs are clean
            conn.execute(
                "DELETE FROM oi_thesis_flag WHERE flag_date = ? AND rule_id = ?",
                (flag_date, rule_id),
            )
            conn.executemany("""
                INSERT INTO oi_thesis_flag
                  (flag_date, rule_id, symbol, side, rank,
                   spot_at_flag, ce_oi_chg, pe_oi_chg, net_thesis,
                   pcr_at_flag, surge_at_flag, total_oi, sector,
                   flag_snap_ts, captured_at)
                VALUES (?,?,?,?,?, ?,?,?,?, ?,?,?,?, ?,?)
            """, bull_rows + bear_rows)
            conn.execute("COMMIT;")

        log.info("capture: %s [%s] — wrote %d bull, %d bear flags",
                 flag_date, rule_id, len(bull_rows), len(bear_rows))
        return {"flag_date": flag_date, "rule_id": rule_id,
                "bull": len(bull_rows), "bear": len(bear_rows)}


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------
def verify(flag_date: str, db_path: Optional[Path] = None,
           rule_id: str = DEFAULT_RULE_ID) -> dict:
    """For every flag with flag_date=<flag_date> AND rule_id, look up the
    next trading day's OHLC and compute outcome. Idempotent.
    """
    init_db(db_path)
    with closing(_connect(db_path)) as conn:
        next_date = _next_trading_date_with_data(conn, flag_date)
        if not next_date:
            log.info("verify: no T+1 data yet for flag_date=%s", flag_date)
            return {"flag_date": flag_date, "rule_id": rule_id, "resolved": 0, "next_date": None}

        flags = conn.execute(
            "SELECT * FROM oi_thesis_flag WHERE flag_date = ? AND rule_id = ?",
            (flag_date, rule_id),
        ).fetchall()
        if not flags:
            log.info("verify: no flags for %s [%s]", flag_date, rule_id)
            return {"flag_date": flag_date, "rule_id": rule_id, "resolved": 0, "next_date": next_date}

        outcomes = []
        resolved_at = _now_ist().isoformat(timespec="seconds")

        for f in flags:
            ohlc = _ohlc_for(conn, next_date, f["symbol"])
            if not ohlc:
                continue
            _, day_high, day_low, day_close = ohlc
            close_t = f["spot_at_flag"]
            if not close_t or close_t <= 0:
                continue

            chg_close = (day_close - close_t) / close_t * 100.0
            if f["side"] == "bull":
                chg_peak = (day_high - close_t) / close_t * 100.0   # signed +
                close_didnt_cross = day_close >= close_t
                win_loose  = 1 if (chg_peak >= PEAK_PCT_WIN    and close_didnt_cross) else 0
                win_strict = 1 if (chg_peak >= PEAK_PCT_STRICT and close_didnt_cross) else 0
                win_close  = 1 if (day_close > close_t) else 0
            else:  # bear
                chg_peak = (close_t - day_low) / close_t * 100.0     # signed +
                close_didnt_cross = day_close <= close_t
                win_loose  = 1 if (chg_peak >= PEAK_PCT_WIN    and close_didnt_cross) else 0
                win_strict = 1 if (chg_peak >= PEAK_PCT_STRICT and close_didnt_cross) else 0
                win_close  = 1 if (day_close < close_t) else 0

            outcomes.append((
                f["flag_date"], rule_id, f["symbol"], f["side"], next_date,
                close_t, day_close, day_high, day_low,
                round(chg_close, 3), round(chg_peak, 3),
                win_loose, win_strict, win_close, resolved_at,
            ))

        if outcomes:
            conn.execute("BEGIN;")
            conn.execute(
                "DELETE FROM oi_thesis_outcome WHERE flag_date = ? AND rule_id = ?",
                (flag_date, rule_id),
            )
            conn.executemany("""
                INSERT INTO oi_thesis_outcome
                  (flag_date, rule_id, symbol, side, resolution_date,
                   spot_at_flag, spot_close_next, spot_high_next, spot_low_next,
                   chg_pct_close, chg_pct_peak,
                   win_peak_loose, win_peak_strict, win_close_only, resolved_at)
                VALUES (?,?,?,?,?, ?,?,?,?, ?,?, ?,?,?,?)
            """, outcomes)
            conn.execute("COMMIT;")

        log.info("verify: %s [%s] -> %s, resolved %d flags",
                 flag_date, rule_id, next_date, len(outcomes))
        return {"flag_date": flag_date, "rule_id": rule_id,
                "resolved": len(outcomes), "next_date": next_date}


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def report(n_days: int = 30, db_path: Optional[Path] = None) -> dict:
    """Print a rolling stats report. Returns the data dict for programmatic use."""
    init_db(db_path)
    cutoff = (_now_ist() - timedelta(days=n_days)).date().isoformat()
    with closing(_connect(db_path)) as conn:
        # Aggregate per side
        rows = conn.execute("""
            SELECT
                side,
                COUNT(*) AS n,
                SUM(win_peak_loose) AS w_loose,
                SUM(win_peak_strict) AS w_strict,
                SUM(win_close_only) AS w_close,
                AVG(chg_pct_close) AS avg_close,
                AVG(chg_pct_peak)  AS avg_peak
              FROM oi_thesis_outcome
             WHERE flag_date >= ?
             GROUP BY side
        """, (cutoff,)).fetchall()
        per_side = {r["side"]: dict(r) for r in rows}

        # Headline detail of last resolved date
        last_resolved = conn.execute(
            "SELECT MAX(flag_date) FROM oi_thesis_outcome"
        ).fetchone()[0]
        last_detail = []
        if last_resolved:
            last_detail = conn.execute("""
                SELECT f.symbol, f.side, f.rank, o.spot_at_flag, o.spot_close_next,
                       o.spot_high_next, o.spot_low_next,
                       o.chg_pct_peak, o.chg_pct_close,
                       o.win_peak_loose, o.win_peak_strict
                  FROM oi_thesis_outcome o
                  JOIN oi_thesis_flag    f USING (flag_date, symbol, side)
                 WHERE o.flag_date = ?
                 ORDER BY f.side, f.rank
            """, (last_resolved,)).fetchall()

    lines = []
    lines.append("=" * 78)
    lines.append(f"  OI Thesis Tracker — Rolling {n_days}d Report")
    lines.append(f"  Win loose: peak >= {PEAK_PCT_WIN}% AND close didn't cross. Strict: {PEAK_PCT_STRICT}%.")
    lines.append("=" * 78)
    if not per_side:
        lines.append("  No resolved flags yet in the window.")
    else:
        for side in ("bull", "bear"):
            d = per_side.get(side)
            if not d:
                lines.append(f"  {side.upper():<5}  no flags resolved")
                continue
            n = d["n"]
            wl = d["w_loose"] or 0
            ws = d["w_strict"] or 0
            wc = d["w_close"] or 0
            ac = d["avg_close"] or 0.0
            ap = d["avg_peak"] or 0.0
            lines.append(
                f"  {side.upper():<5}  n={n:<4} "
                f"loose={wl}/{n} ({100*wl/n:.0f}%)  "
                f"strict={ws}/{n} ({100*ws/n:.0f}%)  "
                f"close={wc}/{n} ({100*wc/n:.0f}%)  "
                f"avg_peak={ap:+.2f}%  avg_close={ac:+.2f}%"
            )

    if last_detail:
        lines.append("")
        lines.append(f"  Latest resolved day: {last_resolved}")
        lines.append("  " + "-" * 76)
        lines.append(f"  {'Side':<5}{'#':<3}{'Symbol':<13}{'T close':>9}{'T+1 close':>10}"
                     f"{'Peak':>8}{'Close':>8}  {'Win?':<6}")
        for r in last_detail:
            mark = "✓ loose"
            if r["win_peak_strict"]: mark = "✓ STRICT"
            elif not r["win_peak_loose"]: mark = "✗"
            lines.append(
                f"  {r['side']:<5}{r['rank']:<3}{r['symbol']:<13}"
                f"{(r['spot_at_flag'] or 0):>9.2f}{(r['spot_close_next'] or 0):>10.2f}"
                f"{(r['chg_pct_peak'] or 0):>+7.2f}%{(r['chg_pct_close'] or 0):>+7.2f}%  {mark}"
            )

    out = "\n".join(lines)
    print(out)
    return {"text": out, "per_side": per_side, "last_resolved": last_resolved}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _cli():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"

    if cmd == "init":
        init_db()
        print("OK: initialized")
        return

    if cmd == "capture":
        date = sys.argv[2] if len(sys.argv) > 2 else _today_str()
        if date == "today":
            date = _today_str()
        result = capture(date)
        print(f"OK: captured {result['bull']} bull / {result['bear']} bear flags for {result['flag_date']}")
        return

    if cmd == "verify":
        if len(sys.argv) < 3:
            print("Usage: verify YYYY-MM-DD"); sys.exit(2)
        result = verify(sys.argv[2])
        print(f"OK: resolved {result['resolved']} flags for {result['flag_date']} → {result['next_date']}")
        return

    if cmd == "daily":
        # Verify yesterday's flags first, then capture today's
        yesterday = (_now_ist() - timedelta(days=1)).date().isoformat()
        v = verify(yesterday)
        c = capture(_today_str())
        print(f"OK: verified {v['resolved']} from {yesterday}, captured {c['bull']}/{c['bear']} for today")
        return

    if cmd == "report":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 30
        report(n)
        return

    print(__doc__)
    sys.exit(2)


if __name__ == "__main__":
    _cli()
