"""
auto_paper_trader.py — Walk-Forward Validated Paper Trading Engine
===================================================================
Rebuilt with OOS backtest data (walk-forward May 2024 – May 2026).

Signal chain:
  NIFTY direction → Sector alignment → Stock ranking → ITM option → Paper trade

Key changes from WF backtest:
  - Whitelist rebuilt from OOS top 30 performers (41 stocks, 3 tiers)
  - Blacklist rebuilt from consistent bottom-30 losers across train+test
  - Preferred side per stock (bull vs bear) from WF data
  - SL changed from 15% premium to 0.5% spot (WF optimal)
  - Targets changed to 5x SL (WF optimal: SL=0.5%, TGT=5x)
  - WF tier bonus in composite scoring
  - Side-agreement bonus when live signals match WF preferred direction

WF OOS highlights:
  - Best configs: SL=0.5% spot, TGT=4-5x, on 15m/30m timeframes
  - Bull side dominates (24/30 top OOS stocks are bull)
  - Win rate 15-27% with PF 3.2-9.2 (low WR, high RR system)
  - Total OOS net: Rs 15.57 Cr across 209 stocks

Usage:
    from auto_paper_trader import AutoPaperTrader
    trader = AutoPaperTrader(server)
    asyncio.create_task(trader.run())
"""

import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("auto_trader")

IST = timezone(timedelta(hours=5, minutes=30))

# ---------------------------------------------------------------------------
# Walk-Forward Validated Stock Universe
# ---------------------------------------------------------------------------
# Tier system from OOS top 30 analysis:
#   TIER_1 ("both"): Top 30 in BOTH train and test → most robust
#   TIER_2 ("test"): Top 30 in OOS test only → validated OOS
#   TIER_3 ("train"): Top 30 in train only → needs OOS confirmation
#
# Each entry: { "side": preferred side, "tf": best timeframe, "pf": profit factor,
#               "wr": win rate, "net_k": net P&L in thousands, "tier": 1/2/3 }
# ---------------------------------------------------------------------------

WF_STOCKS = {
    # === TIER 1: Both train+test top 30 (most robust) ===
    "SUPREMEIND":  {"side": "bear", "tf": "15m", "pf": 4.68, "wr": 31.9, "net_k": 710, "tier": 1},
    "CONCOR":      {"side": "bear", "tf": "15m", "pf": 3.65, "wr": 26.8, "net_k": 493, "tier": 1},
    "SIEMENS":     {"side": "bull", "tf": "30m", "pf": 5.60, "wr": 35.9, "net_k": 473, "tier": 1},
    "BANDHANBNK":  {"side": "bull", "tf": "15m", "pf": 3.80, "wr": 27.5, "net_k": 392, "tier": 1},
    "ADANIGREEN":  {"side": "bull", "tf": "30m", "pf": 6.25, "wr": 38.5, "net_k": 391, "tier": 1},
    "PAGEIND":     {"side": "bear", "tf": "30m", "pf": 5.17, "wr": 34.1, "net_k": 375, "tier": 1},

    # === TIER 2: Test (OOS) top 30 only ===
    "BIOCON":      {"side": "bull", "tf": "15m", "pf": 4.15, "wr": 29.3, "net_k": 518, "tier": 2},
    "HEROMOTOCO":  {"side": "bull", "tf": "15m", "pf": 3.92, "wr": 28.2, "net_k": 508, "tier": 2},
    "WIPRO":       {"side": "bull", "tf": "15m", "pf": 3.67, "wr": 26.9, "net_k": 462, "tier": 2},
    "NBCC":        {"side": "bull", "tf": "15m", "pf": 4.00, "wr": 28.6, "net_k": 458, "tier": 2},
    "HAVELLS":     {"side": "bear", "tf": "15m", "pf": 3.64, "wr": 26.7, "net_k": 448, "tier": 2},
    "BSE":         {"side": "bear", "tf": "1h",  "pf": 9.23, "wr": 48.0, "net_k": 443, "tier": 2},
    "HINDZINC":    {"side": "bull", "tf": "15m", "pf": 4.09, "wr": 29.0, "net_k": 430, "tier": 2},
    "CUMMINSIND":  {"side": "bull", "tf": "15m", "pf": 4.25, "wr": 29.8, "net_k": 419, "tier": 2},
    "MCX":         {"side": "bull", "tf": "30m", "pf": 7.33, "wr": 42.3, "net_k": 418, "tier": 2},
    "GODFRYPHLP":  {"side": "bull", "tf": "15m", "pf": 3.62, "wr": 26.6, "net_k": 391, "tier": 2},
    "MUTHOOTFIN":  {"side": "bull", "tf": "15m", "pf": 4.10, "wr": 29.1, "net_k": 387, "tier": 2},
    "JIOFIN":      {"side": "bear", "tf": "15m", "pf": 3.57, "wr": 26.3, "net_k": 381, "tier": 2},
    "CHOLAFIN":    {"side": "bull", "tf": "30m", "pf": 4.40, "wr": 30.6, "net_k": 377, "tier": 2},
    "CAMS":        {"side": "bull", "tf": "15m", "pf": 4.32, "wr": 29.5, "net_k": 376, "tier": 2},
    "RECLTD":      {"side": "bull", "tf": "15m", "pf": 4.86, "wr": 32.7, "net_k": 373, "tier": 2},
    "BLUESTARCO":  {"side": "bull", "tf": "15m", "pf": 4.09, "wr": 29.0, "net_k": 370, "tier": 2},
    "ABCAPITAL":   {"side": "bull", "tf": "15m", "pf": 3.41, "wr": 25.4, "net_k": 369, "tier": 2},
    "AUBANK":      {"side": "bull", "tf": "15m", "pf": 3.19, "wr": 24.2, "net_k": 364, "tier": 2},
    "BANKBARODA":  {"side": "bull", "tf": "30m", "pf": 7.50, "wr": 42.9, "net_k": 361, "tier": 2},

    # === TIER 3: Train top 30 only (need OOS confirmation) ===
    "ASTRAL":      {"side": "bull", "tf": "15m", "pf": 3.75, "wr": 27.3, "net_k": 483, "tier": 3},
    "ADANIENSOL":  {"side": "bull", "tf": "30m", "pf": 3.23, "wr": 24.4, "net_k": 439, "tier": 3},
    "SONACOMS":    {"side": "bull", "tf": "15m", "pf": 3.45, "wr": 25.7, "net_k": 435, "tier": 3},
    "LTM":         {"side": "bull", "tf": "15m", "pf": 3.11, "wr": 23.8, "net_k": 396, "tier": 3},
    "DELHIVERY":   {"side": "bear", "tf": "30m", "pf": 5.50, "wr": 35.5, "net_k": 395, "tier": 3},
    "MPHASIS":     {"side": "bull", "tf": "15m", "pf": 3.29, "wr": 24.1, "net_k": 376, "tier": 3},
    "TIINDIA":     {"side": "bull", "tf": "15m", "pf": 3.39, "wr": 25.3, "net_k": 373, "tier": 3},
    "UPL":         {"side": "bear", "tf": "15m", "pf": 2.37, "wr": 19.2, "net_k": 342, "tier": 3},
    "TECHM":       {"side": "bear", "tf": "15m", "pf": 2.70, "wr": 20.3, "net_k": 328, "tier": 3},
    "RBLBANK":     {"side": "bull", "tf": "30m", "pf": 7.00, "wr": 41.2, "net_k": 321, "tier": 3},
    "LT":          {"side": "bull", "tf": "15m", "pf": 5.18, "wr": 33.3, "net_k": 321, "tier": 3},
    "SAMMAANCAP":  {"side": "bull", "tf": "30m", "pf": 5.36, "wr": 34.9, "net_k": 320, "tier": 3},
    "INFY":        {"side": "bear", "tf": "15m", "pf": 3.19, "wr": 23.4, "net_k": 319, "tier": 3},
    "INDUSINDBK":  {"side": "bull", "tf": "30m", "pf": 4.80, "wr": 32.4, "net_k": 351, "tier": 3},
    "ADANIENT":    {"side": "bull", "tf": "15m", "pf": 2.75, "wr": 21.5, "net_k": 300, "tier": 3},
}

# Convenience sets for fast lookup
WHITELIST = set(WF_STOCKS.keys())

# ---------------------------------------------------------------------------
# WF-validated blacklist: consistent losers across train + test bottom 30
# "BOTH" = in bottom 30 of both periods (strongest signal to avoid)
# ---------------------------------------------------------------------------
BLACKLIST = {
    # BOTH train+test bottom 30 — never trade these
    "TATACONSUM", "UNITDSPR", "COLPAL", "APOLLOHOSP", "SUNPHARMA", "MANAPPURAM",
    # Test (OOS) bottom 30 — confirmed losers in live-like conditions
    "BOSCHLTD", "RELIANCE", "HINDUNILVR", "BRITANNIA", "NESTLEIND",
    "HCLTECH", "TITAN", "LAURUSLABS", "DALBHARAT", "TORNTPHARM",
    "PIDILITIND", "ADANIPOWER", "OBEROIRLTY", "LUPIN", "AUROPHARMA",
    # Train bottom 30 — structural losers
    "ONGC", "ZYDUSLIFE", "ABB", "DRREDDY", "BAJAJ-AUTO",
    "CIPLA", "ITC", "BHARTIARTL", "BHARATFORG", "LICHSGFIN",
    "PIIND", "PNB", "VBL", "TVSMOTOR", "HAL", "NHPC", "DIVISLAB", "FORTIS",
    # Legacy blacklist (illiquid options)
    "IDEA", "YESBANK", "SUZLON", "GMRAIRPORT", "NMDC",
    "MOTHERSON", "BANKINDIA", "ASHOKLEY", "CANBK", "IDFCFIRSTB",
}


# ---------------------------------------------------------------------------
# Risk parameters from WF optimal config
# ---------------------------------------------------------------------------
# Best OOS configs across all timeframes:
#   SL = 0.5% of spot price
#   Target = 5x SL (i.e., 2.5% of spot)
#   Preferred TFs: 15m (21/30 top stocks), 30m (8/30), 1h (1/30)
#   Bull side dominates: 24/30 top OOS stocks are bull
# ---------------------------------------------------------------------------

SL_PCT = 0.5       # 0.5% of spot price
TGT_MULT = 5.0     # target = 5x SL
COST_PER_TRADE = 300  # brokerage + STT + slippage estimate


# ---------------------------------------------------------------------------
# Volume Profile Engine
# ---------------------------------------------------------------------------

def calculate_volume_profile(candles: List[Dict[str, Any]], num_bins: int = 50, value_area_pct: float = 0.70) -> Optional[Dict[str, float]]:
    """
    Calculate Volume Profile (POC, VAH, VAL) from a list of candles.
    Matches TradingView's step-profile methodology.
    """
    if not candles:
        return None

    highs = [c.get("high", c.get("close", 0)) for c in candles]
    lows = [c.get("low", c.get("close", 0)) for c in candles]
    highest = max(highs)
    lowest = min(lows)

    if highest == lowest:
        return None

    bin_size = (highest - lowest) / num_bins
    bins = [0] * num_bins
    bin_prices = [lowest + (i + 0.5) * bin_size for i in range(num_bins)]

    total_volume = 0
    for c in candles:
        h = c.get("high", c.get("close", 0))
        l = c.get("low", c.get("close", 0))
        v = c.get("volume", 0)
        
        if h == l:
            idx = min(int((h - lowest) / bin_size), num_bins - 1)
            bins[idx] += v
        else:
            start_idx = max(0, int((l - lowest) / bin_size))
            end_idx = min(num_bins - 1, int((h - lowest) / bin_size))
            span = end_idx - start_idx + 1
            vol_per_bin = v / span
            for i in range(start_idx, end_idx + 1):
                bins[i] += vol_per_bin
        total_volume += v

    if total_volume == 0:
        return None

    poc_idx = max(range(len(bins)), key=bins.__getitem__)
    poc = bin_prices[poc_idx]

    va_volume = bins[poc_idx]
    target_va_vol = total_volume * value_area_pct

    upper_idx = poc_idx
    lower_idx = poc_idx

    while va_volume < target_va_vol and (upper_idx < num_bins - 1 or lower_idx > 0):
        up_vol = 0
        if upper_idx < num_bins - 1:
            up_vol = bins[upper_idx + 1] + (bins[upper_idx + 2] if upper_idx < num_bins - 2 else 0)
        
        dn_vol = 0
        if lower_idx > 0:
            dn_vol = bins[lower_idx - 1] + (bins[lower_idx - 2] if lower_idx > 1 else 0)

        if up_vol >= dn_vol and upper_idx < num_bins - 1:
            upper_idx += 1
            va_volume += bins[upper_idx]
        elif lower_idx > 0:
            lower_idx -= 1
            va_volume += bins[lower_idx]
        elif upper_idx < num_bins - 1:
            upper_idx += 1
            va_volume += bins[upper_idx]
        else:
            break

    vah = bin_prices[upper_idx] + (bin_size / 2)
    val = bin_prices[lower_idx] - (bin_size / 2)

    return {
        "poc": poc,
        "vah": vah,
        "val": val
    }


# ---------------------------------------------------------------------------
# Signal chain functions
# ---------------------------------------------------------------------------

def get_nifty_regime(state: Dict[str, Dict[str, Any]]) -> Tuple[str, float, dict]:
    """
    Determine NIFTY regime from market breadth.
    Returns: (regime, breadth_pct, details_dict)
    """
    total = 0
    up = 0
    dn = 0
    flat = 0
    for sym, st in state.items():
        chg = st.get("chg_pct", 0)
        total += 1
        if chg > 0.1:
            up += 1
        elif chg < -0.1:
            dn += 1
        else:
            flat += 1

    if total == 0:
        return "NEUTRAL", 50.0, {"up": 0, "dn": 0, "flat": 0, "total": 0}

    breadth_pct = round(up / total * 100, 1)
    details = {"up": up, "dn": dn, "flat": flat, "total": total, "breadth_pct": breadth_pct}

    if breadth_pct >= 60:
        return "BULLISH", breadth_pct, details
    elif breadth_pct <= 40:
        return "BEARISH", breadth_pct, details
    else:
        return "NEUTRAL", breadth_pct, details


def get_sector_scores(state: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Compute sector-level scores from individual stock data.
    Returns sorted list by abs(avg_chg) descending.
    """
    sectors: Dict[str, list] = {}
    for sym, st in state.items():
        sec = st.get("sector", "OTHER")
        if sec not in sectors:
            sectors[sec] = []
        sectors[sec].append(st)

    results = []
    for sec, stocks in sectors.items():
        if not stocks:
            continue
        chg_sum = sum(s.get("chg_pct", 0) for s in stocks)
        avg_chg = chg_sum / len(stocks)
        up = sum(1 for s in stocks if s.get("chg_pct", 0) > 0.1)
        dn = sum(1 for s in stocks if s.get("chg_pct", 0) < -0.1)

        if avg_chg > 0.5:
            signal = "BULLISH"
        elif avg_chg < -0.5:
            signal = "BEARISH"
        else:
            signal = "NEUTRAL"

        results.append({
            "sector": sec,
            "avg_chg": round(avg_chg, 2),
            "stock_count": len(stocks),
            "up_count": up,
            "dn_count": dn,
            "signal": signal,
        })

    results.sort(key=lambda x: abs(x["avg_chg"]), reverse=True)
    return results


def rank_stocks_for_trade(
    state: Dict[str, Dict[str, Any]],
    regime: str,
    aligned_sectors: List[str],
) -> List[Dict[str, Any]]:
    """
    Rank stocks using WF-validated scoring:
    - WF tier bonus (tier 1 > tier 2 > tier 3 > unranked)
    - Side agreement (live regime matches WF preferred side)
    - Base tradability score
    - OI buildup alignment
    - Volume surge
    - PCR signal alignment
    - Premium viability (>= Rs 15)
    """
    candidates = []
    trade_side = "bull" if regime == "BULLISH" else "bear"

    for sym, st in state.items():
        # --- HARD FILTERS ---
        if st.get("sector", "OTHER") not in aligned_sectors:
            continue
        if sym in BLACKLIST:
            continue
        if not st.get("ltp") or st["ltp"] <= 0:
            continue
        if st.get("atm_strike") is None:
            continue
        if not st.get("prem_ok"):
            continue

        # Premium check for the directional side
        ce_oi_chg = st.get("ce_oi_chg", 0)
        pe_oi_chg = st.get("pe_oi_chg", 0)

        if regime == "BULLISH":
            if ce_oi_chg >= 0 or pe_oi_chg <= 0:
                continue  # Must be CE OI negative, PE OI positive
            entry_prem = st.get("atm_ce", 0)
        else:
            if ce_oi_chg <= 0 or pe_oi_chg >= 0:
                continue  # Must be CE OI positive, PE OI negative
            entry_prem = st.get("atm_pe", 0)

        if not entry_prem or entry_prem < 15:
            continue

        # --- VOLUME PROFILE / VAH-VAL FILTER ---
        # The ultimate test: only enter if price is breaking out/bouncing off Value Area
        candles = st.get("candles", [])
        if not candles:
            continue
            
        vp = calculate_volume_profile(candles)
        if not vp:
            continue
            
        vah, val, poc = vp["vah"], vp["val"], vp["poc"]
        ltp = st["ltp"]
        
        # We want to be within 0.25% of VAH (bullish breakout) or VAL (bearish breakdown)
        # or bouncing off them.
        proximity = 0.0025
        
        valid_setup = False
        vp_reason = ""
        
        if regime == "BULLISH":
            # Bullish trades: Price near VAH (breakout) or bouncing off VAL (support)
            if abs(ltp - vah) / vah <= proximity:
                valid_setup = True
                vp_reason = "VAH Breakout"
            elif abs(ltp - val) / val <= proximity:
                valid_setup = True
                vp_reason = "VAL Bounce"
        else:
            # Bearish trades: Price near VAL (breakdown) or rejecting from VAH (resistance)
            if abs(ltp - val) / val <= proximity:
                valid_setup = True
                vp_reason = "VAL Breakdown"
            elif abs(ltp - vah) / vah <= proximity:
                valid_setup = True
                vp_reason = "VAH Reject"
                
        if not valid_setup:
            continue

        # --- SCORING ---
        rank_score = 0
        reasons = []
        wf_info = WF_STOCKS.get(sym)

        # [WF] Tier bonus — strongest signal
        if wf_info:
            tier = wf_info["tier"]
            if tier == 1:
                rank_score += 25
                reasons.append(f"WF-T1 (PF={wf_info['pf']:.1f})")
            elif tier == 2:
                rank_score += 18
                reasons.append(f"WF-T2 (PF={wf_info['pf']:.1f})")
            elif tier == 3:
                rank_score += 10
                reasons.append(f"WF-T3 (PF={wf_info['pf']:.1f})")

            # [WF] Side agreement — does live direction match WF preferred side?
            if wf_info["side"] == trade_side:
                rank_score += 15
                reasons.append(f"Side-match({wf_info['side']})")
            else:
                # Trading against WF preferred side — penalty
                rank_score -= 10
                reasons.append(f"Side-contra({wf_info['side']})")
        else:
            # Not in WF universe — can still trade if strong signals, but no bonus
            reasons.append("No-WF")

        # Base tradability score (0-100 from server)
        base_score = st.get("score", 0)
        rank_score += base_score * 0.3  # 30% weight (reduced from 40% to make room for WF)
        reasons.append(f"Score {base_score}")

        # Volume surge
        vol_surge = st.get("vol_surge", 0)
        if vol_surge >= 2.0:
            rank_score += 18
            reasons.append(f"Vol {vol_surge:.1f}x")
        elif vol_surge >= 1.5:
            rank_score += 10
            reasons.append(f"Vol {vol_surge:.1f}x")
        elif vol_surge >= 1.2:
            rank_score += 4

        # OI buildup alignment
        buildup = st.get("buildup", "NEUTRAL")
        if regime == "BULLISH" and buildup in ("LONG_BUILD", "SHORT_COVER"):
            rank_score += 18
            reasons.append(f"OI:{buildup}")
        elif regime == "BEARISH" and buildup in ("SHORT_BUILD", "LONG_UNWIND"):
            rank_score += 18
            reasons.append(f"OI:{buildup}")
        elif buildup != "NEUTRAL":
            rank_score -= 5

        # PCR alignment
        pcr_sig = st.get("pcr_sig", "NEUTRAL")
        if regime == "BULLISH" and pcr_sig in ("BULLISH", "MILDLY_BULL"):
            rank_score += 12
            reasons.append(f"PCR:{pcr_sig}")
        elif regime == "BEARISH" and pcr_sig in ("BEARISH", "MILDLY_BEAR"):
            rank_score += 12
            reasons.append(f"PCR:{pcr_sig}")

        # Price momentum alignment
        chg_pct = st.get("chg_pct", 0)
        if regime == "BULLISH" and chg_pct > 0.5:
            rank_score += 8
            reasons.append(f"Mom +{chg_pct:.1f}%")
        elif regime == "BEARISH" and chg_pct < -0.5:
            rank_score += 8
            reasons.append(f"Mom {chg_pct:.1f}%")

        # Range (intraday volatility = opportunity)
        range_pct = st.get("range_pct", 0)
        if range_pct >= 2.0:
            rank_score += 6
        elif range_pct >= 1.0:
            rank_score += 3

        # Premium liquidity bonus
        if entry_prem >= 30:
            rank_score += 4
            reasons.append(f"Prem Rs{entry_prem:.0f}")
        else:
            reasons.append(f"Prem Rs{entry_prem:.0f}")

        candidates.append({
            "symbol": sym,
            "sector": st.get("sector", ""),
            "rank_score": round(rank_score, 1),
            "base_score": base_score,
            "chg_pct": round(chg_pct, 2),
            "vol_surge": round(vol_surge, 1),
            "buildup": buildup,
            "pcr_sig": pcr_sig,
            "entry_prem": round(entry_prem, 2),
            "is_whitelist": sym in WHITELIST,
            "wf_tier": wf_info["tier"] if wf_info else 0,
            "wf_side": wf_info["side"] if wf_info else None,
            "wf_pf": wf_info["pf"] if wf_info else 0,
            "side_match": (wf_info["side"] == trade_side) if wf_info else False,
            "reasons": reasons,
            "atm_strike": st.get("atm_strike"),
            "ltp": st.get("ltp"),
            "lot_size": st.get("lot", 1),
            "vp_reason": vp_reason,
            "vah": vah,
            "val": val,
            "poc": poc,
        })

    candidates.sort(key=lambda x: x["rank_score"], reverse=True)
    return candidates


def pick_itm_strike(
    state: Dict[str, Dict[str, Any]],
    symbol: str,
    regime: str,
) -> Tuple[Optional[float], Optional[float], str]:
    """
    Pick a 1-ITM strike for the directional side.
    CE for BULLISH (strike < spot), PE for BEARISH (strike > spot).
    """
    st = state.get(symbol, {})
    strike_map = st.get("strike_map", {})
    atm_strike = st.get("atm_strike", 0)
    ltp = st.get("ltp", 0)

    if not atm_strike or not ltp:
        if regime == "BULLISH":
            return atm_strike, st.get("atm_ce", 0), "CE"
        else:
            return atm_strike, st.get("atm_pe", 0), "PE"

    strikes = sorted(strike_map.keys())
    if not strikes:
        if regime == "BULLISH":
            return atm_strike, st.get("atm_ce", 0), "CE"
        else:
            return atm_strike, st.get("atm_pe", 0), "PE"

    if regime == "BULLISH":
        itm_strikes = [s for s in strikes if s < atm_strike]
        if itm_strikes:
            best_strike = itm_strikes[-1]
            sd = strike_map.get(best_strike, {})
            prem = sd.get("ce_ltp", 0)
            if prem and prem >= 15:
                return best_strike, prem, "CE"
        return atm_strike, st.get("atm_ce", 0), "CE"

    else:
        itm_strikes = [s for s in strikes if s > atm_strike]
        if itm_strikes:
            best_strike = itm_strikes[0]
            sd = strike_map.get(best_strike, {})
            prem = sd.get("pe_ltp", 0)
            if prem and prem >= 15:
                return best_strike, prem, "PE"
        return atm_strike, st.get("atm_pe", 0), "PE"


def build_trade_reason(
    regime: str,
    regime_details: dict,
    sector_info: dict,
    candidate: dict,
    strike: float,
    option_type: str,
    premium: float,
) -> str:
    """Build human-readable reason string for the trade."""
    lines = []

    breadth = regime_details.get("breadth_pct", 50)
    up = regime_details.get("up", 0)
    dn = regime_details.get("dn", 0)
    total = regime_details.get("total", 0)
    lines.append(f"NIFTY {regime} -- Breadth {breadth:.0f}% ({up}/{total} up, {dn}/{total} dn)")

    sec = candidate.get("sector", "")
    sec_chg = sector_info.get("avg_chg", 0)
    sec_signal = sector_info.get("signal", "NEUTRAL")
    lines.append(f"Sector {sec} {sec_signal} ({sec_chg:+.2f}%) -- aligned with market")

    sym = candidate["symbol"]
    signals = candidate.get("reasons", [])
    lines.append(f"{sym} -- {', '.join(signals)}")

    # WF validation line
    wf_tier = candidate.get("wf_tier", 0)
    wf_side = candidate.get("wf_side")
    wf_pf = candidate.get("wf_pf", 0)
    if wf_tier > 0:
        side_tag = "MATCH" if candidate.get("side_match") else "CONTRA"
        lines.append(f"WF Tier {wf_tier} | Preferred: {wf_side} ({side_tag}) | PF={wf_pf:.1f}")
    else:
        lines.append("WF: Not in validated universe (opportunistic)")

    chg = candidate.get("chg_pct", 0)
    spot = candidate.get("ltp", 0)
    sl_spot = round(spot * SL_PCT / 100, 2)
    lines.append(f"LTP Rs{spot:.2f} ({chg:+.2f}%) | {option_type} {strike:.0f} @ Rs{premium:.2f}")
    lines.append(f"SL: 0.5% spot = Rs{sl_spot:.2f} | Target: 5x = Rs{sl_spot*5:.2f}")
    lines.append(f"Rank Score: {candidate.get('rank_score', 0):.0f}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# AutoPaperTrader class
# ---------------------------------------------------------------------------

class AutoPaperTrader:
    """
    Background task: scans every SCAN_INTERVAL during trading hours,
    enters paper trades when full signal chain aligns.
    """

    SCAN_INTERVAL = 300  # 5 minutes
    MAX_TRADES_PER_DAY = 2
    TRADE_START_HOUR = 9
    TRADE_START_MIN = 30
    TRADE_END_HOUR = 14
    TRADE_END_MIN = 50
    EOD_EXIT_HOUR = 15
    EOD_EXIT_MIN = 15

    def __init__(self, server):
        self.server = server
        self._running = False
        self._today_trades: List[str] = []
        self._today_date: Optional[str] = None
        self._last_scan_result: Optional[dict] = None
        self._scan_count = 0

    def _now_ist(self) -> datetime:
        return datetime.now(IST)

    def _is_trading_window(self) -> bool:
        now = self._now_ist()
        start = now.replace(hour=self.TRADE_START_HOUR, minute=self.TRADE_START_MIN, second=0)
        # End time strictly set to 12:00 PM per Top Pick logic
        end = now.replace(hour=12, minute=0, second=0)
        return start <= now <= end

    def _is_eod_exit_time(self) -> bool:
        now = self._now_ist()
        eod = now.replace(hour=self.EOD_EXIT_HOUR, minute=self.EOD_EXIT_MIN, second=0)
        return now >= eod and now < eod + timedelta(minutes=5)

    def _reset_daily_state(self):
        today = self._now_ist().strftime("%Y-%m-%d")
        if self._today_date != today:
            self._today_date = today
            self._today_trades = []
            log.info("Auto-trader: new day %s, trade counter reset", today)

    def _count_today_auto_trades_for_user(self, user_id: int) -> int:
        """Count auto trades entered today for a specific user."""
        cursor = self.server._db.conn.execute(
            "SELECT COUNT(*) FROM paper_trades WHERE user_id = ? AND trade_type = 'auto' AND date(created_at) = date('now')"
        )
        return cursor.fetchone()[0]

    def _count_today_auto_trades_for_all_users(self) -> int:
        """Count total auto trades entered today across all users."""
        cursor = self.server._db.conn.execute(
            "SELECT COUNT(*) FROM paper_trades WHERE trade_type = 'auto' AND date(created_at) = date('now')"
        )
        return cursor.fetchone()[0]

    def _count_today_auto_trades(self) -> int:
        """Legacy helper: counts total auto trades today across all users."""
        return self._count_today_auto_trades_for_all_users()

    async def _enter_trade(
        self,
        user_id: int,
        user_settings: dict,
        candidate: dict,
        strike: float,
        premium: float,
        option_type: str,
        reason: str,
        regime: str,
    ) -> Optional[dict]:
        """Enter an auto paper trade with WF-validated risk params and persist to SQLite."""
        sym = candidate["symbol"]
        st = self.server.state.get(sym, {})
        lot_size = st.get("lot", 1)
        lots = user_settings.get("default_lots", 1)
        spot = st.get("ltp", 0)

        # WF-optimal risk params: SL = 0.5% spot, TGT = 5x SL
        sl_spot_pts = round(spot * SL_PCT / 100, 2)  # 0.5% of spot in Rs
        # Translate to premium: approximate premium SL as proportional
        delta_est = 0.65
        sl_prem_pts = round(sl_spot_pts * delta_est, 2)
        sl_premium = round(premium - sl_prem_pts, 2)
        if sl_premium < 0:
            sl_premium = round(premium * 0.5, 2)

        tgt_prem_pts = round(sl_prem_pts * TGT_MULT, 2)
        target1 = round(premium + tgt_prem_pts * 0.4, 2)  # T1 at 2x (partial)
        target2 = round(premium + tgt_prem_pts, 2)         # T2 at 5x (full)

        sl_spot_trigger = round(spot - sl_spot_pts, 2) if regime == "BULLISH" else round(spot + sl_spot_pts, 2)

        trade_id = self.server._db.create_paper_trade(
            user_id=user_id,
            symbol=sym,
            direction="BULLISH" if option_type == "CE" else "BEARISH",
            trade_type="auto",
            strike=strike,
            expiry=st.get("expiry"),
            entry_premium=round(premium, 2),
            lots=lots,
            lot_size=lot_size,
            sl_premium=sl_premium,
            sl_spot=sl_spot_trigger,
            t1_premium=target1,
            t2_premium=target2,
            status="ENTERED",
            entry_reason=reason,
            option_type=option_type,
            spot_at_entry=round(spot, 2),
        )

        db_row = self.server._db.get_paper_trade(trade_id)
        if not db_row:
            log.error("Failed to retrieve created auto trade ID: %s", trade_id)
            return None

        trade = self.server._db_row_to_memory_trade(db_row)
        self.server.paper_trades.append(trade)

        log.info("AUTO PAPER TRADE (User %d): %s %s %s %s @ Rs%.2f | SL Rs%.2f (spot %s) | T1 Rs%.2f | T2 Rs%.2f | WF-T%d | Rank %.0f",
                 user_id, trade["id"], sym, option_type, strike, premium, sl_premium, sl_spot_trigger,
                 target1, target2, candidate.get("wf_tier", 0), candidate.get("rank_score", 0))
        log.info("  Reason: %s", reason.replace("\n", " | "))

        await self.server._broadcast_paper_trades()
        await self.server._broadcast_to_user(user_id, {
            "type": "auto_trade",
            "trade": self.server._paper_trade_to_dict(trade),
            "ts": time.time(),
        })

        return trade

    async def _eod_exit_all(self):
        """Exit all open paper trades at EOD."""
        open_trades = [t for t in self.server.paper_trades if t["status"] == "OPEN"]
        if not open_trades:
            return

        exited = []
        for trade in open_trades:
            exit_prem = self.server._get_strike_premium(trade)
            if exit_prem <= 0:
                exit_prem = trade["entry_premium"]

            qty = trade["lot_size"] * trade.get("lots", 1)
            pnl = round((exit_prem - trade["entry_premium"]) * qty, 2)
            pnl_pct = round((pnl / (trade["entry_premium"] * qty) * 100), 2) if (trade["entry_premium"] * qty) != 0 else 0.0
            costs_estimated = 40.0
            net_pnl = round(pnl - costs_estimated, 2)

            self.server._db.update_paper_trade(
                trade["id"],
                status="EXITED",
                exit_premium=exit_prem,
                exit_reason="EOD_EXIT",
                pnl=pnl,
                pnl_pct=pnl_pct,
                costs_estimated=costs_estimated,
                net_pnl=net_pnl,
                exited_at=datetime.utcnow().isoformat(),
            )

            trade["status"] = "CLOSED"
            trade["exit_time"] = datetime.now(timezone.utc).isoformat()
            trade["exit_premium"] = round(exit_prem, 2)
            trade["exit_reason"] = "EOD_EXIT"
            trade["final_pnl"] = pnl
            exited.append(trade)

            log.info("AUTO EOD EXIT: %s %s exit=%.2f pnl=%.2f",
                     trade["id"], trade["symbol"], exit_prem, pnl)

        if exited:
            exited_ids = {t["id"] for t in exited}
            self.server.paper_trades = [t for t in self.server.paper_trades if t["id"] not in exited_ids]
            await self.server._broadcast_paper_trades()
            
            # Send paper_exit message to each user individually
            user_exits = {}
            for t in exited:
                u_id = t["user_id"]
                if u_id not in user_exits:
                    user_exits[u_id] = []
                user_exits[u_id].append(t)
            for u_id, u_trades in user_exits.items():
                await self.server._broadcast_to_user(u_id, {
                    "type": "paper_exit",
                    "trades": [self.server._paper_trade_to_dict(t) for t in u_trades],
                    "reason": "EOD_AUTO_EXIT",
                    "ts": time.time(),
                })

    async def scan_and_trade(self) -> dict:
        """Run one full scan cycle."""
        self._scan_count += 1
        self._reset_daily_state()

        result = {
            "scan_time": self._now_ist().strftime("%H:%M:%S"),
            "scan_number": self._scan_count,
            "regime": None,
            "sectors_scanned": 0,
            "candidates_found": 0,
            "trades_entered": [],
            "skip_reason": None,
        }

        active_users = self.server._db.get_all_auto_trade_settings()
        if not active_users:
            result["skip_reason"] = "No active users with auto-trading enabled"
            self._last_scan_result = result
            return result

        if not self.server.state or len(self.server.state) < 50:
            result["skip_reason"] = "Insufficient data (< 50 stocks loaded)"
            self._last_scan_result = result
            return result

        chain_count = sum(1 for s in self.server.state.values() if s.get("atm_strike") is not None)
        if chain_count < 20:
            result["skip_reason"] = f"Chain data sparse ({chain_count} stocks)"
            self._last_scan_result = result
            return result

        # Step 1: NIFTY Regime
        regime, breadth, regime_details = get_nifty_regime(self.server.state)
        result["regime"] = regime
        result["breadth"] = breadth
        result["regime_details"] = regime_details

        if regime == "NEUTRAL":
            result["skip_reason"] = f"Market NEUTRAL (breadth {breadth:.0f}%) -- no signal"
            self._last_scan_result = result
            return result

        # Step 2: Sector alignment
        sector_scores = get_sector_scores(self.server.state)
        result["sectors_scanned"] = len(sector_scores)

        if regime == "BULLISH":
            aligned_sectors = [s["sector"] for s in sector_scores if s["avg_chg"] > 0.3]
        else:
            aligned_sectors = [s["sector"] for s in sector_scores if s["avg_chg"] < -0.3]

        if not aligned_sectors:
            result["skip_reason"] = f"No sectors aligned with {regime} regime"
            self._last_scan_result = result
            return result

        result["aligned_sectors"] = aligned_sectors

        # Step 3: Rank stocks (WF-enhanced scoring)
        candidates = rank_stocks_for_trade(self.server.state, regime, aligned_sectors)
        result["candidates_found"] = len(candidates)

        if not candidates:
            result["skip_reason"] = "No viable candidates after filtering"
            self._last_scan_result = result
            return result

        result["top_candidates"] = candidates[:10]

        # Step 4: Enter trades for active users
        for user_conf in active_users:
            user_id = user_conf["user_id"]
            max_pos = user_conf.get("auto_trade_max_positions", 2)
            today_count = self._count_today_auto_trades_for_user(user_id)
            if today_count >= max_pos:
                continue

            slots_available = max_pos - today_count
            entered = 0

            for cand in candidates:
                if entered >= slots_available:
                    break

                sym = cand["symbol"]

                # Check if this user already traded this symbol today (open or closed)
                cursor = self.server._db.conn.execute(
                    "SELECT COUNT(*) FROM paper_trades WHERE user_id = ? AND symbol = ? AND date(created_at) = date('now')",
                    (user_id, sym)
                )
                if cursor.fetchone()[0] > 0:
                    continue

                # Check if this user currently has this symbol open
                if any(t["symbol"] == sym and t.get("user_id") == user_id and t["status"] == "OPEN" for t in self.server.paper_trades):
                    continue

                strike, premium, option_type = pick_itm_strike(self.server.state, sym, regime)
                if not premium or premium < 15:
                    continue

                sec = cand.get("sector", "")
                sector_info_match = next(
                    (s for s in sector_scores if s["sector"] == sec),
                    {"avg_chg": 0, "signal": "NEUTRAL"}
                )

                reason = build_trade_reason(
                    regime=regime,
                    regime_details=regime_details,
                    sector_info=sector_info_match,
                    candidate=cand,
                    strike=strike,
                    option_type=option_type,
                    premium=premium,
                )

                trade = await self._enter_trade(
                    user_id=user_id,
                    user_settings=user_conf,
                    candidate=cand,
                    strike=strike,
                    premium=premium,
                    option_type=option_type,
                    reason=reason,
                    regime=regime,
                )

                if trade:
                    result["trades_entered"].append({
                        "id": trade["id"],
                        "user_id": user_id,
                        "symbol": sym,
                        "option_type": option_type,
                        "strike": strike,
                        "premium": premium,
                        "rank_score": cand["rank_score"],
                        "wf_tier": cand.get("wf_tier", 0),
                        "side_match": cand.get("side_match", False),
                        "reason_summary": cand["reasons"],
                    })
                    entered += 1

        self._last_scan_result = result
        return result

    async def run(self):
        """Main loop: scan every SCAN_INTERVAL during trading hours."""
        self._running = True
        log.info("Auto paper trader started (scan every %ds, max %d/day, SL=%.1f%% spot, TGT=%.0fx)",
                 self.SCAN_INTERVAL, self.MAX_TRADES_PER_DAY, SL_PCT, TGT_MULT)
        log.info("WF universe: %d stocks (%d T1, %d T2, %d T3) | Blacklist: %d",
                 len(WF_STOCKS),
                 sum(1 for v in WF_STOCKS.values() if v["tier"] == 1),
                 sum(1 for v in WF_STOCKS.values() if v["tier"] == 2),
                 sum(1 for v in WF_STOCKS.values() if v["tier"] == 3),
                 len(BLACKLIST))

        await asyncio.sleep(60)

        while self._running:
            try:
                if not self.server._settings.get("auto_trade_enabled", True):
                    await asyncio.sleep(30)
                    continue

                if self._is_eod_exit_time():
                    await self._eod_exit_all()
                    await asyncio.sleep(360)
                    continue

                if self._is_trading_window():
                    result = await self.scan_and_trade()
                    if result.get("trades_entered"):
                        log.info("Scan #%d: entered %d trades",
                                 result["scan_number"], len(result["trades_entered"]))
                    elif result.get("skip_reason"):
                        log.info("Scan #%d: %s", result["scan_number"], result["skip_reason"])

                await asyncio.sleep(self.SCAN_INTERVAL)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.error("Auto paper trader error: %s", exc, exc_info=True)
                await asyncio.sleep(60)

        log.info("Auto paper trader stopped")

    async def stop(self):
        self._running = False

    def get_status(self) -> dict:
        """Return status for admin/API."""
        wf_summary = {
            "total_stocks": len(WF_STOCKS),
            "tier_1": sum(1 for v in WF_STOCKS.values() if v["tier"] == 1),
            "tier_2": sum(1 for v in WF_STOCKS.values() if v["tier"] == 2),
            "tier_3": sum(1 for v in WF_STOCKS.values() if v["tier"] == 3),
            "blacklist_size": len(BLACKLIST),
            "sl_pct": SL_PCT,
            "tgt_mult": TGT_MULT,
        }
        return {
            "enabled": self.server._settings.get("auto_trade_enabled", True),
            "running": self._running,
            "scan_count": self._scan_count,
            "today_auto_trades": self._count_today_auto_trades(),
            "max_per_day": self.MAX_TRADES_PER_DAY,
            "in_trading_window": self._is_trading_window(),
            "last_scan": self._last_scan_result,
            "wf_config": wf_summary,
        }
