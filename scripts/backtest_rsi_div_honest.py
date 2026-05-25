#!/usr/bin/env python3
"""
backtest_rsi_div_honest.py
==========================
Bug-fixed, walk-forward, cost-adjusted RSI divergence backtest on MIDCPNIFTY.

Fixes applied vs sweep:
  BUG 1: Partial-exit P&L now correctly halves residual leg
  BUG 2: Trailing SL updates at end-of-bar (no intrabar look-ahead)
  BUG 3: Partial intrabar SL/TGT order — SL hit assumed first if both possible
  ENTRY: Now at NEXT bar's open (was: trigger bar close)
  COSTS: 0.33 pts/trade deducted (₹50 round-trip on lot=150)
  W-F:   2022-2024 in-sample → pick best variant → 2025-2026 out-of-sample

Output: data/research/backtest_rsi_div_HONEST_MIDCPNIFTY_<date>.md
"""

import math, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
import pandas as pd

ROOT    = Path(__file__).parent.parent
DATA_15 = Path("/Users/amitkumar/Desktop/nifty-analyzer/data/index_spot/MIDCPNIFTY_15m_5y.parquet")
DATA_30 = Path("/Users/amitkumar/Desktop/nifty-analyzer/data/index_spot/MIDCPNIFTY_30m_5y.parquet")
DATA_1M = Path("/Users/amitkumar/Desktop/nifty-analyzer/data/index_spot/MIDCPNIFTY_1m_5y.parquet")
OUT_DIR = ROOT / "data" / "research"
OUT_DIR.mkdir(parents=True, exist_ok=True)

LOT          = 150
COST_RS      = 50.0           # ₹50 round-trip per trade (Zerodha/Upstox typical)
COST_PTS     = COST_RS / LOT  # 0.333 pts per trade

# Walk-forward split
SPLIT_DATE = pd.Timestamp('2025-01-01', tz='Asia/Kolkata')

# ── RSI / Pivot params ─────────────────────────────────────────
RSI_LEN     = 14
PIVOT_LEFT  = 5
PIVOT_RIGHT = 5
MIN_BARS    = 5
MAX_BARS    = 60
ENTRY_WINDOW = 10

# Reduced sweep grid — only the variants worth testing
SWEEP = [
    ('fixed', 30,  'fixed', 60),
    ('fixed', 30,  'fixed', 90),
    ('fixed', 40,  'fixed', 80),
    ('fixed', 40,  'fixed', 100),
    ('fixed', 40,  'fixed', 120),
    ('fixed', 50,  'fixed', 100),
    ('fixed', 50,  'fixed', 150),
    ('fixed', 60,  'fixed', 120),
    ('signal_candle', 20, 'fixed', 80),
    ('signal_candle', 20, 'fixed', 120),
    ('atr', 10, 'fixed', 80),
    ('atr', 15, 'fixed', 100),
    ('atr', 20, 'fixed', 120),
    ('fixed', 40, 'trailing', 25),
    ('fixed', 50, 'trailing', 30),
    ('fixed', 40, 'partial', 80),
    ('fixed', 40, 'partial', 100),
    ('fixed', 50, 'partial', 100),
]

# ══════════════════════════════════════════════════════════════
# CORE: RSI + PIVOTS + DIVERGENCE  (Pine-exact)
# ══════════════════════════════════════════════════════════════

def compute_rsi(closes, period=RSI_LEN):
    rsi = [float('nan')] * len(closes)
    if len(closes) < period + 1:
        return rsi
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0.0)); losses.append(max(-d, 0.0))
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    rsi[period] = 100.0 if al == 0 else 100.0 - 100.0/(1 + ag/al)
    for i in range(period, len(gains)):
        ag = (ag*(period-1) + gains[i]) / period
        al = (al*(period-1) + losses[i]) / period
        rsi[i+1] = 100.0 if al == 0 else 100.0 - 100.0/(1 + ag/al)
    return rsi

def find_pivots(values, left, right):
    n = len(values)
    pl = [False]*n; ph = [False]*n
    for i in range(left, n-right):
        v = values[i]
        if math.isnan(v): continue
        nb = [j for j in range(i-left, i+right+1)
              if j != i and 0 <= j < n and not math.isnan(values[j])]
        if len(nb) < left+right: continue
        pl[i] = all(values[j] > v for j in nb)
        ph[i] = all(values[j] < v for j in nb)
    return pl, ph

def compute_atr(candles, period=14):
    n = len(candles)
    atr_vals = [float('nan')] * n
    if n < period + 1:
        return atr_vals
    trs = []
    for i in range(1, n):
        h, l, pc = candles[i]['high'], candles[i]['low'], candles[i-1]['close']
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    a = sum(trs[:period]) / period
    atr_vals[period] = a
    for i in range(period, len(trs)):
        a = (a*(period-1) + trs[i]) / period
        atr_vals[i+1] = a
    return atr_vals

def detect_divergences(candles):
    closes = [c['close'] for c in candles]
    highs  = [c['high']  for c in candles]
    lows   = [c['low']   for c in candles]
    N = len(candles)
    rsi_v = compute_rsi(closes)
    rsi_pl, rsi_ph = find_pivots(rsi_v, PIVOT_LEFT, PIVOT_RIGHT)

    pl_ev, ph_ev = [], []
    for i in range(N):
        pb = i - PIVOT_RIGHT
        if pb < 0: continue
        if rsi_pl[pb]: pl_ev.append((i, pb, lows[pb],  rsi_v[pb], highs[pb]))
        if rsi_ph[pb]: ph_ev.append((i, pb, highs[pb], rsi_v[pb], lows[pb]))

    divs = []
    for idx in range(1, len(pl_ev)):
        di, pb, lv, rv, hv = pl_ev[idx]
        pi, _, pl2, pr2, _ = pl_ev[idx-1]
        bs = di - (pi+1)
        if bs < MIN_BARS or bs > MAX_BARS: continue
        if lv < pl2 and rv > pr2:
            divs.append({'type':'bullish','det_bar':di,'trigger':hv,
                         'rsi_val':round(rv,2),'det_low':candles[di]['low'],
                         'det_high':candles[di]['high']})
    for idx in range(1, len(ph_ev)):
        di, pb, hv, rv, lv = ph_ev[idx]
        pi, _, ph2, pr2, _ = ph_ev[idx-1]
        bs = di - (pi+1)
        if bs < MIN_BARS or bs > MAX_BARS: continue
        if hv > ph2 and rv < pr2:
            divs.append({'type':'bearish','det_bar':di,'trigger':lv,
                         'rsi_val':round(rv,2),'det_low':candles[di]['low'],
                         'det_high':candles[di]['high']})
    divs.sort(key=lambda d: d['det_bar'])
    return divs

# ══════════════════════════════════════════════════════════════
# HONEST BACKTEST ENGINE
# Fixes:
#   - Entry at NEXT bar's open (no using close before bar ends)
#   - Partial-exit P&L correctly halves residual leg
#   - Trailing SL only updates AFTER bar closes (uses bar's close, then
#     apply on the NEXT bar)
#   - Intrabar SL/TGT order: when both possible, SL is assumed first
#     (conservative — never assume favorable fill)
#   - Cost deducted (COST_PTS per trade)
# ══════════════════════════════════════════════════════════════

def run_backtest(candles, side, sl_type, sl_param, tgt_type, tgt_param):
    N = len(candles)
    atr_vals = compute_atr(candles)
    all_divs = detect_divergences(candles)
    divs = [d for d in all_divs if d['type'] == ('bearish' if side=='bear' else 'bullish')]

    trades = []
    used = set()

    for div in divs:
        det = div['det_bar']
        trigger = div['trigger']
        dtype = div['type']

        # Look for trigger bar (close crosses trigger), then enter at NEXT bar open
        for i in range(det+1, min(det+ENTRY_WINDOW+1, N-1)):  # need i+1 to exist
            if i in used: continue
            c = candles[i]
            if dtype == 'bullish' and c['close'] <= trigger: continue
            if dtype == 'bearish' and c['close'] >= trigger: continue

            # FIX: entry at next bar's open
            entry_bar = i + 1
            if entry_bar >= N: break
            ep = candles[entry_bar]['open']
            direction = 'LONG' if dtype == 'bullish' else 'SHORT'
            used.add(entry_bar)

            # ── SL ──
            atr_now = atr_vals[entry_bar]
            if math.isnan(atr_now): atr_now = 40.0

            if sl_type == 'fixed':
                sl_pts = float(sl_param)
            elif sl_type == 'signal_candle':
                candle_range = div['det_high'] - div['det_low']
                sl_pts = max(candle_range, float(sl_param))
            else:  # atr
                sl_pts = max((sl_param / 10.0) * atr_now, 15.0)

            sl_pts = round(sl_pts, 2)
            sl_lvl = round(ep - sl_pts, 2) if direction == 'LONG' else round(ep + sl_pts, 2)

            # ── TGT ──
            if tgt_type in ('fixed', 'partial'):
                tgt_pts = float(tgt_param)
            elif tgt_type == 'trailing':
                tgt_pts = sl_pts * 1.5  # trigger trail at 1.5R
            else:
                tgt_pts = float(tgt_param)
            tgt_lvl = round(ep + tgt_pts, 2) if direction == 'LONG' else round(ep - tgt_pts, 2)

            # ── Simulate trade (no look-ahead) ──
            outcome = None
            exit_price = None
            exit_bar_idx = None
            partial_done = False
            trail_sl = sl_lvl
            MAX_HOLD = 200

            for j in range(entry_bar + 1, min(entry_bar + 1 + MAX_HOLD, N)):
                bar = candles[j]
                bars_held = j - entry_bar

                # ─── Conservative intrabar fill order: SL first ───
                if direction == 'LONG':
                    active_sl = trail_sl if tgt_type == 'trailing' else sl_lvl

                    # Check SL first (conservative)
                    if bar['low'] <= active_sl:
                        if partial_done:
                            # Half already at TGT, half exits at trail (could be BE or higher)
                            outcome = 'PARTIAL_TRAIL'
                            exit_price = active_sl
                        else:
                            outcome = 'SL'
                            exit_price = active_sl
                        exit_bar_idx = j; break

                    # Then check TGT
                    if not partial_done and bar['high'] >= tgt_lvl:
                        if tgt_type == 'partial':
                            partial_done = True
                            trail_sl = ep   # move stop to BE for residual
                            # Continue to next bar to manage residual
                        else:
                            outcome = 'TGT'
                            exit_price = tgt_lvl
                            exit_bar_idx = j; break

                    # Trailing logic (end-of-bar update — use this bar's close
                    # to set trail FOR NEXT BAR's SL check)
                    if tgt_type == 'trailing' and bar['close'] >= ep + tgt_pts:
                        new_trail = bar['close'] - tgt_param
                        if new_trail > trail_sl:
                            trail_sl = new_trail

                else:  # SHORT
                    active_sl = trail_sl if tgt_type == 'trailing' else sl_lvl

                    # SL first
                    if bar['high'] >= active_sl:
                        if partial_done:
                            outcome = 'PARTIAL_TRAIL'
                            exit_price = active_sl
                        else:
                            outcome = 'SL'
                            exit_price = active_sl
                        exit_bar_idx = j; break

                    # TGT
                    if not partial_done and bar['low'] <= tgt_lvl:
                        if tgt_type == 'partial':
                            partial_done = True
                            trail_sl = ep
                        else:
                            outcome = 'TGT'
                            exit_price = tgt_lvl
                            exit_bar_idx = j; break

                    if tgt_type == 'trailing' and bar['close'] <= ep - tgt_pts:
                        new_trail = bar['close'] + tgt_param
                        if new_trail < trail_sl:
                            trail_sl = new_trail

            # Force-close at max hold
            if exit_bar_idx is None:
                j = min(entry_bar + MAX_HOLD, N-1)
                exit_price = candles[j]['close']
                exit_bar_idx = j
                pnl_chk = (exit_price-ep) if direction=='LONG' else (ep-exit_price)
                outcome = 'TIMEOUT_W' if pnl_chk > 0 else 'TIMEOUT_L'

            bars_held = exit_bar_idx - entry_bar

            # ── PnL (FIXED) ──
            residual_move = (exit_price - ep) if direction == 'LONG' else (ep - exit_price)

            if tgt_type == 'partial' and partial_done:
                # 50% took TGT at tgt_pts, 50% exited at residual_move
                pnl_pts = 0.5 * tgt_pts + 0.5 * residual_move
            else:
                pnl_pts = residual_move

            # Deduct cost (round-trip)
            pnl_pts -= COST_PTS

            trades.append({
                'direction': direction, 'entry_ts': candles[entry_bar]['ts'],
                'entry_price': round(ep,2), 'exit_price': round(exit_price,2),
                'exit_ts': candles[exit_bar_idx]['ts'],
                'sl_pts': sl_pts, 'tgt_pts': tgt_pts,
                'outcome': 'TGT' if pnl_pts > 0 else 'SL',
                'raw_outcome': outcome,
                'pnl_pts': round(pnl_pts, 2),
                'bars_held': bars_held, 'rsi_at_div': div['rsi_val'],
                'partial': partial_done,
            })
            break  # one trade per divergence

    return trades

def stats(trades, sl_type, sl_param, tgt_type, tgt_param):
    if not trades:
        return {'n':0, 'win_rate':0, 'pf':0, 'net_pts':0, 'net_rs':0,
                'max_dd':0, 'max_cl':0, 'avg_bars':0,
                'sl_type':sl_type, 'sl_param':sl_param,
                'tgt_type':tgt_type, 'tgt_param':tgt_param, 'trades':[]}
    wins   = [t for t in trades if t['pnl_pts'] > 0]
    losses = [t for t in trades if t['pnl_pts'] <= 0]
    gp = sum(t['pnl_pts'] for t in wins)
    gl = abs(sum(t['pnl_pts'] for t in losses))
    net = sum(t['pnl_pts'] for t in trades)

    eq = peak = mdd = 0
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
            ts = t['entry_ts']
            k = ts.strftime('%Y-%m') if hasattr(ts,'strftime') else str(ts)[:7]
            monthly.setdefault(k, {'pnl':0,'n':0,'wins':0})
            monthly[k]['pnl']  += t['pnl_pts']
            monthly[k]['n']    += 1
            monthly[k]['wins'] += 1 if t['pnl_pts'] > 0 else 0
        except: pass

    return {
        'n': len(trades), 'n_wins': len(wins), 'n_losses': len(losses),
        'win_rate': round(len(wins)/len(trades)*100, 1),
        'pf': round(gp/gl, 2) if gl > 0 else 999,
        'net_pts': round(net, 1),
        'net_rs': round(net * LOT, 0),
        'gross_profit': round(gp, 1), 'gross_loss': round(gl, 1),
        'avg_win': round(gp/len(wins), 1) if wins else 0,
        'avg_loss': round(gl/len(losses), 1) if losses else 0,
        'max_dd': round(mdd, 1), 'max_cl': mcl,
        'avg_bars': round(sum(t['bars_held'] for t in trades)/len(trades), 1),
        'sl_type': sl_type, 'sl_param': sl_param,
        'tgt_type': tgt_type, 'tgt_param': tgt_param,
        'monthly': monthly, 'trades': trades,
    }


def split_trades(trades, split_ts):
    """Split trades into in-sample (before split) and out-of-sample (after)."""
    in_s, out_s = [], []
    for t in trades:
        ts = t['entry_ts']
        if hasattr(ts, 'tz_convert'):
            ts_check = ts
        else:
            ts_check = pd.Timestamp(ts)
        if ts_check < split_ts:
            in_s.append(t)
        else:
            out_s.append(t)
    return in_s, out_s


# ══════════════════════════════════════════════════════════════
# DATA LOADING
# ══════════════════════════════════════════════════════════════

def _df_to_candles(df):
    df = df.sort_values('timestamp').reset_index(drop=True)
    df['ts_ist'] = df['timestamp'].dt.tz_convert('Asia/Kolkata')
    df = df[
        (df['ts_ist'].dt.time >= pd.Timestamp('09:15').time()) &
        (df['ts_ist'].dt.time <= pd.Timestamp('15:30').time())
    ].reset_index(drop=True)
    return [{'ts':r['ts_ist'],'open':float(r['open']),'high':float(r['high']),
             'low':float(r['low']),'close':float(r['close']),'volume':int(r['volume'])}
            for _,r in df.iterrows()]

def load_candles(tf):
    if tf == '5m':
        df1 = pd.read_parquet(DATA_1M)
        df1 = df1.sort_values('timestamp').reset_index(drop=True)
        df1['ts_ist'] = df1['timestamp'].dt.tz_convert('Asia/Kolkata')
        df1 = df1[
            (df1['ts_ist'].dt.time >= pd.Timestamp('09:15').time()) &
            (df1['ts_ist'].dt.time <= pd.Timestamp('15:30').time())
        ].reset_index(drop=True)
        df1['bar_key'] = df1['ts_ist'].apply(
            lambda t: t.replace(minute=(t.minute//5)*5, second=0, microsecond=0))
        g = df1.groupby('bar_key').agg(
            open=('open','first'), high=('high','max'),
            low=('low','min'), close=('close','last'), volume=('volume','sum')
        ).reset_index().rename(columns={'bar_key':'ts_ist'})
        return [{'ts':r['ts_ist'],'open':float(r['open']),'high':float(r['high']),
                 'low':float(r['low']),'close':float(r['close']),'volume':int(r['volume'])}
                for _,r in g.iterrows()]
    elif tf == '15m':
        return _df_to_candles(pd.read_parquet(DATA_15))
    elif tf == '30m':
        return _df_to_candles(pd.read_parquet(DATA_30))
    raise ValueError(f"Unknown tf: {tf}")

# ══════════════════════════════════════════════════════════════
# REPORT
# ══════════════════════════════════════════════════════════════

def variant_label(r):
    sl = {'fixed': f"SL={int(r['sl_param'])}",
          'signal_candle': f"SL=SignalCandle(min{int(r['sl_param'])})",
          'atr': f"SL={r['sl_param']/10:.1f}xATR"}[r['sl_type']]
    tg = {'fixed': f"TGT={int(r['tgt_param'])}",
          'trailing': f"TGT=Trail({int(r['tgt_param'])})",
          'partial': f"TGT=Partial50@{int(r['tgt_param'])}"}[r['tgt_type']]
    return f"{sl} | {tg}"


def write_report(results, today):
    lines = []
    lines.append(f"# RSI Divergence — HONEST Walk-Forward Backtest")
    lines.append(f"**Date**: {today}  |  **Symbol**: MIDCPNIFTY  |  **Lot**: {LOT}")
    lines.append(f"**Cost**: ₹{COST_RS}/trade ({COST_PTS:.3f} pts)  |  **Split**: {SPLIT_DATE.date()}")
    lines.append("")
    lines.append("## Methodology")
    lines.append("")
    lines.append("1. **In-sample (IS)**: 2022-03 to 2024-12 — pick best variant by PF here")
    lines.append("2. **Out-of-sample (OOS)**: 2025-01 to 2026-05 — test that variant once, no peeking")
    lines.append("3. **Entry**: NEXT bar's open (no using close before bar ends)")
    lines.append("4. **SL/TGT**: SL checked first intrabar (conservative)")
    lines.append("5. **Costs**: 0.33 pts deducted per round-trip trade")
    lines.append("6. **Partial-exit**: P&L correctly = 0.5×TGT + 0.5×residual")
    lines.append("")

    # IS leaderboard per TF/side
    for tf in ['5m', '15m', '30m']:
        for side in ['bear', 'bull']:
            key = f"{tf}_{side}"
            data = results.get(key)
            if not data: continue

            lines.append(f"## {tf.upper()} — {'BEAR' if side=='bear' else 'BULL'}")
            lines.append("")
            lines.append("### In-Sample Leaderboard (2022-03 → 2024-12)")
            lines.append("")
            lines.append("| Variant | N | WR% | PF | Net pts | Net ₹ | MaxDD | MaxCL |")
            lines.append("|---------|---|-----|----|---------|-------|-------|-------|")

            sorted_is = sorted(data['is_results'], key=lambda r: r['pf'], reverse=True)
            for r in sorted_is:
                if r['n'] < 10: continue
                lines.append(
                    f"| {variant_label(r)} | {r['n']} | {r['win_rate']}% | "
                    f"**{r['pf']}** | {r['net_pts']:+.0f} | ₹{r['net_rs']:+,.0f} | "
                    f"{r['max_dd']:.0f} | {r['max_cl']} |"
                )
            lines.append("")

            # OOS test of best
            best_is = data['best_is']
            best_oos = data['best_oos']
            if best_is and best_oos:
                lines.append(f"### Out-of-Sample Test (best IS variant: **{variant_label(best_is)}**)")
                lines.append("")
                lines.append(f"| Window | N | WR% | PF | Net pts | Net ₹/lot | MaxDD | MaxCL |")
                lines.append(f"|--------|---|-----|----|---------|-----------|-------|-------|")
                lines.append(
                    f"| **In-Sample**  | {best_is['n']} | {best_is['win_rate']}% | "
                    f"**{best_is['pf']}** | {best_is['net_pts']:+.0f} | ₹{best_is['net_rs']:+,.0f} | "
                    f"{best_is['max_dd']:.0f} | {best_is['max_cl']} |"
                )
                lines.append(
                    f"| **Out-of-Sample** | {best_oos['n']} | {best_oos['win_rate']}% | "
                    f"**{best_oos['pf']}** | {best_oos['net_pts']:+.0f} | ₹{best_oos['net_rs']:+,.0f} | "
                    f"{best_oos['max_dd']:.0f} | {best_oos['max_cl']} |"
                )
                lines.append("")

                # Verdict
                degradation = (best_oos['pf'] / best_is['pf'] - 1) * 100 if best_is['pf'] > 0 else 0
                if best_oos['pf'] >= 1.2 and best_oos['n'] >= 20:
                    verdict = "✓ **OOS holds up** — strategy has genuine edge on unseen data"
                elif best_oos['pf'] >= 1.0:
                    verdict = "⚠ **OOS marginal** — barely break-even after costs"
                else:
                    verdict = "✗ **OOS fails** — IS result was likely curve-fit"
                lines.append(f"**Verdict**: {verdict}")
                lines.append(f"**PF degradation**: {degradation:+.1f}%")
                lines.append("")

                # OOS monthly
                m = best_oos.get('monthly', {})
                if m:
                    lines.append("#### OOS Monthly Breakdown")
                    lines.append("")
                    lines.append("| Month | N | WR% | PnL pts | PnL ₹ |")
                    lines.append("|-------|---|-----|---------|-------|")
                    for k in sorted(m.keys()):
                        rec = m[k]
                        wr = round(rec['wins']/rec['n']*100, 0) if rec['n'] else 0
                        lines.append(f"| {k} | {rec['n']} | {wr:.0f}% | {rec['pnl']:+.0f} | ₹{rec['pnl']*LOT:+,.0f} |")
                    lines.append("")

    # Final summary
    lines.append("## Final Summary — OOS Performance Only (the honest number)")
    lines.append("")
    lines.append("| TF | Side | Best IS Variant | OOS Trades | OOS WR% | OOS PF | OOS Net ₹ |")
    lines.append("|----|------|-----------------|------------|---------|--------|-----------|")
    for tf in ['5m', '15m', '30m']:
        for side in ['bear', 'bull']:
            key = f"{tf}_{side}"
            data = results.get(key)
            if not data or not data.get('best_oos'): continue
            best_is = data['best_is']; best_oos = data['best_oos']
            lines.append(
                f"| {tf} | {'Bear' if side=='bear' else 'Bull'} | "
                f"{variant_label(best_is)} | {best_oos['n']} | "
                f"{best_oos['win_rate']}% | **{best_oos['pf']}** | "
                f"₹{best_oos['net_rs']:+,.0f} |"
            )
    lines.append("")

    out = OUT_DIR / f"backtest_rsi_div_HONEST_MIDCPNIFTY_{today}.md"
    out.write_text("\n".join(lines))
    return out

# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════

def main():
    today = datetime.now().strftime('%Y-%m-%d')
    results = {}

    for tf in ['5m', '15m', '30m']:
        print(f"\nLoading {tf} data...", end=" ", flush=True)
        try:
            candles = load_candles(tf)
        except Exception as e:
            print(f"ERROR: {e}")
            continue
        print(f"{len(candles)} bars  ({candles[0]['ts'].date()} → {candles[-1]['ts'].date()})")

        for side in ['bear', 'bull']:
            key = f"{tf}_{side}"
            print(f"\n  ── {tf} {side} ──")

            is_results = []
            for idx, (sl_t, sl_p, tg_t, tg_p) in enumerate(SWEEP, 1):
                trades = run_backtest(candles, side, sl_t, sl_p, tg_t, tg_p)
                is_trades, _ = split_trades(trades, SPLIT_DATE)
                is_stats = stats(is_trades, sl_t, sl_p, tg_t, tg_p)
                is_results.append(is_stats)
                print(f"    [{idx:02d}/{len(SWEEP)}] {variant_label(is_stats)} → "
                      f"IS: N={is_stats['n']} WR={is_stats['win_rate']}% PF={is_stats['pf']}")

            valid = [r for r in is_results if r['n'] >= 10]
            if not valid:
                results[key] = {'is_results': is_results, 'best_is': None, 'best_oos': None}
                print(f"    No IS variants with ≥10 trades")
                continue

            # Pick best by IS PF
            best_is = max(valid, key=lambda r: r['pf'])
            print(f"    BEST IS: {variant_label(best_is)}  PF={best_is['pf']} WR={best_is['win_rate']}%")

            # Run that variant on full data, then split out OOS
            full_trades = run_backtest(candles, side,
                                        best_is['sl_type'], best_is['sl_param'],
                                        best_is['tgt_type'], best_is['tgt_param'])
            _, oos_trades = split_trades(full_trades, SPLIT_DATE)
            best_oos = stats(oos_trades, best_is['sl_type'], best_is['sl_param'],
                             best_is['tgt_type'], best_is['tgt_param'])
            print(f"    OOS:      N={best_oos['n']} WR={best_oos['win_rate']}% "
                  f"PF={best_oos['pf']} Net=₹{best_oos['net_rs']:+,.0f}")

            results[key] = {
                'is_results': is_results,
                'best_is': best_is,
                'best_oos': best_oos,
            }

    out = write_report(results, today)
    print(f"\n\nReport saved → {out}")

    # Console final summary
    print("\n" + "═"*70)
    print("  FINAL OOS SUMMARY (the honest number)")
    print("═"*70)
    print(f"  {'TF':<5}{'Side':<7}{'Variant':<35}{'N':>4}{'WR%':>6}{'PF':>6}{'Net ₹':>12}")
    print("  " + "─"*68)
    for tf in ['5m', '15m', '30m']:
        for side in ['bear', 'bull']:
            data = results.get(f"{tf}_{side}")
            if not data or not data.get('best_oos'): continue
            b = data['best_oos']
            print(f"  {tf:<5}{side:<7}{variant_label(b):<35}{b['n']:>4}"
                  f"{b['win_rate']:>5}%{b['pf']:>6}  ₹{b['net_rs']:>+9,.0f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
