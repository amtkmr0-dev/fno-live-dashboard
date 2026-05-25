#!/usr/bin/env python3
"""
run_midcp_honest.py
===================
Runs fno_rsi_backtest.py's EXACT engine on MIDCPNIFTY index spot data
with all bias/bug fixes applied:

  FIX 1 — Entry at NEXT bar's open (not at trigger price)
  FIX 2 — Walk-forward: IS 2022-03→2024-12, OOS 2025-01→2026-05
  FIX 3 — No partial-exit or trailing (matches fno_rsi_backtest.py scope)
  FIX 4 — Cost = ₹50/trade (0.33 pts on lot=150)
  FIX 5 — No-overlap: skip signal if prior trade still open
  FIX 6 — SL/TGT in % (same grid as fno_rsi_backtest.py)
  FIX 7 — Best variant picked on IS only, tested once on OOS

SL grid  : [0.5, 0.75, 1.0, 1.5, 2.0, 3.0] %
TGT grid : [1.0, 1.5, 2.0, 3.0, 4.0, 5.0] %
TFs      : 5m, 15m, 30m
Sides    : bear, bull

Output: data/research/midcp_honest_<date>.md
"""

import math, sys
from datetime import datetime
from pathlib import Path
import pandas as pd

# ── Import the exact engine from fno_rsi_backtest.py ──────────
sys.path.insert(0, str(Path(__file__).parent.parent))
from fno_rsi_backtest import (
    wilder_rsi, find_rsi_pivots, detect_divergences,
    resample_ohlcv, load_parquet
)

DATA_1M  = Path("/Users/amitkumar/Desktop/nifty-analyzer/data/index_spot/MIDCPNIFTY_1m_5y.parquet")
OUT_DIR  = Path(__file__).parent.parent / "data" / "research"
OUT_DIR.mkdir(parents=True, exist_ok=True)

LOT          = 150
COST_RS      = 50.0
COST_PTS     = COST_RS / LOT          # 0.333 pts

SPLIT        = pd.Timestamp("2025-01-01", tz="Asia/Kolkata")

SL_PCT_GRID  = [0.5, 0.75, 1.0, 1.5, 2.0, 3.0]
TGT_PCT_GRID = [1.0, 1.5, 2.0, 3.0, 4.0, 5.0]
TIMEFRAMES   = {"5m": 5, "15m": 15, "30m": 30}
ENTRY_WINDOW = 10

# ══════════════════════════════════════════════════════════════
# HONEST TRADE SIMULATOR
# Uses fno_rsi_backtest.py's detection engine but fixes entry
# ══════════════════════════════════════════════════════════════

def simulate_honest(signals, side, candles):
    """
    For each signal:
      - Scan up to ENTRY_WINDOW bars for trigger hit (same as original)
      - FIX: Enter at NEXT bar's open (not at trigger price)
      - SL/TGT in % of entry price (same as original)
      - SL checked before TGT intrabar (same as original)
      - No-overlap: skip if prior trade still open
      - Cost deducted per trade
    Returns list of trade dicts.
    """
    N = len(candles)
    highs  = [c['high']  for c in candles]
    lows   = [c['low']   for c in candles]
    closes = [c['close'] for c in candles]
    opens  = [c['open']  for c in candles]

    trades = []
    last_exit_bar = -1

    for sig in signals:
        det_bar = sig['det_bar']
        trigger = sig['trigger']

        # No-overlap: skip if previous trade still open
        if det_bar <= last_exit_bar:
            continue

        # Phase 1: find trigger bar (same logic as original)
        entry_trigger_bar = None
        for b in range(det_bar, min(det_bar + ENTRY_WINDOW, N)):
            if side == 'bear' and lows[b] <= trigger:
                entry_trigger_bar = b; break
            elif side == 'bull' and highs[b] >= trigger:
                entry_trigger_bar = b; break

        if entry_trigger_bar is None:
            continue

        # FIX: enter at NEXT bar's open
        entry_bar = entry_trigger_bar + 1
        if entry_bar >= N:
            continue

        ep = opens[entry_bar]
        if ep <= 0:
            continue

        # Phase 2: iterate over SL/TGT grid — collect all results
        # (we return per-trade results for each config separately)
        trades.append({
            'entry_bar': entry_bar,
            'entry_price': ep,
            'entry_ts': candles[entry_bar]['ts'],
            'det_bar': det_bar,
            'rsi_at_signal': sig['rsi'],
            'trigger': trigger,
        })
        last_exit_bar = entry_bar  # will be updated after exit found

    return trades


def simulate_exits(entry_trades, side, candles, sl_pct, tgt_pct):
    """
    Given pre-computed entry trades, simulate exits for one SL/TGT config.
    Returns list of completed trade dicts with pnl.
    """
    N = len(candles)
    highs  = [c['high']  for c in candles]
    lows   = [c['low']   for c in candles]
    closes = [c['close'] for c in candles]

    results = []
    last_exit_bar = -1

    for t in entry_trades:
        entry_bar = t['entry_bar']

        # No-overlap check per config
        if entry_bar <= last_exit_bar:
            continue

        ep = t['entry_price']
        if side == 'bear':
            sl_lvl  = ep * (1 + sl_pct / 100.0)
            tgt_lvl = ep * (1 - tgt_pct / 100.0)
        else:
            sl_lvl  = ep * (1 - sl_pct / 100.0)
            tgt_lvl = ep * (1 + tgt_pct / 100.0)

        exit_bar = None; exit_price = None; result = None

        for b in range(entry_bar + 1, N):
            if side == 'bear':
                if highs[b] >= sl_lvl:
                    exit_bar = b; exit_price = sl_lvl; result = 'SL'; break
                if lows[b] <= tgt_lvl:
                    exit_bar = b; exit_price = tgt_lvl; result = 'TGT'; break
            else:
                if lows[b] <= sl_lvl:
                    exit_bar = b; exit_price = sl_lvl; result = 'SL'; break
                if highs[b] >= tgt_lvl:
                    exit_bar = b; exit_price = tgt_lvl; result = 'TGT'; break

        if exit_bar is None:
            exit_bar = N - 1
            exit_price = closes[exit_bar]
            result = 'EXPIRED'

        last_exit_bar = exit_bar

        pnl_pts = (ep - exit_price) if side == 'bear' else (exit_price - ep)
        pnl_pts -= COST_PTS  # deduct cost

        results.append({
            'entry_bar': entry_bar,
            'exit_bar': exit_bar,
            'entry_ts': t['entry_ts'],
            'exit_ts': candles[exit_bar]['ts'],
            'entry_price': round(ep, 2),
            'exit_price': round(exit_price, 2),
            'sl_lvl': round(sl_lvl, 2),
            'tgt_lvl': round(tgt_lvl, 2),
            'result': result,
            'pnl_pts': round(pnl_pts, 2),
            'pnl_rs': round(pnl_pts * LOT, 2),
            'rsi_at_signal': t['rsi_at_signal'],
        })

    return results

def stats(trades):
    if not trades:
        return {'n':0,'wr':0,'pf':0,'net_pts':0,'net_rs':0,
                'max_dd_pts':0,'mcl':0,'avg_bars':0,'monthly':{}}
    wins   = [t for t in trades if t['pnl_pts'] > 0]
    losses = [t for t in trades if t['pnl_pts'] <= 0]
    gp = sum(t['pnl_pts'] for t in wins)
    gl = abs(sum(t['pnl_pts'] for t in losses))
    net = sum(t['pnl_pts'] for t in trades)

    eq = peak = mdd = 0.0
    for t in trades:
        eq += t['pnl_pts']
        peak = max(peak, eq)
        mdd = max(mdd, peak - eq)

    mcl = cl = 0
    for t in trades:
        if t['pnl_pts'] <= 0: cl += 1; mcl = max(mcl, cl)
        else: cl = 0

    monthly = {}
    for t in trades:
        try:
            k = t['entry_ts'].strftime('%Y-%m')
            monthly.setdefault(k, {'n':0,'wins':0,'pnl':0.0})
            monthly[k]['n']    += 1
            monthly[k]['wins'] += 1 if t['pnl_pts'] > 0 else 0
            monthly[k]['pnl']  += t['pnl_pts']
        except: pass

    return {
        'n': len(trades), 'n_wins': len(wins), 'n_losses': len(losses),
        'wr': round(len(wins)/len(trades)*100, 1),
        'pf': round(gp/gl, 2) if gl > 0 else 999,
        'net_pts': round(net, 1),
        'net_rs': round(net * LOT, 0),
        'avg_win_pts': round(gp/len(wins), 1) if wins else 0,
        'avg_loss_pts': round(gl/len(losses), 1) if losses else 0,
        'max_dd_pts': round(mdd, 1),
        'max_dd_rs': round(mdd * LOT, 0),
        'mcl': mcl,
        'avg_bars': round(sum(t['exit_bar']-t['entry_bar'] for t in trades)/len(trades), 1),
        'monthly': monthly,
    }


def split(trades, split_ts):
    ins = [t for t in trades if t['entry_ts'] < split_ts]
    oos = [t for t in trades if t['entry_ts'] >= split_ts]
    return ins, oos

# ══════════════════════════════════════════════════════════════
# DATA LOADING
# ══════════════════════════════════════════════════════════════

def load_tf(tf_minutes):
    """Load 1m parquet, resample to tf_minutes, return candle dicts."""
    df = load_parquet(str(DATA_1M))
    # Convert to IST if needed
    if df['timestamp'].dt.tz is None:
        df['timestamp'] = df['timestamp'].dt.tz_localize('Asia/Kolkata')
    else:
        df['timestamp'] = df['timestamp'].dt.tz_convert('Asia/Kolkata')

    if tf_minutes > 1:
        df = resample_ohlcv(df, tf_minutes)
        # Re-apply IST tz after resample (resample may strip tz)
        if df['timestamp'].dt.tz is None:
            df['timestamp'] = df['timestamp'].dt.tz_localize('Asia/Kolkata')

    return [{'ts': r['timestamp'], 'open': float(r['open']), 'high': float(r['high']),
             'low': float(r['low']), 'close': float(r['close'])}
            for _, r in df.iterrows()]


# ══════════════════════════════════════════════════════════════
# REPORT
# ══════════════════════════════════════════════════════════════

def write_report(all_results, today):
    lines = []
    lines.append("# MIDCPNIFTY RSI Divergence — Honest Backtest (fno_rsi_backtest engine)")
    lines.append(f"**Date**: {today}  |  **Lot**: {LOT}  |  **Cost**: ₹{COST_RS}/trade")
    lines.append(f"**IS**: 2022-03 → 2024-12  |  **OOS**: 2025-01 → 2026-05")
    lines.append(f"**Entry**: Next bar open  |  **SL/TGT**: % of entry price  |  **No-overlap**: ON")
    lines.append(f"**SL grid**: {SL_PCT_GRID}%  |  **TGT grid**: {TGT_PCT_GRID}%")
    lines.append("")

    for tf in ['5m', '15m', '30m']:
        for side in ['bear', 'bull']:
            key = f"{tf}_{side}"
            data = all_results.get(key)
            if not data: continue

            lines.append(f"## {tf.upper()} — {'BEAR (Short)' if side=='bear' else 'BULL (Long)'}")
            lines.append("")
            lines.append("### In-Sample Grid (2022-03 → 2024-12) — sorted by PF")
            lines.append("")
            lines.append("| SL% | TGT% | RR | N | WR% | PF | Net pts | Net ₹ | MaxDD ₹ | MCL |")
            lines.append("|-----|------|----|---|-----|----|---------|-------|---------|-----|")

            is_grid = sorted(data['is_grid'], key=lambda r: r['pf'], reverse=True)
            for r in is_grid:
                if r['n'] < 10: continue
                rr = round(r['tgt_pct'] / r['sl_pct'], 1)
                lines.append(
                    f"| {r['sl_pct']} | {r['tgt_pct']} | 1:{rr} | {r['n']} | "
                    f"{r['wr']}% | **{r['pf']}** | {r['net_pts']:+.0f} | "
                    f"₹{r['net_rs']:+,.0f} | ₹{r['max_dd_rs']:,.0f} | {r['mcl']} |"
                )
            lines.append("")

            best_is = data.get('best_is')
            best_oos = data.get('best_oos')
            if not best_is: continue

            rr = round(best_is['tgt_pct'] / best_is['sl_pct'], 1)
            lines.append(f"### Walk-Forward: Best IS → OOS  (SL={best_is['sl_pct']}% | TGT={best_is['tgt_pct']}% | RR=1:{rr})")
            lines.append("")
            lines.append("| Window | N | WR% | PF | Net pts | Net ₹/lot | MaxDD ₹ | MCL |")
            lines.append("|--------|---|-----|----|---------|-----------|---------|-----|")
            lines.append(
                f"| **In-Sample** | {best_is['n']} | {best_is['wr']}% | "
                f"**{best_is['pf']}** | {best_is['net_pts']:+.0f} | "
                f"₹{best_is['net_rs']:+,.0f} | ₹{best_is['max_dd_rs']:,.0f} | {best_is['mcl']} |"
            )
            if best_oos:
                lines.append(
                    f"| **Out-of-Sample** | {best_oos['n']} | {best_oos['wr']}% | "
                    f"**{best_oos['pf']}** | {best_oos['net_pts']:+.0f} | "
                    f"₹{best_oos['net_rs']:+,.0f} | ₹{best_oos['max_dd_rs']:,.0f} | {best_oos['mcl']} |"
                )
                deg = (best_oos['pf'] / best_is['pf'] - 1) * 100 if best_is['pf'] > 0 else 0
                if best_oos['pf'] >= 1.3 and best_oos['n'] >= 15:
                    verdict = "✓ OOS holds — genuine edge"
                elif best_oos['pf'] >= 1.0:
                    verdict = "⚠ OOS marginal — barely profitable"
                else:
                    verdict = "✗ OOS fails — IS was curve-fit"
                lines.append("")
                lines.append(f"**Verdict**: {verdict}  |  PF degradation: {deg:+.1f}%")
                lines.append("")

                # OOS monthly
                m = best_oos.get('monthly', {})
                if m:
                    lines.append("#### OOS Monthly")
                    lines.append("")
                    lines.append("| Month | N | WR% | PnL pts | PnL ₹ |")
                    lines.append("|-------|---|-----|---------|-------|")
                    for k in sorted(m.keys()):
                        rec = m[k]
                        wr = round(rec['wins']/rec['n']*100, 0) if rec['n'] else 0
                        lines.append(f"| {k} | {rec['n']} | {wr:.0f}% | {rec['pnl']:+.1f} | ₹{rec['pnl']*LOT:+,.0f} |")
                    lines.append("")

    # Final summary
    lines.append("## Final OOS Summary")
    lines.append("")
    lines.append("| TF | Side | SL% | TGT% | RR | OOS N | OOS WR% | OOS PF | OOS Net ₹ | Verdict |")
    lines.append("|----|------|-----|------|----|-------|---------|--------|-----------|---------|")
    for tf in ['5m', '15m', '30m']:
        for side in ['bear', 'bull']:
            data = all_results.get(f"{tf}_{side}")
            if not data or not data.get('best_oos'): continue
            bi = data['best_is']; bo = data['best_oos']
            rr = round(bi['tgt_pct'] / bi['sl_pct'], 1)
            v = "✓" if bo['pf'] >= 1.3 and bo['n'] >= 15 else ("⚠" if bo['pf'] >= 1.0 else "✗")
            lines.append(
                f"| {tf} | {'Bear' if side=='bear' else 'Bull'} | "
                f"{bi['sl_pct']}% | {bi['tgt_pct']}% | 1:{rr} | "
                f"{bo['n']} | {bo['wr']}% | **{bo['pf']}** | "
                f"₹{bo['net_rs']:+,.0f} | {v} |"
            )
    lines.append("")

    out = OUT_DIR / f"midcp_honest_{today}.md"
    out.write_text("\n".join(lines))
    return out

# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════

def main():
    today = datetime.now().strftime('%Y-%m-%d')
    all_results = {}

    for tf_label, tf_min in TIMEFRAMES.items():
        print(f"\nLoading {tf_label} data...", end=" ", flush=True)
        candles = load_tf(tf_min)
        print(f"{len(candles)} bars  ({candles[0]['ts'].date()} → {candles[-1]['ts'].date()})")

        # Build arrays for the engine
        highs  = [c['high']  for c in candles]
        lows   = [c['low']   for c in candles]
        closes = [c['close'] for c in candles]

        # RSI + pivots (once per TF)
        rsi_vals = wilder_rsi(closes)
        pivot_lows, pivot_highs = find_rsi_pivots(rsi_vals)
        bear_sigs, bull_sigs = detect_divergences(highs, lows, rsi_vals, pivot_lows, pivot_highs)
        print(f"  Signals: {len(bear_sigs)} bear, {len(bull_sigs)} bull")

        for side, signals in [('bear', bear_sigs), ('bull', bull_sigs)]:
            key = f"{tf_label}_{side}"
            print(f"\n  ── {tf_label} {side} ({len(signals)} signals) ──")

            # Get entry bars once (no-overlap applied at entry level)
            entry_trades = simulate_honest(signals, side, candles)
            print(f"     Entries found: {len(entry_trades)}")

            # Sweep grid — IS only
            is_grid = []
            for sl_pct in SL_PCT_GRID:
                for tgt_pct in TGT_PCT_GRID:
                    full_trades = simulate_exits(entry_trades, side, candles, sl_pct, tgt_pct)
                    is_trades, _ = split(full_trades, SPLIT)
                    s = stats(is_trades)
                    s['sl_pct'] = sl_pct; s['tgt_pct'] = tgt_pct
                    is_grid.append(s)
                    print(f"     SL={sl_pct}% TGT={tgt_pct}%  IS: N={s['n']} WR={s['wr']}% PF={s['pf']}")

            # Pick best IS config (PF, min 10 trades)
            valid = [r for r in is_grid if r['n'] >= 10]
            if not valid:
                print(f"     No IS configs with ≥10 trades")
                all_results[key] = {'is_grid': is_grid, 'best_is': None, 'best_oos': None}
                continue

            best_cfg = max(valid, key=lambda r: r['pf'])
            print(f"\n     BEST IS: SL={best_cfg['sl_pct']}% TGT={best_cfg['tgt_pct']}%  "
                  f"PF={best_cfg['pf']} WR={best_cfg['wr']}%")

            # OOS test — run best config on full data, split out OOS
            full_trades = simulate_exits(entry_trades, side, candles,
                                         best_cfg['sl_pct'], best_cfg['tgt_pct'])
            _, oos_trades = split(full_trades, SPLIT)
            oos_s = stats(oos_trades)
            oos_s['sl_pct'] = best_cfg['sl_pct']; oos_s['tgt_pct'] = best_cfg['tgt_pct']
            print(f"     OOS:      N={oos_s['n']} WR={oos_s['wr']}% PF={oos_s['pf']} "
                  f"Net=₹{oos_s['net_rs']:+,.0f}")

            all_results[key] = {
                'is_grid': is_grid,
                'best_is': best_cfg,
                'best_oos': oos_s,
            }

    out = write_report(all_results, today)
    print(f"\n\nReport → {out}")

    # Console final table
    print("\n" + "═"*75)
    print("  FINAL OOS RESULTS (honest, bias-free)")
    print("═"*75)
    print(f"  {'TF':<5}{'Side':<6}{'SL%':>5}{'TGT%':>6}{'RR':>5}{'N':>5}{'WR%':>7}{'PF':>6}{'Net ₹':>12}{'Verdict'}")
    print("  " + "─"*73)
    for tf in ['5m', '15m', '30m']:
        for side in ['bear', 'bull']:
            data = all_results.get(f"{tf}_{side}")
            if not data or not data.get('best_oos'): continue
            bi = data['best_is']; bo = data['best_oos']
            rr = round(bi['tgt_pct'] / bi['sl_pct'], 1)
            v = "✓ Edge" if bo['pf'] >= 1.3 and bo['n'] >= 15 else ("⚠ Marginal" if bo['pf'] >= 1.0 else "✗ No edge")
            print(f"  {tf:<5}{side:<6}{bi['sl_pct']:>5}{bi['tgt_pct']:>6}  1:{rr:<3}"
                  f"{bo['n']:>5}{bo['wr']:>6}%{bo['pf']:>6}  ₹{bo['net_rs']:>+9,.0f}  {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
