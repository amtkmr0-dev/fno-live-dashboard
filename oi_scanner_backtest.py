"""
oi_scanner_backtest.py — Validate the OI Scanner rule against historical outcomes.

Rule under test (Convention A):
  BULL: ce_oi_chg < 0 AND pe_oi_chg > 0  → buy CE
  BEAR: ce_oi_chg > 0 AND pe_oi_chg < 0  → buy PE

For every historical EOD snapshot where the rule fired, look up the next trading
day's high/low/close and compute:
  - chg_pct_close: T+1 close move from T close (signed in dir of thesis)
  - chg_pct_peak:  peak T+1 move (T+1 high for bull, T+1 low for bear)
  - win_loose:  peak >= +0.75% AND close didn't cross
  - win_strict: peak >= +1.0%  AND close didn't cross
  - win_close:  close moved in expected direction

Then stratify by:
  - magnitude quintile (|pe_chg - ce_chg|)
  - PCR bucket
  - vol_surge bucket
  - mp_dist bucket (spot distance from max_pain)
  - sector

Output: overall hit rate + condition-stratified hit rates.
The conditions that materially boost hit rate vs baseline are your edge.

Usage:
  python3 oi_scanner_backtest.py             # run with defaults, print summary
  python3 oi_scanner_backtest.py --since 2026-05-15
  python3 oi_scanner_backtest.py --json      # machine-readable output

Caveats:
  - Honest about sample size: if N < 30 in any bucket, label as "noisy".
  - Outcomes only computable when next-day OHLC exists in stock_daily_ohlc.
  - This is the SAME framework oi_thesis_tracker uses for its rule_id="oi_div_v1",
    repurposed for the live scanner rule (which is the same rule, top-N free).
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from contextlib import closing
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Win thresholds (match oi_thesis_tracker)
PEAK_PCT_LOOSE  = 0.75
PEAK_PCT_STRICT = 1.0
LIQUIDITY_FLOOR = 100_000

DB_PATH = Path(__file__).parent / "data" / "quantra_history.db"


# ────────────────────────── Helpers ──────────────────────────
def _connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    p = db_path or DB_PATH
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    return conn


def _next_trading_date(conn: sqlite3.Connection, after: str) -> Optional[str]:
    """Earliest stock_daily_ohlc date strictly after `after`. Returns None if no data."""
    r = conn.execute(
        "SELECT MIN(trading_date) FROM stock_daily_ohlc WHERE trading_date > ?",
        (after,),
    ).fetchone()
    return r[0] if r and r[0] else None


def _ohlc_for(conn: sqlite3.Connection, trading_date: str, symbol: str):
    """Return (open, high, low, close) on trading_date for symbol, or None if missing."""
    r = conn.execute(
        "SELECT open, high, low, close FROM stock_daily_ohlc WHERE trading_date = ? AND symbol = ?",
        (trading_date, symbol),
    ).fetchone()
    if not r:
        return None
    return (r["open"], r["high"], r["low"], r["close"])


def _bucket_pcr(pcr: Optional[float]) -> str:
    if pcr is None: return "?"
    if pcr <= 0.5: return "PCR<=0.5"
    if pcr <= 0.7: return "PCR 0.5-0.7"
    if pcr <= 1.0: return "PCR 0.7-1.0"
    return "PCR>1.0"


def _bucket_surge(s: Optional[float]) -> str:
    if s is None: return "?"
    if s < 1.0: return "Surge<1"
    if s < 1.5: return "Surge 1.0-1.5"
    if s < 2.5: return "Surge 1.5-2.5"
    return "Surge>2.5"


def _bucket_mp(d: Optional[float]) -> str:
    if d is None: return "?"
    if d <= -5: return "MP<-5%"
    if d <= -2: return "MP -2 to -5%"
    if d <= 2:  return "MP +/-2%"
    if d <= 5:  return "MP 2-5%"
    return "MP>5%"


def _bucket_quintile(values: List[float], v: float) -> str:
    """Q1..Q5 by quintile rank of v in values. Q5 = top 20%."""
    if not values or v is None:
        return "?"
    qs = statistics.quantiles(values, n=5) if len(values) >= 5 else [max(values)] * 4
    if v >= qs[3]: return "Q5 (top)"
    if v >= qs[2]: return "Q4"
    if v >= qs[1]: return "Q3"
    if v >= qs[0]: return "Q2"
    return "Q1 (bottom)"


# ────────────────────────── Core ──────────────────────────
@dataclass
class Outcome:
    flag_date: str
    resolution_date: str
    symbol: str
    side: str            # 'bull' or 'bear'
    sector: Optional[str]
    spot_at_flag: float
    ce_oi_chg: int
    pe_oi_chg: int
    magnitude: int       # |pe_chg - ce_chg|
    pcr: Optional[float]
    vol_surge: Optional[float]
    mp_dist: Optional[float]
    spot_close_next: float
    chg_pct_close: float
    chg_pct_peak: float
    win_loose: int
    win_strict: int
    win_close: int


def collect_outcomes(conn: sqlite3.Connection, since: Optional[str] = None) -> List[Outcome]:
    """Walk every (date, symbol) EOD snapshot, find next-day OHLC, compute outcome."""
    where = "WHERE 1=1"
    params: List[Any] = []
    if since:
        where += " AND trading_date >= ?"
        params.append(since)

    # Take the LAST snapshot per (date, symbol) as the EOD signal
    eod_query = f"""
        WITH eod AS (
            SELECT trading_date, symbol, MAX(snap_ts) AS last_ts
            FROM chain_snapshot
            {where}
            GROUP BY trading_date, symbol
        )
        SELECT cs.*
        FROM chain_snapshot cs
        JOIN eod ON cs.trading_date = eod.trading_date
                AND cs.symbol      = eod.symbol
                AND cs.snap_ts     = eod.last_ts
        WHERE cs.total_oi >= ?
          AND ((cs.ce_oi_chg < 0 AND cs.pe_oi_chg > 0)
            OR (cs.ce_oi_chg > 0 AND cs.pe_oi_chg < 0))
    """
    params.append(LIQUIDITY_FLOOR)
    rows = conn.execute(eod_query, params).fetchall()

    # Pull sectors for stocks (best-effort, from stock_daily)
    sectors: Dict[str, str] = {}
    for r in conn.execute("SELECT DISTINCT symbol, sector FROM stock_daily").fetchall():
        if r["sector"]:
            sectors[r["symbol"]] = r["sector"]

    outcomes: List[Outcome] = []
    for row in rows:
        ce = row["ce_oi_chg"] or 0
        pe = row["pe_oi_chg"] or 0
        side = "bull" if (ce < 0 and pe > 0) else "bear"
        magnitude = abs(pe - ce)
        spot_at_flag = row["spot_ltp"]
        if not spot_at_flag or spot_at_flag <= 0:
            continue

        next_date = _next_trading_date(conn, row["trading_date"])
        if not next_date:
            continue
        ohlc = _ohlc_for(conn, next_date, row["symbol"])
        if not ohlc:
            continue
        _, day_high, day_low, day_close = ohlc

        chg_close = (day_close - spot_at_flag) / spot_at_flag * 100.0
        if side == "bull":
            chg_peak = (day_high - spot_at_flag) / spot_at_flag * 100.0
            close_held = day_close >= spot_at_flag
            close_dir  = day_close > spot_at_flag
        else:
            chg_peak = (spot_at_flag - day_low) / spot_at_flag * 100.0
            close_held = day_close <= spot_at_flag
            close_dir  = day_close < spot_at_flag

        outcomes.append(Outcome(
            flag_date=row["trading_date"],
            resolution_date=next_date,
            symbol=row["symbol"],
            side=side,
            sector=sectors.get(row["symbol"]),
            spot_at_flag=spot_at_flag,
            ce_oi_chg=ce,
            pe_oi_chg=pe,
            magnitude=magnitude,
            pcr=row["pcr"],
            vol_surge=row["vol_surge"],
            mp_dist=row["mp_dist_pct"],
            spot_close_next=day_close,
            chg_pct_close=round(chg_close, 3),
            chg_pct_peak=round(chg_peak, 3),
            win_loose=int(chg_peak >= PEAK_PCT_LOOSE  and close_held),
            win_strict=int(chg_peak >= PEAK_PCT_STRICT and close_held),
            win_close=int(close_dir),
        ))
    return outcomes


def stratify(outcomes: List[Outcome], side: Optional[str] = None) -> Dict[str, Any]:
    """Return summary stats + per-bucket hit rates for the chosen side (or all)."""
    pool = outcomes if side is None else [o for o in outcomes if o.side == side]
    n = len(pool)
    if not n:
        return {"n": 0, "msg": "no outcomes"}

    def rate(predicate_field: str) -> Dict[str, Any]:
        wins = sum(getattr(o, predicate_field) for o in pool)
        return {"n": n, "wins": wins, "rate_pct": round(100 * wins / n, 1)}

    # Magnitude quintile cutoffs (over all outcomes regardless of side)
    mag_values = sorted([o.magnitude for o in outcomes if o.magnitude is not None])

    def stratify_by(key_fn) -> Dict[str, Dict[str, Any]]:
        buckets: Dict[str, List[Outcome]] = {}
        for o in pool:
            k = key_fn(o)
            buckets.setdefault(k, []).append(o)
        return {
            k: {
                "n": len(v),
                "loose_pct": round(100 * sum(o.win_loose for o in v) / len(v), 1),
                "strict_pct": round(100 * sum(o.win_strict for o in v) / len(v), 1),
                "close_pct": round(100 * sum(o.win_close  for o in v) / len(v), 1),
                "avg_peak_pct": round(sum(o.chg_pct_peak  for o in v) / len(v), 2),
                "avg_close_pct": round(sum(o.chg_pct_close for o in v) / len(v), 2),
                "noisy": len(v) < 30,
            }
            for k, v in sorted(buckets.items(), key=lambda kv: -len(kv[1]))
        }

    avg_peak  = round(sum(o.chg_pct_peak  for o in pool) / n, 2)
    avg_close = round(sum(o.chg_pct_close for o in pool) / n, 2)

    return {
        "n": n,
        "overall": {
            "loose": rate("win_loose"),
            "strict": rate("win_strict"),
            "close": rate("win_close"),
            "avg_peak_pct": avg_peak,
            "avg_close_pct": avg_close,
        },
        "by_magnitude": stratify_by(lambda o: _bucket_quintile(mag_values, o.magnitude)),
        "by_pcr":       stratify_by(lambda o: _bucket_pcr(o.pcr)),
        "by_surge":     stratify_by(lambda o: _bucket_surge(o.vol_surge)),
        "by_mp_dist":   stratify_by(lambda o: _bucket_mp(o.mp_dist)),
        "by_sector":    stratify_by(lambda o: o.sector or "?"),
    }


def fmt_table(d: Dict[str, Dict[str, Any]], title: str, min_n: int = 5) -> str:
    """Pretty-print a stratification table."""
    lines = [f"  {title}"]
    if not d:
        lines.append("    (no data)")
        return "\n".join(lines)
    lines.append(f"    {'bucket':<22} {'n':>4} {'loose%':>8} {'strict%':>8} {'close%':>8} {'avg_peak':>10} {'avg_close':>10}")
    for k, v in d.items():
        if v["n"] < min_n:
            continue
        flag = " ⚠" if v.get("noisy") else ""
        lines.append(f"    {k:<22} {v['n']:>4} {v['loose_pct']:>7}% {v['strict_pct']:>7}% {v['close_pct']:>7}% {v['avg_peak_pct']:>+9.2f}% {v['avg_close_pct']:>+9.2f}%{flag}")
    return "\n".join(lines)


def print_report(outcomes: List[Outcome]):
    print("=" * 90)
    print(f"  OI Scanner — Backtest of bull/bear configuration rule")
    print(f"  Win loose: peak >= {PEAK_PCT_LOOSE}% AND close didn't cross | strict: peak >= {PEAK_PCT_STRICT}%")
    print(f"  Date range: {min(o.flag_date for o in outcomes) if outcomes else 'no data'}"
          f" to {max(o.flag_date for o in outcomes) if outcomes else ''}")
    print(f"  Total resolved outcomes: {len(outcomes)}")
    print("=" * 90)
    if not outcomes:
        print("\nNo resolvable outcomes yet. Need stock_daily_ohlc data for next trading day.")
        print("Suggested fix: run `python3 historical_data.py fetch SYM1,SYM2,...` for the universe.")
        return

    for side in ("bull", "bear"):
        s = stratify(outcomes, side=side)
        if s["n"] == 0:
            print(f"\n[{side.upper()}] no outcomes")
            continue
        print(f"\n[{side.upper()}]  N={s['n']}  baseline:")
        ov = s["overall"]
        print(f"  Loose hit rate  : {ov['loose']['wins']}/{ov['loose']['n']} = {ov['loose']['rate_pct']}%")
        print(f"  Strict hit rate : {ov['strict']['wins']}/{ov['strict']['n']} = {ov['strict']['rate_pct']}%")
        print(f"  Close hit rate  : {ov['close']['wins']}/{ov['close']['n']} = {ov['close']['rate_pct']}%")
        print(f"  Avg peak move   : {ov['avg_peak_pct']:+.2f}%")
        print(f"  Avg close move  : {ov['avg_close_pct']:+.2f}%")
        print()
        print(fmt_table(s["by_magnitude"], "By magnitude quintile (bigger = more position size):"))
        print()
        print(fmt_table(s["by_pcr"],       "By PCR bucket:"))
        print()
        print(fmt_table(s["by_surge"],     "By volume surge bucket:"))
        print()
        print(fmt_table(s["by_mp_dist"],   "By max-pain distance bucket:"))
        print()
        print(fmt_table(s["by_sector"],    "By sector:"))
        print()

    if len(outcomes) < 100:
        print()
        print("⚠  Sample size is small (<100 outcomes). Buckets marked ⚠ have N<30 — treat as suggestive, not conclusive.")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--since", help="Earliest flag_date to include (YYYY-MM-DD)")
    p.add_argument("--json", action="store_true", help="Output JSON instead of human-readable")
    p.add_argument("--side", choices=["bull", "bear"], help="Only one side")
    args = p.parse_args()

    with closing(_connect()) as conn:
        outcomes = collect_outcomes(conn, since=args.since)

    if args.side:
        outcomes = [o for o in outcomes if o.side == args.side]

    if args.json:
        result = {
            "n": len(outcomes),
            "outcomes": [asdict(o) for o in outcomes],
            "summary_bull": stratify(outcomes, side="bull"),
            "summary_bear": stratify(outcomes, side="bear"),
        }
        print(json.dumps(result, indent=2, default=str))
    else:
        print_report(outcomes)


if __name__ == "__main__":
    main()
