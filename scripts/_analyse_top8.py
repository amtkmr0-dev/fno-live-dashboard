"""Analyse the top 4 bullish + top 4 bearish (live OI thesis) with chart features.

Flow:
  1. Read /tmp/qt_top8.json (built by _pick_top8.py)
  2. For each name: load cached daily OHLC (or fetch if missing)
  3. Compute chart features (RSI, ATR, BB, pivots, weekly H/L, FVGs, trend)
  4. Compute chart_bias and trade_levels
  5. Print a per-stock report and a final ranked tabular summary
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import historical_data  # noqa: E402
import chart_features   # noqa: E402

DB = ROOT / "data" / "quantra_history.db"


def load_or_fetch(symbol: str, days: int = 90):
    rows = historical_data.load_cached(symbol, days=days, db_path=DB)
    if len(rows) >= 30:
        return rows, "cache"
    # Need to fetch. Resolve token, instrument key, then call API.
    token = historical_data._resolve_token()
    if not token:
        return rows, "no_token"
    ikey_map = historical_data._ikeys_for([symbol], db_path=DB)
    ikey = ikey_map.get(symbol)
    if not ikey:
        return rows, "no_ikey"
    try:
        candles = historical_data.fetch_daily_candles(ikey, days=days, token=token)
        if candles:
            historical_data.cache_candles(symbol, candles, db_path=DB)
            rows = historical_data.load_cached(symbol, days=days, db_path=DB)
            return rows, f"fetched({len(candles)})"
    except Exception as exc:
        return rows, f"fetch_err: {exc}"
    return rows, "fetch_empty"


def confluence(side: str, oi_st: dict, feat: dict, levels: dict) -> int:
    """0-100 confluence score combining OI thesis side + chart bias + trade structure."""
    bias = chart_features.chart_bias(feat)
    score = 0
    # OI side weight (40)
    score += 40
    # Chart bias agreement (30)
    if side == "bull":
        score += {"STRONG_BULL": 30, "BULL": 22, "NEUTRAL": 12, "BEAR": 0, "STRONG_BEAR": -10}.get(bias, 0)
    else:
        score += {"STRONG_BEAR": 30, "BEAR": 22, "NEUTRAL": 12, "BULL": 0, "STRONG_BULL": -10}.get(bias, 0)
    # Trade structure quality (15) — risk/reward to T1
    if levels:
        rr = levels.get("rr_t1") or 0
        if rr >= 2.5:   score += 15
        elif rr >= 1.5: score += 10
        elif rr >= 1.0: score += 5
    # PCR confirmation (15)
    pcr = oi_st.get("pcr") or 0
    if side == "bull":
        if pcr <= 0.46: score += 15
        elif pcr <= 0.58: score += 8
        elif pcr <= 0.75: score += 3
    else:
        if pcr >= 0.75: score += 15
        elif pcr >= 0.58: score += 8
        elif pcr >= 0.46: score += 3
    return max(0, min(100, score))


def fmt_levels(L):
    if not L:
        return "—"
    return (f"E:{L['entry']:.2f} SL:{L['stop']:.2f} "
            f"T1:{L['target1']:.2f} T2:{L['target2']:.2f} "
            f"RR:{L['rr_t1']:.1f}")


def analyse_side(picks, side):
    print(f"\n{'='*86}")
    print(f"  {side.upper()} candidates (top 4 by OI rotation)")
    print(f"{'='*86}")
    out = []
    for st in picks:
        sym = st["sym"]
        rows, src = load_or_fetch(sym, days=90)
        if not rows:
            print(f"\n[{sym}] no historical data ({src}); skipping")
            continue
        feat = chart_features.compute(rows)
        bias = chart_features.chart_bias(feat)
        levels = chart_features.trade_levels(feat, side=side)
        conf = confluence(side, st, feat, levels)

        last = feat.get("last_close")
        rsi_v = feat.get("rsi14")
        atr_v = feat.get("atr14")
        bb_pb = feat.get("bb_pct_b")
        sma20 = feat.get("sma20")
        sma50 = feat.get("sma50")
        sma200 = feat.get("sma200")
        fvgs = feat.get("fvgs") or []
        weekly = feat.get("weekly") or {}
        pivots = feat.get("pivots") or {}

        def _f(v, p=2):
            return f"{v:.{p}f}" if isinstance(v, (int, float)) else "—"

        print(f"\n[{sym}]  ltp {st['ltp']:.2f}  chg {st['chg_pct']:+.2f}%  "
              f"max_pain {st.get('max_pain') or '—'}  pcr {st['pcr']:.2f} "
              f"({st['pcr_sig']})  buildup {st['buildup']}  src={src}")
        print(f"  Chart: bias={bias}  RSI14={_f(rsi_v, 1)}  ATR14={_f(atr_v, 2)}  "
              f"BB%B={_f(bb_pb, 2)}  SMA20/50/200 = "
              f"{_f(sma20, 1)}/{_f(sma50, 1)}/{_f(sma200, 1)}")
        if pivots:
            print(f"  Pivots:  R2={pivots['R2']:.2f}  R1={pivots['R1']:.2f}  "
                  f"PP={pivots['PP']:.2f}  S1={pivots['S1']:.2f}  S2={pivots['S2']:.2f}")
        if weekly.get("week_high") and weekly.get("week_low"):
            print(f"  Weekly:  H={weekly['week_high']:.2f}  L={weekly['week_low']:.2f}")
        if fvgs:
            recent = fvgs[-2:]
            for g in recent:
                size_pct = (g["high"] - g["low"]) / max(g["mid_close"], 0.01) * 100
                print(f"  FVG {g['type']:4} @ {g['low']:.2f}-{g['high']:.2f} ({size_pct:.2f}%)")
        print(f"  OI rotation:  CE_chg={st['ce_oi_chg']:+,.0f}  PE_chg={st['pe_oi_chg']:+,.0f}  "
              f"total_oi={st['total_oi']:,.0f}")
        print(f"  Levels ({side}): {fmt_levels(levels)}")
        print(f"  >>> Confluence score: {conf}/100")

        out.append({
            "sym": sym, "side": side, "conf": conf, "bias": bias,
            "ltp": st["ltp"], "chg": st["chg_pct"], "pcr": st["pcr"],
            "rsi": rsi_v, "atr": atr_v, "levels": levels,
            "max_pain": st.get("max_pain"),
        })
    return out


def main():
    if not Path("/tmp/qt_top8.json").exists():
        print("ERROR: run scripts/_pick_top8.py first")
        return 1
    with open("/tmp/qt_top8.json") as f:
        picks = json.load(f)

    bull_out = analyse_side(picks["bull"], "bull")
    bear_out = analyse_side(picks["bear"], "bear")

    # Final ranking — interleaved by confluence
    all_out = sorted(bull_out + bear_out, key=lambda x: -x["conf"])
    print(f"\n\n{'='*98}")
    print("  FINAL RANKING (by confluence — OI thesis + chart bias + trade structure + PCR)")
    print(f"{'='*98}")
    print(f'{"#":>2}  {"sym":12} {"side":4} {"conf":>5} {"bias":>11} {"ltp":>9} '
          f'{"chg%":>6} {"pcr":>5} {"rsi":>5}  entry → stop → T1   RR')
    for i, x in enumerate(all_out, 1):
        L = x["levels"] or {}
        ent = L.get("entry") or 0
        slp = L.get("stop") or 0
        t1  = L.get("target1") or 0
        rr  = L.get("rr_t1") or 0
        print(f'{i:>2}  {x["sym"]:12} {x["side"]:4} {x["conf"]:>5} {x["bias"]:>11} '
              f'{x["ltp"]:>9.2f} {x["chg"]:>+6.2f} {x["pcr"]:>5.2f} '
              f'{(x["rsi"] or 0):>5.1f}  {ent:>7.2f} → {slp:>7.2f} → {t1:>7.2f}  {rr:>4.1f}')

    # Save markdown report
    out_dir = ROOT / "data" / "research"
    out_dir.mkdir(parents=True, exist_ok=True)
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    md_path = out_dir / f"{today}_top4_each_analysis.md"
    with open(md_path, "w") as f:
        f.write(f"# Top 4 each side — Live OI Thesis + Chart Confluence\n\nDate: {today}\n\n")
        f.write("## Final ranking\n\n")
        f.write("| # | Symbol | Side | Conf | Chart bias | LTP | Chg% | PCR | RSI | Entry | Stop | T1 | RR |\n")
        f.write("|---|--------|------|------|------------|-----|------|-----|-----|-------|------|----|----|\n")
        for i, x in enumerate(all_out, 1):
            L = x["levels"] or {}
            f.write(
                f'| {i} | {x["sym"]} | {x["side"].upper()} | {x["conf"]} | '
                f'{x["bias"]} | {x["ltp"]:.2f} | {x["chg"]:+.2f}% | '
                f'{x["pcr"]:.2f} | {(x["rsi"] or 0):.1f} | '
                f'{(L.get("entry") or 0):.2f} | {(L.get("stop") or 0):.2f} | '
                f'{(L.get("target1") or 0):.2f} | {(L.get("rr_t1") or 0):.1f} |\n'
            )
        f.write(
            "\n## Method\n\n"
            "- OI thesis filter: bullish=CE↓ PE↑, bearish=CE↑ PE↓ (live, today, total_oi≥100k)\n"
            "- Top 4 each side ranked by |ce_chg| + |pe_chg| (OI-rotation magnitude)\n"
            "- Chart bias from 90-day daily OHLC: SMA20/50/200, RSI14, BB %B (chart_features.compute)\n"
            "- Trade levels: ATR-based stop tightened to prior pivot, T1=pivot R2/S2, T2=weekly H/L or 3.5×ATR\n"
            "- Confluence (0-100): OI side 40 + bias agreement 0-30 + trade structure 0-15 + PCR confirm 0-15\n"
        )
    print(f"\nReport saved → {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
