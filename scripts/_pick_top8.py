"""Pick the live top-4 bullish and top-4 bearish per the OI thesis rule.

Bullish:  ce_oi_chg < 0  AND  pe_oi_chg > 0   (calls being unwound, puts written → support)
Bearish:  ce_oi_chg > 0  AND  pe_oi_chg < 0   (calls written, puts unwound → resistance)

Ranked by total |OI rotation| (|ce_chg| + |pe_chg|) — proxy for conviction.
Liquidity floor: total_oi >= 100_000.
"""
import json
import urllib.request

LIQ = 100_000

with urllib.request.urlopen("http://localhost:8081/api/state") as r:
    d = json.load(r)

stocks = d.get("stocks", d) if isinstance(d, dict) else d
items = list(stocks.items()) if isinstance(stocks, dict) else []

bull, bear = [], []
for sym, st in items:
    toi = st.get("total_oi") or 0
    ce = st.get("ce_oi_chg") or 0
    pe = st.get("pe_oi_chg") or 0
    if toi < LIQ:
        continue
    rot = abs(ce) + abs(pe)
    if ce < 0 and pe > 0:
        bull.append((sym, st, rot))
    elif ce > 0 and pe < 0:
        bear.append((sym, st, rot))

bull.sort(key=lambda x: -x[2])
bear.sort(key=lambda x: -x[2])


def fmt(rows, label):
    print(f"\n=== TOP 4 {label} (live OI thesis) ===")
    print(
        f'{"sym":12} {"ltp":>9} {"chg%":>7} {"pcr":>5} {"sig":>14} '
        f'{"buildup":>14} {"ce_chg":>11} {"pe_chg":>11} {"toi":>10} {"score":>5}'
    )
    for sym, st, _ in rows[:4]:
        print(
            f'{sym:12} {st.get("ltp", 0):>9.2f} {st.get("chg_pct", 0):>7.2f} '
            f'{st.get("pcr", 0):>5.2f} {st.get("pcr_sig", ""):>14} '
            f'{st.get("buildup", ""):>14} {st.get("ce_oi_chg", 0):>11,} '
            f'{st.get("pe_oi_chg", 0):>11,} {st.get("total_oi", 0):>10,} '
            f'{st.get("score", 0):>5}'
        )


fmt(bull, "BULLISH (CE\u2193 PE\u2191)")
fmt(bear, "BEARISH (CE\u2191 PE\u2193)")

picks = {
    "bull": [
        {"sym": s, "ltp": st.get("ltp"), "max_pain": st.get("max_pain"),
         "atm_strike": st.get("atm_strike"), "atm_ce": st.get("atm_ce"),
         "atm_pe": st.get("atm_pe"), "pcr": st.get("pcr"),
         "atm_iv": st.get("atm_iv"), "score": st.get("score"),
         "buildup": st.get("buildup"), "pcr_sig": st.get("pcr_sig"),
         "chg_pct": st.get("chg_pct"), "range_pct": st.get("range_pct"),
         "ce_oi_chg": st.get("ce_oi_chg"), "pe_oi_chg": st.get("pe_oi_chg"),
         "total_oi": st.get("total_oi")}
        for s, st, _ in bull[:4]
    ],
    "bear": [
        {"sym": s, "ltp": st.get("ltp"), "max_pain": st.get("max_pain"),
         "atm_strike": st.get("atm_strike"), "atm_ce": st.get("atm_ce"),
         "atm_pe": st.get("atm_pe"), "pcr": st.get("pcr"),
         "atm_iv": st.get("atm_iv"), "score": st.get("score"),
         "buildup": st.get("buildup"), "pcr_sig": st.get("pcr_sig"),
         "chg_pct": st.get("chg_pct"), "range_pct": st.get("range_pct"),
         "ce_oi_chg": st.get("ce_oi_chg"), "pe_oi_chg": st.get("pe_oi_chg"),
         "total_oi": st.get("total_oi")}
        for s, st, _ in bear[:4]
    ],
}
with open("/tmp/qt_top8.json", "w") as f:
    json.dump(picks, f, indent=2)

print("\nSaved 8 picks to /tmp/qt_top8.json")
