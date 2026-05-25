#!/usr/bin/env python3
"""
backtest_rsi_divergence.py
==========================
Backtest the Pine-exact RSI Divergence strategy on MIDCPNIFTY
using local parquet files (15m and 30m spot data).

Usage:
    python3 scripts/backtest_rsi_divergence.py
    python3 scripts/backtest_rsi_divergence.py --tf 15m
    python3 scripts/backtest_rsi_divergence.py --tf 30m
    python3 scripts/backtest_rsi_divergence.py --tf both

Output:
    - Console summary table
    - data/research/backtest_rsi_div_MIDCPNIFTY.md
"""

import argparse
import math
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd

# ── Paths ──────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
DATA_DIR = Path("/Users/amitkumar/Desktop/nifty-analyzer/data/index_spot")
OUT_DIR = ROOT / "data" / "research"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ── Strategy Parameters (from scanner config) ──────────────────
MIDCPNIFTY_CONFIG = {
    "15m": {
        "side": "bear",
        "sl_pts": 40,
        "tgt_pts": 100,
        "label": "15m Bear (Bearish Divergence → Short)",
    },
    "30m": {
        "side": "bull",
        "sl_pts": 30,
        "tgt_pts": 100,
        "label": "30m Bull (Bullish Divergence → Long)",
    },
}

# ── RSI Divergence Detection Parameters ───────────────────────
RSI_LEN     = 14
PIVOT_LEFT  = 5
PIVOT_RIGHT = 5
MIN_BARS    = 5
MAX_BARS    = 60
ENTRY_WINDOW = 10

IST = timezone(timedelta(hours=5, minutes=30))


# ══════════════════════════════════════════════════════════════
# CORE FUNCTIONS (Pine-exact, same as live scanner)
# ══════════════════════════════════════════════════════════════

def compute_rsi(closes: list, period: int = RSI_LEN) -> list:
    """Wilder's RSI — matches TradingView/Pine exactly."""
    rsi = [float('nan')] * len(closes)
    if len(closes) < period + 1:
        return rsi
    gains, losses = [], []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rsi[period] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1 + avg_gain / avg_loss)
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rsi[i + 1] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1 + avg_gain / avg_loss)
    return rsi


def find_pivots(values: list, left: int, right: int):
    """Find pivot highs and lows — matches Pine ta.pivothigh/ta.pivotlow."""
    n = len(values)
    pl = [False] * n
    ph = [False] * n
    for i in range(left, n - right):
        val = values[i]
        if math.isnan(val):
            continue
        neighbors = [j for j in range(i - left, i + right + 1)
                     if j != i and 0 <= j < n and not math.isnan(values[j])]
        if len(neighbors) < left + right:
            continue
        pl[i] = all(values[j] > val for j in neighbors)
        ph[i] = all(values[j] < val for j in neighbors)
    return pl, ph


def detect_divergences(candles: list) -> list:
    """
    Pine-exact divergence detection.
    Compares only with the immediately previous pivot.
    Returns list of divergence dicts sorted by detection bar.
    """
    closes = [c["close"] for c in candles]
    highs  = [c["high"]  for c in candles]
    lows   = [c["low"]   for c in candles]
    N = len(candles)

    rsi_vals = compute_rsi(closes)
    rsi_pl, rsi_ph = find_pivots(rsi_vals, PIVOT_LEFT, PIVOT_RIGHT)

    # Build detection events (detected PIVOT_RIGHT bars after the pivot)
    pl_events = []  # (detection_bar, pivot_bar, low, rsi, high_of_pivot_candle)
    ph_events = []  # (detection_bar, pivot_bar, high, rsi, low_of_pivot_candle)
    for i in range(N):
        pb = i - PIVOT_RIGHT
        if pb < 0:
            continue
        if rsi_pl[pb]:
            pl_events.append((i, pb, lows[pb], rsi_vals[pb], highs[pb]))
        if rsi_ph[pb]:
            ph_events.append((i, pb, highs[pb], rsi_vals[pb], lows[pb]))

    divergences = []

    # Bullish: price lower low + RSI higher low
    for idx in range(1, len(pl_events)):
        det_bar, pb, low_val, rsi_val, high_val = pl_events[idx]
        prev_det, prev_pb, prev_low, prev_rsi, _ = pl_events[idx - 1]
        barssince = det_bar - (prev_det + 1)
        if barssince < MIN_BARS or barssince > MAX_BARS:
            continue
        if low_val < prev_low and rsi_val > prev_rsi:
            divergences.append({
                'type': 'bullish',
                'det_bar': det_bar,
                'pivot_bar': pb,
                'ts': candles[det_bar]['ts'],
                'trigger': high_val,       # entry above this
                'pivot_low': low_val,
                'rsi_val': round(rsi_val, 2),
                'prev_rsi': round(prev_rsi, 2),
                'prev_low': prev_low,
                'det_candle_low': candles[det_bar]['low'],
                'det_candle_high': candles[det_bar]['high'],
            })

    # Bearish: price higher high + RSI lower high
    for idx in range(1, len(ph_events)):
        det_bar, pb, high_val, rsi_val, low_val = ph_events[idx]
        prev_det, prev_pb, prev_high, prev_rsi, _ = ph_events[idx - 1]
        barssince = det_bar - (prev_det + 1)
        if barssince < MIN_BARS or barssince > MAX_BARS:
            continue
        if high_val > prev_high and rsi_val < prev_rsi:
            divergences.append({
                'type': 'bearish',
                'det_bar': det_bar,
                'pivot_bar': pb,
                'ts': candles[det_bar]['ts'],
                'trigger': low_val,        # entry below this
                'pivot_high': high_val,
                'rsi_val': round(rsi_val, 2),
                'prev_rsi': round(prev_rsi, 2),
                'prev_high': prev_high,
                'det_candle_low': candles[det_bar]['low'],
                'det_candle_high': candles[det_bar]['high'],
            })

    divergences.sort(key=lambda d: d['det_bar'])
    return divergences


# ══════════════════════════════════════════════════════════════
# BACKTEST ENGINE
# ══════════════════════════════════════════════════════════════

def run_backtest(candles: list, side: str, sl_pts: float, tgt_pts: float) -> dict:
    """
    Walk-forward backtest over the full candle history.
    For each divergence, scan forward for entry trigger, then simulate trade.

    Returns dict with trades list and summary stats.
    """
    N = len(candles)
    all_divs = detect_divergences(candles)

    # Filter to the side we care about
    divs = [d for d in all_divs if d['type'] == ('bearish' if side == 'bear' else 'bullish')]

    trades = []
    used_entry_bars = set()  # prevent double-entry on same bar

    for div in divs:
        det_bar = div['det_bar']
        trigger = div['trigger']
        div_type = div['type']

        # Scan forward for entry trigger within ENTRY_WINDOW bars
        for i in range(det_bar + 1, min(det_bar + ENTRY_WINDOW + 1, N)):
            if i in used_entry_bars:
                continue
            c = candles[i]

            entry_triggered = False
            if div_type == 'bullish' and c['close'] > trigger:
                entry_triggered = True
                direction = 'LONG'
            elif div_type == 'bearish' and c['close'] < trigger:
                entry_triggered = True
                direction = 'SHORT'

            if not entry_triggered:
                continue

            entry_price = c['close']
            entry_ts    = c['ts']
            entry_bar   = i
            used_entry_bars.add(i)

            sl_level  = entry_price - sl_pts  if direction == 'LONG' else entry_price + sl_pts
            tgt_level = entry_price + tgt_pts if direction == 'LONG' else entry_price - tgt_pts

            # Simulate trade: scan forward bar by bar
            outcome = 'OPEN'
            exit_price = None
            exit_ts    = None
            exit_bar   = None
            bars_held  = 0
            max_bars   = 200  # max hold time

            for j in range(entry_bar + 1, min(entry_bar + max_bars + 1, N)):
                bar = candles[j]
                bars_held = j - entry_bar

                if direction == 'LONG':
                    # Check SL first (conservative — intrabar SL)
                    if bar['low'] <= sl_level:
                        outcome    = 'SL'
                        exit_price = sl_level
                        exit_ts    = bar['ts']
                        exit_bar   = j
                        break
                    if bar['high'] >= tgt_level:
                        outcome    = 'TGT'
                        exit_price = tgt_level
                        exit_ts    = bar['ts']
                        exit_bar   = j
                        break
                else:  # SHORT
                    if bar['high'] >= sl_level:
                        outcome    = 'SL'
                        exit_price = sl_level
                        exit_ts    = bar['ts']
                        exit_bar   = j
                        break
                    if bar['low'] <= tgt_level:
                        outcome    = 'TGT'
                        exit_price = tgt_level
                        exit_ts    = bar['ts']
                        exit_bar   = j
                        break

            # If still open at max_bars, close at last bar's close
            if outcome == 'OPEN' and exit_bar is None:
                j = min(entry_bar + max_bars, N - 1)
                exit_price = candles[j]['close']
                exit_ts    = candles[j]['ts']
                exit_bar   = j
                bars_held  = j - entry_bar
                pnl = (exit_price - entry_price) if direction == 'LONG' else (entry_price - exit_price)
                outcome = 'TGT' if pnl > 0 else 'SL'

            pnl_pts = (exit_price - entry_price) if direction == 'LONG' else (entry_price - exit_price)

            trades.append({
                'direction':   direction,
                'div_type':    div_type,
                'entry_ts':    entry_ts,
                'entry_price': round(entry_price, 2),
                'exit_ts':     exit_ts,
                'exit_price':  round(exit_price, 2),
                'sl_level':    round(sl_level, 2),
                'tgt_level':   round(tgt_level, 2),
                'outcome':     outcome,
                'pnl_pts':     round(pnl_pts, 2),
                'bars_held':   bars_held,
                'rsi_at_div':  div['rsi_val'],
                'det_bar':     det_bar,
                'entry_bar':   entry_bar,
            })
            break  # one trade per divergence

    return _summarise(trades, sl_pts, tgt_pts)


def _summarise(trades: list, sl_pts: float, tgt_pts: float) -> dict:
    """Compute summary statistics from trade list."""
    if not trades:
        return {'trades': [], 'n': 0, 'win_rate': 0, 'profit_factor': 0,
                'net_pts': 0, 'avg_win': 0, 'avg_loss': 0, 'max_dd': 0}

    wins   = [t for t in trades if t['outcome'] == 'TGT']
    losses = [t for t in trades if t['outcome'] == 'SL']

    gross_profit = sum(t['pnl_pts'] for t in wins)
    gross_loss   = abs(sum(t['pnl_pts'] for t in losses))
    net_pts      = sum(t['pnl_pts'] for t in trades)

    # Max drawdown (running equity in points)
    equity = 0.0
    peak   = 0.0
    max_dd = 0.0
    for t in trades:
        equity += t['pnl_pts']
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd

    # Consecutive losses
    max_consec_loss = 0
    cur_consec = 0
    for t in trades:
        if t['outcome'] == 'SL':
            cur_consec += 1
            max_consec_loss = max(max_consec_loss, cur_consec)
        else:
            cur_consec = 0

    # Monthly breakdown
    monthly = {}
    for t in trades:
        try:
            ts = t['entry_ts']
            if hasattr(ts, 'strftime'):
                key = ts.strftime('%Y-%m')
            else:
                key = str(ts)[:7]
            monthly.setdefault(key, {'pnl': 0, 'n': 0, 'wins': 0})
            monthly[key]['pnl']  += t['pnl_pts']
            monthly[key]['n']    += 1
            monthly[key]['wins'] += 1 if t['outcome'] == 'TGT' else 0
        except Exception:
            pass

    return {
        'trades':          trades,
        'n':               len(trades),
        'n_wins':          len(wins),
        'n_losses':        len(losses),
        'win_rate':        round(len(wins) / len(trades) * 100, 1) if trades else 0,
        'profit_factor':   round(gross_profit / gross_loss, 2) if gross_loss > 0 else float('inf'),
        'net_pts':         round(net_pts, 1),
        'gross_profit':    round(gross_profit, 1),
        'gross_loss':      round(gross_loss, 1),
        'avg_win_pts':     round(gross_profit / len(wins), 1) if wins else 0,
        'avg_loss_pts':    round(gross_loss / len(losses), 1) if losses else 0,
        'avg_bars_held':   round(sum(t['bars_held'] for t in trades) / len(trades), 1),
        'max_dd_pts':      round(max_dd, 1),
        'max_consec_loss': max_consec_loss,
        'monthly':         monthly,
        'sl_pts':          sl_pts,
        'tgt_pts':         tgt_pts,
        'rr':              round(tgt_pts / sl_pts, 2),
    }


# ══════════════════════════════════════════════════════════════
# DATA LOADING
# ══════════════════════════════════════════════════════════════

def load_parquet(tf: str) -> list:
    """Load MIDCPNIFTY parquet and return list of candle dicts."""
    fname = f"MIDCPNIFTY_{tf}_5y.parquet"
    path  = DATA_DIR / fname
    if not path.exists():
        raise FileNotFoundError(f"Parquet not found: {path}")

    df = pd.read_parquet(path)
    df = df.sort_values('timestamp').reset_index(drop=True)

    # Convert UTC timestamps to IST
    df['ts_ist'] = df['timestamp'].dt.tz_convert('Asia/Kolkata')

    # Filter to market hours only (09:15 – 15:30 IST)
    df = df[
        (df['ts_ist'].dt.time >= pd.Timestamp('09:15').time()) &
        (df['ts_ist'].dt.time <= pd.Timestamp('15:30').time())
    ].reset_index(drop=True)

    candles = []
    for _, row in df.iterrows():
        candles.append({
            'ts':     row['ts_ist'],
            'open':   float(row['open']),
            'high':   float(row['high']),
            'low':    float(row['low']),
            'close':  float(row['close']),
            'volume': int(row['volume']),
        })
    return candles


# ══════════════════════════════════════════════════════════════
# REPORT GENERATION
# ══════════════════════════════════════════════════════════════

def print_summary(label: str, result: dict):
    """Print a clean console summary."""
    print(f"\n{'═'*60}")
    print(f"  {label}")
    print(f"{'═'*60}")
    print(f"  Trades       : {result['n']}")
    print(f"  Win Rate     : {result['win_rate']}%  ({result['n_wins']}W / {result['n_losses']}L)")
    print(f"  Profit Factor: {result['profit_factor']}")
    print(f"  Net Points   : {result['net_pts']:+.1f}")
    print(f"  Gross Profit : +{result['gross_profit']:.1f} pts")
    print(f"  Gross Loss   : -{result['gross_loss']:.1f} pts")
    print(f"  Avg Win      : +{result['avg_win_pts']:.1f} pts")
    print(f"  Avg Loss     : -{result['avg_loss_pts']:.1f} pts")
    print(f"  RR           : 1:{result['rr']}")
    print(f"  Max Drawdown : {result['max_dd_pts']:.1f} pts")
    print(f"  Max Consec L : {result['max_consec_loss']}")
    print(f"  Avg Bars Held: {result['avg_bars_held']}")

    # Net Rs (lot size 150)
    lot = 150
    net_rs = result['net_pts'] * lot
    print(f"\n  Net Rs/lot   : ₹{net_rs:,.0f}  (lot={lot})")
    print(f"  Net Rs/10lot : ₹{net_rs*10:,.0f}")

    # Monthly table (last 12 months)
    monthly = result.get('monthly', {})
    if monthly:
        print(f"\n  Monthly Breakdown (last 12):")
        print(f"  {'Month':<10} {'Trades':>6} {'WR%':>6} {'PnL pts':>10}")
        print(f"  {'-'*36}")
        for k in sorted(monthly.keys())[-12:]:
            m = monthly[k]
            wr = round(m['wins'] / m['n'] * 100, 0) if m['n'] else 0
            print(f"  {k:<10} {m['n']:>6} {wr:>5.0f}% {m['pnl']:>+10.1f}")


def write_markdown(results_15m: dict, results_30m: dict):
    """Write a research markdown report."""
    today = datetime.now().strftime('%Y-%m-%d')
    lines = []
    lines.append(f"# RSI Divergence Backtest — MIDCPNIFTY")
    lines.append(f"**Date**: {today}  |  **Data**: 5-year spot (parquet)  |  **Lot size**: 150")
    lines.append("")
    lines.append("## Strategy")
    lines.append("- **Detection**: Pine-exact RSI(14) pivot divergence (5L/5R pivots)")
    lines.append("- **Entry**: Close crosses trigger level within 10 bars of detection")
    lines.append("- **Exit**: Fixed SL/TGT in index points (intrabar simulation)")
    lines.append("- **Bars since**: 5–60 bars between consecutive pivots")
    lines.append("")

    for label, result, cfg_key in [
        ("15m Bear (Bearish Divergence → Short PE)", results_15m, "15m"),
        ("30m Bull (Bullish Divergence → Long CE)", results_30m, "30m"),
    ]:
        if result is None:
            continue
        lines.append(f"## {label}")
        lines.append("")
        lines.append(f"| Metric | Value |")
        lines.append(f"|--------|-------|")
        lines.append(f"| Trades | {result['n']} |")
        lines.append(f"| Win Rate | **{result['win_rate']}%** |")
        lines.append(f"| Profit Factor | **{result['profit_factor']}** |")
        lines.append(f"| Net Points | **{result['net_pts']:+.1f}** |")
        lines.append(f"| Gross Profit | +{result['gross_profit']:.1f} pts |")
        lines.append(f"| Gross Loss | -{result['gross_loss']:.1f} pts |")
        lines.append(f"| Avg Win | +{result['avg_win_pts']:.1f} pts |")
        lines.append(f"| Avg Loss | -{result['avg_loss_pts']:.1f} pts |")
        lines.append(f"| Risk:Reward | 1:{result['rr']} |")
        lines.append(f"| Max Drawdown | {result['max_dd_pts']:.1f} pts |")
        lines.append(f"| Max Consec Losses | {result['max_consec_loss']} |")
        lines.append(f"| Avg Bars Held | {result['avg_bars_held']} |")
        lines.append(f"| SL / TGT | {result['sl_pts']} / {result['tgt_pts']} pts |")
        lines.append(f"| Net ₹ (1 lot) | ₹{result['net_pts']*150:,.0f} |")
        lines.append("")

        # Monthly table
        monthly = result.get('monthly', {})
        if monthly:
            lines.append("### Monthly Breakdown")
            lines.append("")
            lines.append("| Month | Trades | Win% | PnL pts | PnL ₹ (1 lot) |")
            lines.append("|-------|--------|------|---------|---------------|")
            for k in sorted(monthly.keys()):
                m = monthly[k]
                wr = round(m['wins'] / m['n'] * 100, 1) if m['n'] else 0
                pnl_rs = m['pnl'] * 150
                lines.append(f"| {k} | {m['n']} | {wr}% | {m['pnl']:+.1f} | ₹{pnl_rs:+,.0f} |")
            lines.append("")

        # Last 20 trades
        trades = result.get('trades', [])[-20:]
        if trades:
            lines.append("### Last 20 Trades")
            lines.append("")
            lines.append("| # | Entry | Direction | Entry | Exit | SL | TGT | Outcome | PnL pts |")
            lines.append("|---|-------|-----------|-------|------|----|-----|---------|---------|")
            for i, t in enumerate(trades, 1):
                ets = str(t['entry_ts'])[:16] if t['entry_ts'] else '—'
                lines.append(
                    f"| {i} | {ets} | {t['direction']} | {t['entry_price']} | "
                    f"{t['exit_price']} | {t['sl_level']} | {t['tgt_level']} | "
                    f"**{t['outcome']}** | {t['pnl_pts']:+.1f} |"
                )
            lines.append("")

    out_path = OUT_DIR / f"backtest_rsi_div_MIDCPNIFTY_{today}.md"
    out_path.write_text("\n".join(lines))
    print(f"\n  Report saved → {out_path}")
    return out_path


# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="RSI Divergence Backtest — MIDCPNIFTY")
    parser.add_argument("--tf", choices=["15m", "30m", "both"], default="both",
                        help="Timeframe to backtest (default: both)")
    args = parser.parse_args()

    results = {}

    if args.tf in ("15m", "both"):
        print("Loading MIDCPNIFTY 15m data...", end=" ", flush=True)
        candles_15m = load_parquet("15m")
        print(f"{len(candles_15m)} bars ({candles_15m[0]['ts'].date()} → {candles_15m[-1]['ts'].date()})")
        cfg = MIDCPNIFTY_CONFIG["15m"]
        print("Running 15m Bear backtest...")
        results["15m"] = run_backtest(candles_15m, cfg["side"], cfg["sl_pts"], cfg["tgt_pts"])
        print_summary(cfg["label"], results["15m"])

    if args.tf in ("30m", "both"):
        print("\nLoading MIDCPNIFTY 30m data...", end=" ", flush=True)
        candles_30m = load_parquet("30m")
        print(f"{len(candles_30m)} bars ({candles_30m[0]['ts'].date()} → {candles_30m[-1]['ts'].date()})")
        cfg = MIDCPNIFTY_CONFIG["30m"]
        print("Running 30m Bull backtest...")
        results["30m"] = run_backtest(candles_30m, cfg["side"], cfg["sl_pts"], cfg["tgt_pts"])
        print_summary(cfg["label"], results["30m"])

    # Write markdown report
    write_markdown(results.get("15m"), results.get("30m"))

    return 0


if __name__ == "__main__":
    sys.exit(main())
