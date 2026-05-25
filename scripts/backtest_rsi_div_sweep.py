#!/usr/bin/env python3
"""
backtest_rsi_div_sweep.py
=========================
Multi-variant RSI Divergence backtest on MIDCPNIFTY.
Timeframes : 5m, 15m, 30m  (aggregated from 1m parquet)
SL variants : fixed, signal-candle, ATR-based
TGT variants: fixed, trailing (ATR step), partial-exit
Sides       : bear (short) on all TFs + bull (long) on all TFs

Output: data/research/backtest_rsi_div_sweep_MIDCPNIFTY_<date>.md
"""

import argparse, math, os, sys
from datetime import datetime, timezone, timedelta
from itertools import product
from pathlib import Path
import pandas as pd

ROOT    = Path(__file__).parent.parent
DATA_15 = Path("/Users/amitkumar/Desktop/nifty-analyzer/data/index_spot/MIDCPNIFTY_15m_5y.parquet")
DATA_30 = Path("/Users/amitkumar/Desktop/nifty-analyzer/data/index_spot/MIDCPNIFTY_30m_5y.parquet")
DATA_1M = Path("/Users/amitkumar/Desktop/nifty-analyzer/data/index_spot/MIDCPNIFTY_1m_5y.parquet")
OUT_DIR = ROOT / "data" / "research"
OUT_DIR.mkdir(parents=True, exist_ok=True)

IST = timezone(timedelta(hours=5, minutes=30))

# ── RSI / Pivot params ─────────────────────────────────────────
RSI_LEN     = 14
PIVOT_LEFT  = 5
PIVOT_RIGHT = 5
MIN_BARS    = 5
MAX_BARS    = 60
ENTRY_WINDOW = 10

# ── Sweep grid ─────────────────────────────────────────────────
# (sl_type, sl_param, tgt_type, tgt_param)
# sl_type  : 'fixed' | 'signal_candle' | 'atr'
# tgt_type : 'fixed' | 'trailing_atr' | 'partial'
SWEEP = [
    # Fixed SL / Fixed TGT  (original config + variations)
    ('fixed', 30,  'fixed', 60),
    ('fixed', 30,  'fixed', 90),
    ('fixed', 30,  'fixed', 120),
    ('fixed', 40,  'fixed', 60),
    ('fixed', 40,  'fixed', 100),   # original 15m bear
    ('fixed', 40,  'fixed', 120),
    ('fixed', 50,  'fixed', 100),
    ('fixed', 50,  'fixed', 150),
    ('fixed', 60,  'fixed', 120),
    ('fixed', 60,  'fixed', 180),
    # Signal-candle SL (SL = entry ± candle range, min 20 pts)
    ('signal_candle', 20, 'fixed', 80),
    ('signal_candle', 20, 'fixed', 120),
    ('signal_candle', 20, 'fixed', 160),
    # ATR-based SL (sl_param = ATR multiplier × 10, e.g. 15 = 1.5×ATR)
    ('atr', 10, 'fixed', 80),       # 1.0×ATR SL, fixed 80 TGT
    ('atr', 15, 'fixed', 100),      # 1.5×ATR SL, fixed 100 TGT
    ('atr', 20, 'fixed', 120),      # 2.0×ATR SL, fixed 120 TGT
    # Trailing TGT (trail step = tgt_param pts, lock-in after 1R)
    ('fixed', 30,  'trailing_atr', 20),
    ('fixed', 40,  'trailing_atr', 25),
    ('fixed', 50,  'trailing_atr', 30),
    ('atr',   15,  'trailing_atr', 25),
    # Partial exit: 50% at TGT1, trail rest
    ('fixed', 40,  'partial', 80),
    ('fixed', 40,  'partial', 100),
    ('fixed', 50,  'partial', 100),
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
    """ATR(14) for each bar — returns list same length as candles."""
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
            divs.append({'type':'bullish','det_bar':di,'pivot_bar':pb,
                         'ts':candles[di]['ts'],'trigger':hv,
                         'rsi_val':round(rv,2),'det_low':candles[di]['low'],
                         'det_high':candles[di]['high']})
    for idx in range(1, len(ph_ev)):
        di, pb, hv, rv, lv = ph_ev[idx]
        pi, _, ph2, pr2, _ = ph_ev[idx-1]
        bs = di - (pi+1)
        if bs < MIN_BARS or bs > MAX_BARS: continue
        if hv > ph2 and rv < pr2:
            divs.append({'type':'bearish','det_bar':di,'pivot_bar':pb,
                         'ts':candles[di]['ts'],'trigger':lv,
                         'rsi_val':round(rv,2),'det_low':candles[di]['low'],
                         'det_high':candles[di]['high']})
    divs.sort(key=lambda d: d['det_bar'])
    return divs

# ══════════════════════════════════════════════════════════════
# BACKTEST ENGINE  (supports all SL/TGT variants)
# ══════════════════════════════════════════════════════════════

def run_backtest(candles, side, sl_type, sl_param, tgt_type, tgt_param):
    """
    side      : 'bear' | 'bull'
    sl_type   : 'fixed' | 'signal_candle' | 'atr'
    sl_param  : pts (fixed) | min_pts (signal_candle) | mult×10 (atr)
    tgt_type  : 'fixed' | 'trailing_atr' | 'partial'
    tgt_param : pts (fixed/partial TGT1) | trail_step pts (trailing)
    """
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

        for i in range(det+1, min(det+ENTRY_WINDOW+1, N)):
            if i in used: continue
            c = candles[i]
            if dtype == 'bullish' and c['close'] <= trigger: continue
            if dtype == 'bearish' and c['close'] >= trigger: continue

            ep = c['close']
            direction = 'LONG' if dtype == 'bullish' else 'SHORT'
            used.add(i)

            # ── Compute SL ──────────────────────────────────
            atr_now = atr_vals[i] if not math.isnan(atr_vals[i] or float('nan')) else 40.0
            if sl_type == 'fixed':
                sl_pts = sl_param
            elif sl_type == 'signal_candle':
                candle_range = div['det_high'] - div['det_low']
                sl_pts = max(candle_range, sl_param)
            else:  # atr
                sl_pts = max((sl_param / 10.0) * atr_now, 15.0)

            sl_pts = round(sl_pts, 2)
            sl_lvl = round(ep - sl_pts, 2) if direction == 'LONG' else round(ep + sl_pts, 2)

            # ── Compute initial TGT ─────────────────────────
            if tgt_type in ('fixed', 'partial'):
                tgt1_pts = tgt_param
            elif tgt_type == 'trailing_atr':
                tgt1_pts = sl_pts * 1.5   # first lock-in at 1.5R
            else:
                tgt1_pts = tgt_param

            tgt1_lvl = round(ep + tgt1_pts, 2) if direction == 'LONG' else round(ep - tgt1_pts, 2)

            # ── Simulate trade ──────────────────────────────
            outcome = 'OPEN'
            exit_price = None; exit_bar = None; bars_held = 0
            partial_done = False; partial_pnl = 0.0
            trail_sl = sl_lvl   # for trailing

            MAX_HOLD = 200
            for j in range(i+1, min(i+MAX_HOLD+1, N)):
                bar = candles[j]
                bars_held = j - i

                if direction == 'LONG':
                    # Trailing SL: ratchet up after each tgt_param move
                    if tgt_type == 'trailing_atr' and bar['high'] >= ep + tgt1_pts:
                        # Lock in breakeven + trail by tgt_param pts
                        new_trail = bar['close'] - tgt_param
                        if new_trail > trail_sl:
                            trail_sl = new_trail
                    active_sl = trail_sl if tgt_type == 'trailing_atr' else sl_lvl

                    # Partial: take 50% at TGT1, trail rest
                    if tgt_type == 'partial' and not partial_done:
                        if bar['high'] >= tgt1_lvl:
                            partial_pnl = tgt1_pts * 0.5
                            partial_done = True
                            trail_sl = ep  # move SL to breakeven
                            continue

                    if bar['low'] <= active_sl:
                        outcome = 'SL'
                        exit_price = active_sl; exit_bar = j; break
                    if bar['high'] >= tgt1_lvl and not partial_done:
                        outcome = 'TGT'
                        exit_price = tgt1_lvl; exit_bar = j; break
                    if partial_done and bar['low'] <= trail_sl:
                        outcome = 'TGT'  # partial win
                        exit_price = trail_sl; exit_bar = j; break

                else:  # SHORT
                    if tgt_type == 'trailing_atr' and bar['low'] <= ep - tgt1_pts:
                        new_trail = bar['close'] + tgt_param
                        if new_trail < trail_sl:
                            trail_sl = new_trail
                    active_sl = trail_sl if tgt_type == 'trailing_atr' else sl_lvl

                    if tgt_type == 'partial' and not partial_done:
                        if bar['low'] <= tgt1_lvl:
                            partial_pnl = tgt1_pts * 0.5
                            partial_done = True
                            trail_sl = ep
                            continue

                    if bar['high'] >= active_sl:
                        outcome = 'SL'
                        exit_price = active_sl; exit_bar = j; break
                    if bar['low'] <= tgt1_lvl and not partial_done:
                        outcome = 'TGT'
                        exit_price = tgt1_lvl; exit_bar = j; break
                    if partial_done and bar['high'] >= trail_sl:
                        outcome = 'TGT'
                        exit_price = trail_sl; exit_bar = j; break

            # Force-close at max hold
            if exit_bar is None:
                j = min(i+MAX_HOLD, N-1)
                exit_price = candles[j]['close']
                exit_bar = j; bars_held = j - i
                pnl_chk = (exit_price-ep) if direction=='LONG' else (ep-exit_price)
                outcome = 'TGT' if pnl_chk > 0 else 'SL'

            raw_pnl = (exit_price-ep) if direction=='LONG' else (ep-exit_price)
            pnl_pts = round(raw_pnl + partial_pnl, 2)

            trades.append({
                'direction': direction, 'entry_ts': c['ts'],
                'entry_price': round(ep,2), 'exit_price': round(exit_price,2),
                'sl_pts': sl_pts, 'tgt_pts': tgt1_pts,
                'outcome': outcome, 'pnl_pts': pnl_pts,
                'bars_held': bars_held, 'rsi_at_div': div['rsi_val'],
                'partial': partial_done,
            })
            break  # one trade per divergence

    return _stats(trades, sl_type, sl_param, tgt_type, tgt_param)

def _stats(trades, sl_type, sl_param, tgt_type, tgt_param):
    if not trades:
        return {'n':0,'win_rate':0,'pf':0,'net_pts':0,'max_dd':0,
                'max_cl':0,'avg_bars':0,'sl_type':sl_type,'sl_param':sl_param,
                'tgt_type':tgt_type,'tgt_param':tgt_param,'trades':[]}
    wins   = [t for t in trades if t['outcome']=='TGT']
    losses = [t for t in trades if t['outcome']=='SL']
    gp = sum(t['pnl_pts'] for t in wins)
    gl = abs(sum(t['pnl_pts'] for t in losses))
    net = sum(t['pnl_pts'] for t in trades)

    # Max drawdown
    eq=0; peak=0; mdd=0
    for t in trades:
        eq += t['pnl_pts']
        peak = max(peak, eq)
        mdd = max(mdd, peak-eq)

    # Max consecutive losses
    mcl=0; cl=0
    for t in trades:
        if t['outcome']=='SL': cl+=1; mcl=max(mcl,cl)
        else: cl=0

    # Monthly
    monthly={}
    for t in trades:
        try:
            ts = t['entry_ts']
            k = ts.strftime('%Y-%m') if hasattr(ts,'strftime') else str(ts)[:7]
            monthly.setdefault(k,{'pnl':0,'n':0,'wins':0})
            monthly[k]['pnl'] += t['pnl_pts']
            monthly[k]['n']   += 1
            monthly[k]['wins']+= 1 if t['outcome']=='TGT' else 0
        except: pass

    avg_sl  = sum(t['sl_pts']  for t in trades)/len(trades)
    avg_tgt = sum(t['tgt_pts'] for t in trades)/len(trades)

    return {
        'n': len(trades), 'n_wins': len(wins), 'n_losses': len(losses),
        'win_rate': round(len(wins)/len(trades)*100,1),
        'pf': round(gp/gl,2) if gl>0 else 999,
        'net_pts': round(net,1),
        'gross_profit': round(gp,1), 'gross_loss': round(gl,1),
        'avg_win': round(gp/len(wins),1) if wins else 0,
        'avg_loss': round(gl/len(losses),1) if losses else 0,
        'avg_sl': round(avg_sl,1), 'avg_tgt': round(avg_tgt,1),
        'rr': round(avg_tgt/avg_sl,2) if avg_sl>0 else 0,
        'max_dd': round(mdd,1), 'max_cl': mcl,
        'avg_bars': round(sum(t['bars_held'] for t in trades)/len(trades),1),
        'net_rs': round(net*150,0),
        'sl_type': sl_type, 'sl_param': sl_param,
        'tgt_type': tgt_type, 'tgt_param': tgt_param,
        'monthly': monthly, 'trades': trades,
    }

# ══════════════════════════════════════════════════════════════
# DATA LOADING  (parquet → candle dicts, 5m aggregated from 1m)
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
        # Aggregate 1m → 5m
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
    else:
        raise ValueError(f"Unknown tf: {tf}")

# ══════════════════════════════════════════════════════════════
# REPORT
# ══════════════════════════════════════════════════════════════

def variant_label(r):
    sl_lbl = {
        'fixed':        f"SL={r['sl_param']}pts",
        'signal_candle':f"SL=SignalCandle(min{r['sl_param']})",
        'atr':          f"SL={r['sl_param']/10:.1f}×ATR",
    }[r['sl_type']]
    tgt_lbl = {
        'fixed':        f"TGT={r['tgt_param']}pts",
        'trailing_atr': f"TGT=Trail({r['tgt_param']}step)",
        'partial':      f"TGT=Partial50%@{r['tgt_param']}",
    }[r['tgt_type']]
    return f"{sl_lbl} | {tgt_lbl}"

def write_report(all_results, today):
    lines = []
    lines.append(f"# RSI Divergence Sweep Backtest — MIDCPNIFTY")
    lines.append(f"**Date**: {today}  |  **Data**: 5-year spot  |  **Lot**: 150  |  **Sides**: Bear + Bull")
    lines.append(f"**TFs**: 5m, 15m, 30m  |  **Variants**: {len(SWEEP)} SL/TGT combos each")
    lines.append("")

    for tf in ['5m','15m','30m']:
        for side in ['bear','bull']:
            key = f"{tf}_{side}"
            results = all_results.get(key, [])
            if not results: continue

            lines.append(f"## {tf.upper()} — {'BEAR (Short)' if side=='bear' else 'BULL (Long)'}")
            lines.append("")
            lines.append("| Variant | N | WR% | PF | Net pts | Net ₹/lot | MaxDD | MaxCL | AvgBars | RR |")
            lines.append("|---------|---|-----|----|---------|-----------|-------|-------|---------|-----|")

            # Sort by profit factor descending
            results_sorted = sorted(results, key=lambda r: r['pf'], reverse=True)
            for r in results_sorted:
                if r['n'] < 10: continue  # skip too-few-trades variants
                flag = " ★" if r['pf'] >= 1.5 and r['win_rate'] >= 35 else ""
                lines.append(
                    f"| {variant_label(r)}{flag} | {r['n']} | {r['win_rate']}% | "
                    f"**{r['pf']}** | {r['net_pts']:+.0f} | ₹{r['net_rs']:+,.0f} | "
                    f"{r['max_dd']:.0f} | {r['max_cl']} | {r['avg_bars']} | 1:{r['rr']} |"
                )
            lines.append("")

            # Best variant deep-dive
            best = max((r for r in results if r['n'] >= 10), key=lambda r: r['pf'], default=None)
            if best:
                lines.append(f"### Best Variant: {variant_label(best)}")
                lines.append("")
                lines.append(f"| Metric | Value |")
                lines.append(f"|--------|-------|")
                lines.append(f"| Trades | {best['n']} ({best['n_wins']}W / {best['n_losses']}L) |")
                lines.append(f"| Win Rate | **{best['win_rate']}%** |")
                lines.append(f"| Profit Factor | **{best['pf']}** |")
                lines.append(f"| Net Points | **{best['net_pts']:+.1f}** |")
                lines.append(f"| Net ₹ (1 lot) | **₹{best['net_rs']:+,.0f}** |")
                lines.append(f"| Avg Win | +{best['avg_win']:.1f} pts |")
                lines.append(f"| Avg Loss | -{best['avg_loss']:.1f} pts |")
                lines.append(f"| Max Drawdown | {best['max_dd']:.1f} pts |")
                lines.append(f"| Max Consec Losses | {best['max_cl']} |")
                lines.append(f"| Avg Bars Held | {best['avg_bars']} |")
                lines.append("")

                # Monthly for best
                monthly = best.get('monthly', {})
                if monthly:
                    lines.append("#### Monthly Breakdown")
                    lines.append("")
                    lines.append("| Month | N | WR% | PnL pts | PnL ₹ |")
                    lines.append("|-------|---|-----|---------|-------|")
                    for k in sorted(monthly.keys()):
                        m = monthly[k]
                        wr = round(m['wins']/m['n']*100,0) if m['n'] else 0
                        lines.append(f"| {k} | {m['n']} | {wr:.0f}% | {m['pnl']:+.0f} | ₹{m['pnl']*150:+,.0f} |")
                    lines.append("")

    # Cross-TF summary table
    lines.append("## Cross-TF Best Variants Summary")
    lines.append("")
    lines.append("| TF | Side | Best Variant | N | WR% | PF | Net ₹/lot | MaxDD pts |")
    lines.append("|----|------|-------------|---|-----|----|-----------|-----------|")
    for tf in ['5m','15m','30m']:
        for side in ['bear','bull']:
            key = f"{tf}_{side}"
            results = all_results.get(key, [])
            valid = [r for r in results if r['n'] >= 10]
            if not valid: continue
            best = max(valid, key=lambda r: r['pf'])
            lines.append(
                f"| {tf} | {'Bear' if side=='bear' else 'Bull'} | {variant_label(best)} | "
                f"{best['n']} | {best['win_rate']}% | **{best['pf']}** | "
                f"₹{best['net_rs']:+,.0f} | {best['max_dd']:.0f} |"
            )
    lines.append("")

    out = OUT_DIR / f"backtest_rsi_div_sweep_MIDCPNIFTY_{today}.md"
    out.write_text("\n".join(lines))
    return out

# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════

def main():
    today = datetime.now().strftime('%Y-%m-%d')
    all_results = {}

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
            all_results[key] = []
            total = len(SWEEP)
            for idx, (sl_type, sl_param, tgt_type, tgt_param) in enumerate(SWEEP, 1):
                print(f"  [{idx:02d}/{total}] {tf} {side}  {sl_type}/{sl_param} → {tgt_type}/{tgt_param}",
                      end="\r", flush=True)
                r = run_backtest(candles, side, sl_type, sl_param, tgt_type, tgt_param)
                all_results[key].append(r)

            # Print quick summary for this TF/side
            valid = [r for r in all_results[key] if r['n'] >= 10]
            if valid:
                best = max(valid, key=lambda r: r['pf'])
                print(f"  {tf} {side}: {len(valid)} valid variants | "
                      f"Best PF={best['pf']} WR={best['win_rate']}% "
                      f"Net=₹{best['net_rs']:+,.0f}  [{variant_label(best)}]          ")
            else:
                print(f"  {tf} {side}: no variants with ≥10 trades          ")

    out = write_report(all_results, today)
    print(f"\nReport saved → {out}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
