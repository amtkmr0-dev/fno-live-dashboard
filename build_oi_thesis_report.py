"""
build_oi_thesis_report.py

Generates the daily OI Divergence Thesis research report by combining:
  - Today's OI thesis flags (from oi_thesis_flag table)
  - Live state from /api/state (PCR, IV, max pain, conviction tier)
  - Historical OHLC features (chart_features.compute) — trend, RSI, ATR,
    pivots, weekly H/L, swing levels, FVGs, RV20

Output: data/research/YYYY-MM-DD_oi_thesis_report.md

Run AFTER:
  1. oi_thesis_tracker.py daily   (captures today's flags)
  2. historical_data.py fetch_today_flags   (caches today's OHLC)
"""
from __future__ import annotations

import json
import sqlite3
import sys
import urllib.request
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import data_recorder
import historical_data
import chart_features as cf

IST = timezone(timedelta(hours=5, minutes=30))


def fetch_state() -> Dict[str, Any]:
    with urllib.request.urlopen("http://127.0.0.1:8081/api/state", timeout=5) as r:
        return json.loads(r.read().decode())["stocks"]


def fmt_int(n: Optional[float]) -> str:
    if n is None:
        return "—"
    n = int(n)
    sign = "+" if n > 0 else ("" if n == 0 else "-")
    a = abs(n)
    if a >= 10_000_000: return f"{sign}{a/10_000_000:.2f}Cr"
    if a >= 100_000:    return f"{sign}{a/100_000:.2f}L"
    if a >= 1_000:      return f"{sign}{a/1_000:.1f}K"
    return f"{sign}{a}"


def fmt_pct(n: Optional[float], decimals: int = 1) -> str:
    if n is None: return "—"
    return f"{n:+.{decimals}f}%"


def fmt_p(n: Optional[float]) -> str:
    if n is None: return "—"
    return f"₹{n:,.2f}"


def confluence_score(flag_side: str, features: Dict[str, Any], live: Dict[str, Any]) -> Dict[str, Any]:
    """
    Combine OI thesis with chart features into a 0-100 confluence score
    AND a short reason list. Bull or bear depending on flag_side.
    """
    # Components scored 0-100 then averaged with weights
    parts: Dict[str, float] = {}
    notes: List[str] = []

    # 1. OI thesis strength (already encoded by being in flag set; rank-based)
    parts["oi_thesis"] = 100.0  # presence in top-10 = full points
    notes.append("In OI Thesis top-10 by net |CE−PE|")

    # 2. PCR alignment with flag direction (post-flip Indian convention)
    pcr = live.get("pcr")
    if pcr is None:
        parts["pcr_align"] = 50
    else:
        if flag_side == "bull":
            # bullish if pcr < 1
            parts["pcr_align"] = max(0, min(100, (1 - pcr) * 100 + 50))
        else:
            parts["pcr_align"] = max(0, min(100, (pcr - 1) * 100 + 50))

    # 3. Volume surge alignment (Easley-O'Hara: surge in flag direction)
    surge = live.get("vol_surge") or 0
    parts["surge"] = max(0, min(100, surge * 40))  # 1.0x → 40, 2.5x → 100

    # 4. Chart-side trend agreement
    trend = features.get("trend", "INSUFFICIENT_DATA")
    if flag_side == "bull":
        if trend == "STRONG_UPTREND":   parts["trend"] = 100
        elif trend == "UPTREND":        parts["trend"] = 75
        elif trend == "SIDEWAYS":       parts["trend"] = 50
        elif trend == "DOWNTREND":      parts["trend"] = 25
        elif trend == "STRONG_DOWNTREND": parts["trend"] = 0
        else:                           parts["trend"] = 50
    else:
        if trend == "STRONG_DOWNTREND": parts["trend"] = 100
        elif trend == "DOWNTREND":      parts["trend"] = 75
        elif trend == "SIDEWAYS":       parts["trend"] = 50
        elif trend == "UPTREND":        parts["trend"] = 25
        elif trend == "STRONG_UPTREND": parts["trend"] = 0
        else:                           parts["trend"] = 50

    # 5. RSI confirmation
    rsi_v = features.get("rsi14")
    if rsi_v is None:
        parts["rsi"] = 50
    else:
        if flag_side == "bull":
            # Constructive if RSI in 45-70 (room to run, not yet overbought)
            if 45 <= rsi_v <= 70:        parts["rsi"] = 100
            elif 35 <= rsi_v < 45:       parts["rsi"] = 75   # oversold bounce zone
            elif 70 < rsi_v <= 80:       parts["rsi"] = 50   # extended
            elif rsi_v > 80:             parts["rsi"] = 20   # overbought
            else:                        parts["rsi"] = 35   # < 35 deep oversold
        else:
            if 30 <= rsi_v <= 55:        parts["rsi"] = 100
            elif 55 < rsi_v <= 65:       parts["rsi"] = 75
            elif 20 <= rsi_v < 30:       parts["rsi"] = 50
            elif rsi_v < 20:             parts["rsi"] = 20
            else:                        parts["rsi"] = 35

    # 6. Bollinger %B context
    pb = features.get("bb_pct_b")
    if pb is None:
        parts["bb"] = 50
    else:
        if flag_side == "bull":
            parts["bb"] = max(0, min(100, pb * 100))
        else:
            parts["bb"] = max(0, min(100, (1 - pb) * 100))

    # Weighted blend
    weights = {"oi_thesis": 0.25, "pcr_align": 0.15, "surge": 0.15,
               "trend": 0.20, "rsi": 0.15, "bb": 0.10}
    total = sum(parts[k] * weights[k] for k in weights if k in parts)

    return {
        "score": round(total, 1),
        "parts": {k: round(v, 1) for k, v in parts.items()},
        "trend": trend,
        "rsi": rsi_v,
    }


def render_report(flag_date: str, db_path: Optional[Path] = None) -> str:
    """Build the markdown report. Returns the markdown string."""
    state = fetch_state()
    with closing(data_recorder._connect(db_path)) as conn:
        flags = [dict(r) for r in conn.execute(
            "SELECT * FROM oi_thesis_flag WHERE flag_date = ? AND rule_id = 'oi_div_v1' ORDER BY side, rank",
            (flag_date,),
        ).fetchall()]
    if not flags:
        return f"# OI Thesis Report — {flag_date}\n\nNo flags found for this date.\n"

    # Compute features per stock
    enriched: List[Dict[str, Any]] = []
    for f in flags:
        sym = f["symbol"]
        rows = historical_data.load_cached(sym, days=90)
        feat = cf.compute(rows)
        live = state.get(sym, {})
        conf = confluence_score(f["side"], feat, live)
        levels = cf.trade_levels(feat, f["side"])
        enriched.append({
            "flag": f, "live": live, "feat": feat, "conf": conf, "levels": levels,
        })

    # Sort each side by confluence score desc
    bulls = sorted([e for e in enriched if e["flag"]["side"] == "bull"],
                   key=lambda e: e["conf"]["score"], reverse=True)
    bears = sorted([e for e in enriched if e["flag"]["side"] == "bear"],
                   key=lambda e: e["conf"]["score"], reverse=True)

    # Build markdown
    md: List[str] = []
    md.append(f"# OI Divergence Thesis — Research Report (with Price Action)")
    md.append(f"## Flag Date: {flag_date} (EOD) · Resolves next trading session")
    md.append("")
    md.append("> **Disclaimer.** Analysis, not advice. SEBI FY25: 91% of individual F&O")
    md.append("> traders made net losses. Treat scores as research tags, not signals.")
    md.append("")
    md.append("## Method")
    md.append("")
    md.append("**OI thesis** (rule_v1): top-10 per side, ranked by `|CE_oi_chg − PE_oi_chg|`, ")
    md.append("liquidity floor `total_oi ≥ 1L`. Bull = CE↓/PE↑, Bear = CE↑/PE↓.")
    md.append("")
    md.append("**Confluence score** combines OI thesis with chart features computed from ")
    md.append("90 days of daily OHLC fetched via Upstox v2 historical-candle:")
    md.append("")
    md.append("| Component | Weight | What it measures |")
    md.append("|---|---:|---|")
    md.append("| OI thesis | 25% | Presence in top-10 by net thesis |")
    md.append("| Trend | 20% | SMA20/50/200 stack agreement with flag side |")
    md.append("| PCR alignment | 15% | Indian-retail PCR convention agrees with flag |")
    md.append("| Vol surge | 15% | Today's volume vs 5-day average (Easley-O'Hara) |")
    md.append("| RSI(14) | 15% | Momentum in constructive range for the side |")
    md.append("| Bollinger %B | 10% | Position within 20-day volatility envelope |")
    md.append("")
    md.append("Levels (entry/stop/targets) derived from prior-day pivots + 14-day ATR + weekly H/L.")
    md.append("")

    def render_side(label: str, items: List[Dict[str, Any]]):
        if not items:
            return
        md.append(f"## {label} Side — Ranked by Confluence")
        md.append("")
        md.append("| # | Symbol | Conf | Trend | RSI | OI Net | PCR | Surge | LTP | Entry | Stop | T1 | T2 | RR |")
        md.append("|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        for i, e in enumerate(items, 1):
            f = e["flag"]; live = e["live"]; feat = e["feat"]; conf = e["conf"]; lv = e["levels"]
            ltp = live.get("ltp")
            rsi_str = f"{feat.get('rsi14'):.0f}" if feat.get("rsi14") is not None else "—"
            row = [
                str(i), f"**{f['symbol']}**", f"{conf['score']:.0f}",
                feat.get("trend", "—").replace("_", " ").title(),
                rsi_str, fmt_int(f["net_thesis"]),
                f"{f['pcr_at_flag']:.2f}" if f["pcr_at_flag"] is not None else "—",
                f"{f['surge_at_flag']:.2f}x" if f["surge_at_flag"] is not None else "—",
                fmt_p(ltp),
            ]
            if lv:
                row += [fmt_p(lv["entry"]), fmt_p(lv["stop"]), fmt_p(lv["target1"]), fmt_p(lv["target2"]),
                        f"1:{lv['rr_t1']:.1f}" if lv['rr_t1'] else "—"]
            else:
                row += ["—", "—", "—", "—", "—"]
            md.append("| " + " | ".join(row) + " |")
        md.append("")

        # Top-3 deep notes
        md.append(f"### {label} side — Top-3 deep reads")
        for i, e in enumerate(items[:3], 1):
            f = e["flag"]; feat = e["feat"]; live = e["live"]; conf = e["conf"]; lv = e["levels"]
            md.append("")
            md.append(f"**{i}. {f['symbol']}** — Confluence {conf['score']:.0f}/100 · {feat.get('trend','—').replace('_',' ').title()}")
            # OI side
            md.append(f"- OI: CE chg {fmt_int(f['ce_oi_chg'])}, PE chg {fmt_int(f['pe_oi_chg'])}, net {fmt_int(f['net_thesis'])}, total OI {fmt_int(f['total_oi'])}.")
            # Levels
            md.append(f"- Trend: 20SMA {fmt_p(feat.get('sma20'))}, 50SMA {fmt_p(feat.get('sma50'))}, 200SMA {fmt_p(feat.get('sma200'))}; close above SMA20={feat.get('above_sma20')}, SMA50={feat.get('above_sma50')}, SMA200={feat.get('above_sma200')}.")
            md.append(f"- Momentum: RSI14 {feat.get('rsi14',0):.0f} ({feat.get('rsi_bias','—')}); ATR14 ₹{feat.get('atr14',0):.2f}; RV20 {feat.get('rv20',0):.1f}%.")
            piv = feat.get("pivots") or {}
            wk = feat.get("weekly") or {}
            if piv:
                md.append(f"- Levels: PP {fmt_p(piv.get('PP'))}, R1 {fmt_p(piv.get('R1'))}, R2 {fmt_p(piv.get('R2'))}, S1 {fmt_p(piv.get('S1'))}, S2 {fmt_p(piv.get('S2'))}; week H/L {fmt_p(wk.get('week_high'))} / {fmt_p(wk.get('week_low'))}.")
            sh = feat.get("swing_highs") or []
            sl = feat.get("swing_lows") or []
            if sh or sl:
                md.append(f"- Recent swing H: {', '.join(fmt_p(x) for x in sh) if sh else 'none'}.  Recent swing L: {', '.join(fmt_p(x) for x in sl) if sl else 'none'}.")
            fvgs = feat.get("fvgs") or []
            if fvgs:
                md.append(f"- Unmitigated FVGs (last 3): " + "; ".join(
                    f"{g['type']} @ {g['date']} [{fmt_p(g['low'])}-{fmt_p(g['high'])}]" for g in fvgs))
            else:
                md.append(f"- No unmitigated 3-candle FVGs in last 30 sessions.")
            if lv:
                md.append(f"- **Suggested levels:** Entry {fmt_p(lv['entry'])}, Stop {fmt_p(lv['stop'])}, T1 {fmt_p(lv['target1'])}, T2 {fmt_p(lv['target2'])} (RR 1:{lv['rr_t1']:.1f}).")

    render_side("Bull", bulls)
    render_side("Bear", bears)

    md.append("## Caveats and what's missing")
    md.append("")
    md.append("- **Levels are computed, not eyeballed.** A human chartist may see context")
    md.append("  this script does not (consolidation triangles, multi-month base patterns,")
    md.append("  liquidity sweeps that need session boundaries).")
    md.append("- **No SMC/ICT \"narrative\" reads** — we extract FVGs (3-candle imbalances)")
    md.append("  but not order blocks with displacement, liquidity sweeps, or session")
    md.append("  framing. Those need intraday data + manual context.")
    md.append("- **Confluence score is heuristic** — weights are reasonable defaults but")
    md.append("  not yet backtested. Use it to *sort the list*, not as a probability.")
    md.append("- **Tomorrow's open will move levels.** Pivots are computed from today's")
    md.append("  high/low/close; if there's a gap, recompute against the gap.")
    md.append("- **Indian market session** = 09:15–15:30 IST. Pre-open auction (09:00–09:08)")
    md.append("  often informs gap direction.")
    md.append("")
    md.append("---")
    md.append("")
    md.append(f"Generated {datetime.now(IST).isoformat(timespec='seconds')} from live SQLite + state snapshot + Upstox historical-candle. Method paraphrased for compliance with licensing restrictions.")
    md.append("")
    return "\n".join(md)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--date", default=None, help="flag_date (default: latest in DB)")
    p.add_argument("--out", default=None, help="output md path (default: data/research/<date>_oi_thesis_report.md)")
    args = p.parse_args()

    if args.date:
        date = args.date
    else:
        with closing(data_recorder._connect()) as conn:
            r = conn.execute("SELECT MAX(flag_date) FROM oi_thesis_flag").fetchone()
            date = r[0] if r and r[0] else datetime.now(IST).date().isoformat()

    md = render_report(date)
    out_path = Path(args.out) if args.out else Path(f"data/research/{date}_oi_thesis_report.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md)
    print(f"OK: report written to {out_path}  ({len(md)} chars)")
