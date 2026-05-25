"""
chart_features.py — Computes price-action features from a daily OHLC series.

Pure stdlib. No pandas / numpy dependency. Inputs come from
historical_data.load_cached(symbol, days=N).

Every function takes a `rows` list of dicts (oldest -> newest) of
{date, open, high, low, close, volume[, oi]} and returns a single
number, list, or feature dict.

References
----------
- 20/50/200 SMA: standard textbook.
- Pivot points: classical formula (PP = (H+L+C)/3, R1 = 2*PP - L, etc.).
- RSI(14): Wilder's original formula via EMA-style averaging.
- Bollinger Bands: SMA(20) ± 2 * std(20).
- ATR(14): Wilder's True Range with EMA smoothing.
- Fractals (swing H/L): 5-bar local extrema (Bill Williams convention).
- Fair Value Gap: 3-candle imbalance per ICT-style price action.

All are well-defined, deterministic, and computable from OHLC alone.
We do NOT attempt SMC "narrative" reads (liquidity sweeps, session
displacement, order-block intent) because those need session/timeframe
context we don't have at daily granularity. They belong in a future
intraday extension.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Atomic helpers
# ---------------------------------------------------------------------------
def closes(rows: List[Dict[str, Any]]) -> List[float]:
    return [r["close"] for r in rows if r.get("close") is not None]


def highs(rows: List[Dict[str, Any]]) -> List[float]:
    return [r["high"] for r in rows]


def lows(rows: List[Dict[str, Any]]) -> List[float]:
    return [r["low"] for r in rows]


def sma(values: List[float], n: int) -> Optional[float]:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def ema(values: List[float], n: int) -> Optional[float]:
    if len(values) < n:
        return None
    k = 2 / (n + 1)
    e = sum(values[:n]) / n  # seed with SMA of first n
    for v in values[n:]:
        e = v * k + e * (1 - k)
    return e


def stdev(values: List[float], n: int) -> Optional[float]:
    if len(values) < n:
        return None
    sl = values[-n:]
    m = sum(sl) / n
    var = sum((x - m) ** 2 for x in sl) / n
    return math.sqrt(var)


# ---------------------------------------------------------------------------
# Trend / level features
# ---------------------------------------------------------------------------
def trend_features(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Returns SMAs, current close vs each SMA, simple trend direction tag."""
    cl = closes(rows)
    if not cl:
        return {"available": False}
    last = cl[-1]
    s20 = sma(cl, 20)
    s50 = sma(cl, 50)
    s200 = sma(cl, 200)

    def above(c, s):
        return None if s is None else c > s

    # Simple trend tag using stack of SMAs
    if s20 and s50 and s200:
        if last > s20 > s50 > s200:
            tag = "STRONG_UPTREND"
        elif last > s20 and s20 > s50:
            tag = "UPTREND"
        elif last < s20 < s50 < s200:
            tag = "STRONG_DOWNTREND"
        elif last < s20 and s20 < s50:
            tag = "DOWNTREND"
        else:
            tag = "SIDEWAYS"
    elif s20 and s50:
        tag = "UPTREND" if last > s20 > s50 else ("DOWNTREND" if last < s20 < s50 else "SIDEWAYS")
    else:
        tag = "INSUFFICIENT_DATA"

    return {
        "available": True,
        "last_close": last,
        "sma20": s20,
        "sma50": s50,
        "sma200": s200,
        "above_sma20": above(last, s20),
        "above_sma50": above(last, s50),
        "above_sma200": above(last, s200),
        "trend": tag,
    }


def pivot_levels(prior_row: Dict[str, Any]) -> Optional[Dict[str, float]]:
    """Classical floor-trader pivots from the prior session's H/L/C."""
    if not prior_row:
        return None
    h, l, c = prior_row["high"], prior_row["low"], prior_row["close"]
    pp = (h + l + c) / 3
    r1 = 2 * pp - l
    s1 = 2 * pp - h
    r2 = pp + (h - l)
    s2 = pp - (h - l)
    r3 = h + 2 * (pp - l)
    s3 = l - 2 * (h - pp)
    return {"PP": pp, "R1": r1, "R2": r2, "R3": r3, "S1": s1, "S2": s2, "S3": s3,
            "prior_high": h, "prior_low": l, "prior_close": c}


def weekly_levels(rows: List[Dict[str, Any]]) -> Dict[str, Optional[float]]:
    """High/low of the last 5 trading sessions."""
    if len(rows) < 5:
        return {"week_high": None, "week_low": None}
    last5 = rows[-5:]
    return {
        "week_high": max(r["high"] for r in last5),
        "week_low":  min(r["low"]  for r in last5),
    }


# ---------------------------------------------------------------------------
# Momentum
# ---------------------------------------------------------------------------
def rsi(values: List[float], n: int = 14) -> Optional[float]:
    """Wilder's RSI(14). Returns 0-100, or None if insufficient data."""
    if len(values) < n + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(values)):
        diff = values[i] - values[i - 1]
        gains.append(max(0.0, diff))
        losses.append(max(0.0, -diff))
    # First averages = simple mean of first n gains/losses
    avg_g = sum(gains[:n]) / n
    avg_l = sum(losses[:n]) / n
    # Wilder smoothing for the rest
    for g, l in zip(gains[n:], losses[n:]):
        avg_g = (avg_g * (n - 1) + g) / n
        avg_l = (avg_l * (n - 1) + l) / n
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100 - (100 / (1 + rs))


def momentum_features(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    cl = closes(rows)
    r = rsi(cl, 14)
    bias = None
    if r is not None:
        if r >= 70:
            bias = "OVERBOUGHT"
        elif r <= 30:
            bias = "OVERSOLD"
        elif r >= 55:
            bias = "BULLISH"
        elif r <= 45:
            bias = "BEARISH"
        else:
            bias = "NEUTRAL"
    return {"rsi14": r, "rsi_bias": bias}


# ---------------------------------------------------------------------------
# Volatility
# ---------------------------------------------------------------------------
def atr(rows: List[Dict[str, Any]], n: int = 14) -> Optional[float]:
    """Wilder's ATR(14). Returns rupees-per-day average true range."""
    if len(rows) < n + 1:
        return None
    trs = []
    for i in range(1, len(rows)):
        h, l = rows[i]["high"], rows[i]["low"]
        pc = rows[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    a = sum(trs[:n]) / n
    for tr in trs[n:]:
        a = (a * (n - 1) + tr) / n
    return a


def bollinger(rows: List[Dict[str, Any]], n: int = 20, k: float = 2.0) -> Optional[Dict[str, float]]:
    cl = closes(rows)
    m = sma(cl, n)
    sd = stdev(cl, n)
    if m is None or sd is None:
        return None
    return {"mid": m, "upper": m + k * sd, "lower": m - k * sd, "width_pct": (4 * sd) / m * 100}


def realized_volatility(rows: List[Dict[str, Any]], n: int = 20) -> Optional[float]:
    """20-day annualized close-to-close realized volatility, %."""
    cl = closes(rows)
    if len(cl) < n + 1:
        return None
    returns = []
    for i in range(len(cl) - n, len(cl)):
        if cl[i - 1] <= 0:
            continue
        returns.append(math.log(cl[i] / cl[i - 1]))
    if not returns:
        return None
    m = sum(returns) / len(returns)
    var = sum((r - m) ** 2 for r in returns) / len(returns)
    daily_vol = math.sqrt(var)
    # ~252 trading days/year
    return daily_vol * math.sqrt(252) * 100


# ---------------------------------------------------------------------------
# Swing / pattern features
# ---------------------------------------------------------------------------
def swing_levels(rows: List[Dict[str, Any]], window: int = 5) -> Tuple[List[float], List[float]]:
    """Bill Williams 5-bar fractals: pivot if center bar's high/low is the
    extreme of the window. Returns (recent_swing_highs, recent_swing_lows).
    """
    if len(rows) < window:
        return [], []
    half = window // 2
    sh, sl = [], []
    for i in range(half, len(rows) - half):
        win = rows[i - half:i + half + 1]
        if max(r["high"] for r in win) == rows[i]["high"]:
            sh.append(rows[i]["high"])
        if min(r["low"] for r in win) == rows[i]["low"]:
            sl.append(rows[i]["low"])
    return sh[-5:], sl[-5:]   # last 5 of each


def fair_value_gaps(rows: List[Dict[str, Any]], lookback: int = 30) -> List[Dict[str, Any]]:
    """
    ICT-style 3-candle fair value gap. Returns un-mitigated gaps in the
    most recent `lookback` bars.

    Bullish FVG: candle[i+1].low > candle[i-1].high   (gap above prior high)
    Bearish FVG: candle[i+1].high < candle[i-1].low   (gap below prior low)
    "Un-mitigated" = no later candle's range has touched the gap zone.
    """
    if len(rows) < 3:
        return []
    n = min(len(rows), lookback)
    section = rows[-n:]
    gaps = []
    for i in range(1, len(section) - 1):
        prev_c, mid_c, next_c = section[i - 1], section[i], section[i + 1]
        # Bullish gap
        if next_c["low"] > prev_c["high"]:
            gap = {
                "type": "bull",
                "date": mid_c.get("date"),
                "low":  prev_c["high"],
                "high": next_c["low"],
                "mid_close": mid_c["close"],
            }
            # Mitigation check: any later candle whose low <= gap["high"] AND high >= gap["low"]
            mitigated = any(
                r["low"] <= gap["high"] and r["high"] >= gap["low"]
                for r in section[i + 2:]
            )
            if not mitigated:
                gaps.append(gap)
        elif next_c["high"] < prev_c["low"]:
            gap = {
                "type": "bear",
                "date": mid_c.get("date"),
                "low":  next_c["high"],
                "high": prev_c["low"],
                "mid_close": mid_c["close"],
            }
            mitigated = any(
                r["low"] <= gap["high"] and r["high"] >= gap["low"]
                for r in section[i + 2:]
            )
            if not mitigated:
                gaps.append(gap)
    return gaps[-3:]  # last 3 unmitigated


# ---------------------------------------------------------------------------
# Top-level: compute everything for one symbol
# ---------------------------------------------------------------------------
def compute(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Master feature dict. Caller passes `rows` (oldest -> newest, daily OHLC).
    All features that can't be computed (insufficient data) are returned as None.
    """
    if not rows:
        return {"available": False, "n_bars": 0}
    out: Dict[str, Any] = {"available": True, "n_bars": len(rows), "last_date": rows[-1]["date"]}
    out.update(trend_features(rows))
    out.update(momentum_features(rows))
    bb = bollinger(rows)
    if bb:
        out["bollinger"] = bb
        out["bb_pct_b"] = (rows[-1]["close"] - bb["lower"]) / (bb["upper"] - bb["lower"]) if bb["upper"] != bb["lower"] else None
    out["atr14"] = atr(rows)
    out["rv20"] = realized_volatility(rows, 20)
    out["pivots"] = pivot_levels(rows[-2]) if len(rows) >= 2 else None
    out["weekly"] = weekly_levels(rows)
    sh, sl = swing_levels(rows, window=5)
    out["swing_highs"] = sh
    out["swing_lows"] = sl
    out["fvgs"] = fair_value_gaps(rows, lookback=30)
    return out


# ---------------------------------------------------------------------------
# Confluence helpers (used by report builder)
# ---------------------------------------------------------------------------
def chart_bias(features: Dict[str, Any]) -> str:
    """
    Quick string summary of where the chart leans, BEFORE we look at OI.
    Returns one of: STRONG_BULL, BULL, NEUTRAL, BEAR, STRONG_BEAR, INSUFFICIENT.
    """
    if not features.get("available"):
        return "INSUFFICIENT"
    score = 0
    if features.get("above_sma20"):  score += 1
    if features.get("above_sma50"):  score += 1
    if features.get("above_sma200"): score += 1
    rsi_v = features.get("rsi14")
    if rsi_v is not None:
        if rsi_v > 60:   score += 1
        elif rsi_v < 40: score -= 1
    bb = features.get("bollinger")
    pb = features.get("bb_pct_b")
    if pb is not None:
        if pb > 0.85:    score += 1
        elif pb < 0.15:  score -= 1
    if score >= 4: return "STRONG_BULL"
    if score >= 2: return "BULL"
    if score <= -3: return "STRONG_BEAR"
    if score <= -1: return "BEAR"
    return "NEUTRAL"


def trade_levels(features: Dict[str, Any], side: str) -> Optional[Dict[str, float]]:
    """
    Suggest entry / stop / targets from chart features, given a directional bias.

    Conservative defaults:
      Bull:  entry = pivot R1 break (or last close);
             stop  = max(prior_low, last_close - 1.5*ATR);
             T1    = pivot R2;  T2 = week_high or last_close + 3*ATR.
      Bear:  mirror.

    None if insufficient data.
    """
    if not features.get("available"):
        return None
    last = features.get("last_close")
    a = features.get("atr14")
    p = features.get("pivots")
    w = features.get("weekly")
    if not last or not a:
        return None

    if side == "bull":
        entry = last
        stop = (p["prior_low"] if p else last) if p else last - 1.5 * a
        # Tighter of pivot S1 and 1.5*ATR stop
        atr_stop = last - 1.5 * a
        stop = max(stop, atr_stop)
        # Floor: stop must be at least 0.5*ATR below entry
        if entry - stop < 0.5 * a:
            stop = entry - 0.5 * a
        target1 = p["R2"] if p and p["R2"] > last else last + 2 * a
        target2 = (w["week_high"] if w and w["week_high"] and w["week_high"] > last else last + 3.5 * a)
        risk = max(entry - stop, 0.01 * entry)  # min 1% risk to avoid div blowup
        return {"entry": entry, "stop": stop, "target1": target1, "target2": target2,
                "rr_t1": (target1 - entry) / risk,
                "atr": a}
    else:  # bear
        entry = last
        stop = (p["prior_high"] if p else last) if p else last + 1.5 * a
        atr_stop = last + 1.5 * a
        stop = min(stop, atr_stop)
        if stop - entry < 0.5 * a:
            stop = entry + 0.5 * a
        target1 = p["S2"] if p and p["S2"] < last else last - 2 * a
        target2 = (w["week_low"] if w and w["week_low"] and w["week_low"] < last else last - 3.5 * a)
        risk = max(stop - entry, 0.01 * entry)
        return {"entry": entry, "stop": stop, "target1": target1, "target2": target2,
                "rr_t1": (entry - target1) / risk,
                "atr": a}
