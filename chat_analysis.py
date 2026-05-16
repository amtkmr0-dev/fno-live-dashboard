#!/usr/bin/env python3
"""
chat_analysis.py — Deep analysis engine for Quantra AI chat.

Fetches live data from Upstox API, computes technicals (RSI, MACD, EMA),
option chain analysis (OI walls, PCR, max pain, IV skew, buildup), and
returns structured text for LLM context injection.

Called by auth_proxy.py before sending to AI provider.

Architecture:
  User question → auth_proxy → chat_analysis → Upstox API
                                                     ↓
                                               Fetch chain, candles, OI
                                                     ↓
                                               Compute analysis
                                                     ↓
                                               Structured text → LLM
"""

import asyncio
import gzip
import csv
import io
import json
import logging
import os
import re
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Optional, Union

import aiohttp

log = logging.getLogger("chat_analysis")

UPSTOX_BASE = "https://api.upstox.com"
INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.csv.gz"
OI_NOISE_THRESHOLD = 5000

# In-memory caches (reset on restart)
_instruments_cache = {}        # symbol → {equity_key, name, lot_size, ...}
_instruments_cache_date = None  # YYYY-MM-DD string

INDEX_KEYS = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "FINNIFTY": "NSE_INDEX|Nifty Fin Service",
    "INDIAVIX": "NSE_INDEX|India VIX",
}


# ============================================================
# TOKEN
# ============================================================

def get_upstox_token():
    """Read Upstox access token from config.env or env var."""
    # Try config.env (same as ws_server)
    for fname in ("config.env", "../config.env"):
        if os.path.exists(fname):
            with open(fname) as f:
                for line in f:
                    line = line.strip()
                    if "=" in line and not line.startswith("#"):
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip('"').strip("'")
                        if k in ("UPSTOX_ACCESS_TOKEN", "ACCESS_TOKEN", "UPSTOX_TOKEN"):
                            if v:
                                return v
    # Try auth_config.json
    if os.path.exists("auth_config.json"):
        try:
            with open("auth_config.json") as f:
                cfg = json.load(f)
            if cfg.get("upstox_token"):
                return cfg["upstox_token"]
        except Exception:
            pass
    return os.environ.get("UPSTOX_ACCESS_TOKEN", "")


def _headers(token):
    return {
        "Accept": "application/json",
        "Api-Version": "2.0",
        "Authorization": f"Bearer {token}",
    }


# ============================================================
# INSTRUMENTS LOOKUP (symbol → instrument key)
# ============================================================

async def _load_instruments(session):
    """Download and parse NSE instruments CSV. Cached for the day."""
    global _instruments_cache, _instruments_cache_date
    today = datetime.now().strftime("%Y-%m-%d")
    if _instruments_cache_date == today and _instruments_cache:
        return _instruments_cache

    cache_path = f"/tmp/.upstox_nse_{today.replace('-','')}.csv"

    # Try local cache first
    if os.path.exists(cache_path):
        with open(cache_path, "r") as f:
            raw_csv = f.read()
    else:
        log.info("Downloading NSE instruments CSV...")
        async with session.get(INSTRUMENTS_URL, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status != 200:
                log.error(f"Failed to download instruments: {resp.status}")
                return _instruments_cache
            gz_data = await resp.read()
        raw_csv = gzip.decompress(gz_data).decode("utf-8")
        with open(cache_path, "w") as f:
            f.write(raw_csv)

    reader = csv.DictReader(io.StringIO(raw_csv))
    eq_map = {}   # name → {instrument_key, tradingsymbol}
    fut_map = {}  # name → {instrument_key, lot_size, expiry}

    for row in reader:
        itype = (row.get("instrument_type") or "").upper()
        exchange = row.get("exchange") or ""
        name = (row.get("name") or "").upper().strip()
        ikey = row.get("instrument_key") or ""
        tsym = (row.get("tradingsymbol") or "").upper().strip()

        if exchange == "NSE_EQ" and itype in ("EQ", ""):
            eq_map[name] = {"instrument_key": ikey, "tradingsymbol": tsym}

        if exchange == "NSE_FO" and itype in ("FUTSTK", "FUT"):
            lot = 0
            try:
                lot = int(float(row.get("lot_size") or 0))
            except ValueError:
                pass
            expiry = row.get("expiry") or ""
            if name not in fut_map or expiry < fut_map[name].get("expiry", "9999"):
                fut_map[name] = {
                    "instrument_key": ikey,
                    "lot_size": lot,
                    "expiry": expiry,
                    "tradingsymbol": tsym,
                }

    # Join eq + fut by company name
    result = {}
    for name, fdata in fut_map.items():
        # Derive ticker from FUTSTK tradingsymbol: RELIANCE26MAYFUT → RELIANCE
        ticker = fdata["tradingsymbol"]
        if ticker.endswith("FUT"):
            ticker = ticker[:-3]
        if len(ticker) > 5:
            ticker = ticker[:-5]  # strip YYMMMM

        eq = eq_map.get(name, {})
        result[ticker] = {
            "symbol": ticker,
            "equity_key": eq.get("instrument_key", ""),
            "lot_size": fdata.get("lot_size", 0),
            "nearest_expiry": fdata.get("expiry", ""),
        }

    _instruments_cache = result
    _instruments_cache_date = today
    log.info(f"Loaded {len(result)} F&O instruments")
    return result


async def resolve_instrument_key(session, symbol):
    """Resolve a stock symbol to its NSE_EQ instrument key."""
    instruments = await _load_instruments(session)
    sym = symbol.upper().strip()
    if sym in instruments:
        return instruments[sym].get("equity_key", ""), instruments[sym].get("lot_size", 0)
    # Fuzzy: try partial match
    for k, v in instruments.items():
        if k.startswith(sym) or sym.startswith(k):
            return v.get("equity_key", ""), v.get("lot_size", 0)
    return "", 0


# ============================================================
# UPSTOX API FETCHERS (async)
# ============================================================

async def fetch_option_chain(session, symbol, token):
    """Fetch full option chain for an F&O stock/index."""
    h = _headers(token)

    # Determine instrument key for chain
    sym = symbol.upper().strip()
    if sym in INDEX_KEYS:
        chain_key = INDEX_KEYS[sym]
    else:
        chain_key = f"NSE_FO|{sym}"  # Try direct first

    # Get nearest expiry
    url = f"{UPSTOX_BASE}/v2/option/contract"
    async with session.get(url, headers=h, params={"instrument_key": chain_key}, timeout=aiohttp.ClientTimeout(total=20)) as resp:
        if resp.status == 401:
            return {"error": "token_expired"}
        if resp.status != 200:
            # Try alternative key format using lookup
            eq_key, _ = await resolve_instrument_key(session, sym)
            if not eq_key:
                return {"error": f"Cannot resolve instrument key for {sym}"}
            chain_key = eq_key.replace("NSE_EQ|", "NSE_FO|")
            # Retry
            async with session.get(url, headers=h, params={"instrument_key": chain_key}, timeout=aiohttp.ClientTimeout(total=20)) as resp2:
                if resp2.status != 200:
                    return {"error": f"Chain contracts failed: {resp2.status}"}
                contracts = (await resp2.json()).get("data", [])
        else:
            contracts = (await resp.json()).get("data", [])

    if not contracts:
        return {"error": f"No expiries found for {sym}"}

    # Use nearest expiry
    expiries = sorted(set(c.get("expiry") or c for c in contracts if c))
    if not expiries:
        return {"error": "No expiry dates"}
    expiry = expiries[0]

    # Fetch chain
    chain_url = f"{UPSTOX_BASE}/v2/option/chain"
    async with session.get(chain_url, headers=h,
                           params={"instrument_key": chain_key, "expiry_date": expiry},
                           timeout=aiohttp.ClientTimeout(total=30)) as resp:
        if resp.status != 200:
            return {"error": f"Chain fetch failed: {resp.status}"}
        data = (await resp.json()).get("data", [])

    if not data:
        return {"error": "Empty chain data"}

    # Parse chain
    spot = data[0].get("underlying_spot_price", 0)
    strikes = []
    total_ce_oi = 0
    total_pe_oi = 0
    total_ce_vol = 0
    total_pe_vol = 0

    for r in data:
        strike = r.get("strike_price", 0)
        ce = r.get("call_options", {}).get("market_data", {})
        pe = r.get("put_options", {}).get("market_data", {})
        ce_g = r.get("call_options", {}).get("option_greeks", {})
        pe_g = r.get("put_options", {}).get("option_greeks", {})

        ce_oi = ce.get("oi", 0) or 0
        pe_oi = pe.get("oi", 0) or 0
        ce_prev_oi = ce.get("prev_oi", 0) or 0
        pe_prev_oi = pe.get("prev_oi", 0) or 0
        total_ce_oi += ce_oi
        total_pe_oi += pe_oi
        total_ce_vol += (ce.get("volume", 0) or 0)
        total_pe_vol += (pe.get("volume", 0) or 0)

        strikes.append({
            "strike": strike,
            "ce_ltp": ce.get("ltp", 0) or 0,
            "ce_oi": ce_oi,
            "ce_oi_chg": ce_oi - ce_prev_oi,
            "ce_vol": ce.get("volume", 0) or 0,
            "ce_iv": ce_g.get("iv", 0) or ce.get("iv", 0) or 0,
            "ce_delta": ce_g.get("delta", 0) or 0,
            "pe_ltp": pe.get("ltp", 0) or 0,
            "pe_oi": pe_oi,
            "pe_oi_chg": pe_oi - pe_prev_oi,
            "pe_vol": pe.get("volume", 0) or 0,
            "pe_iv": pe_g.get("iv", 0) or pe.get("iv", 0) or 0,
            "pe_delta": pe_g.get("delta", 0) or 0,
        })

    # Compute derived metrics
    pcr = round(total_pe_oi / total_ce_oi, 3) if total_ce_oi > 0 else 0
    vol_pcr = round(total_pe_vol / total_ce_vol, 3) if total_ce_vol > 0 else 0

    # ATM strike
    atm_strike = min(strikes, key=lambda s: abs(s["strike"] - spot))["strike"] if strikes else 0

    # Max pain
    max_pain = _compute_max_pain(strikes, spot) if strikes else 0

    # OI walls (top 3 by OI on each side)
    sorted_ce = sorted(strikes, key=lambda s: s["ce_oi"], reverse=True)[:3]
    sorted_pe = sorted(strikes, key=lambda s: s["pe_oi"], reverse=True)[:3]
    ce_walls = [{"strike": s["strike"], "oi": s["ce_oi"]} for s in sorted_ce]
    pe_walls = [{"strike": s["strike"], "oi": s["pe_oi"]} for s in sorted_pe]

    # OI change walls (where OI is building today)
    ce_build = sorted([s for s in strikes if s["ce_oi_chg"] > OI_NOISE_THRESHOLD],
                       key=lambda s: s["ce_oi_chg"], reverse=True)[:3]
    pe_build = sorted([s for s in strikes if s["pe_oi_chg"] > OI_NOISE_THRESHOLD],
                       key=lambda s: s["pe_oi_chg"], reverse=True)[:3]

    # ATM IV
    atm_data = next((s for s in strikes if s["strike"] == atm_strike), None)
    atm_iv = round((atm_data["ce_iv"] + atm_data["pe_iv"]) / 2, 2) if atm_data else 0
    atm_ce_prem = atm_data["ce_ltp"] if atm_data else 0
    atm_pe_prem = atm_data["pe_ltp"] if atm_data else 0

    # IV skew (25-delta)
    iv_skew = 0
    otm_puts = [s for s in strikes if s["strike"] < spot and abs(s["pe_delta"]) > 0.15 and abs(s["pe_delta"]) < 0.35]
    otm_calls = [s for s in strikes if s["strike"] > spot and abs(s["ce_delta"]) > 0.15 and abs(s["ce_delta"]) < 0.35]
    if otm_puts and otm_calls:
        avg_put_iv = sum(s["pe_iv"] for s in otm_puts) / len(otm_puts)
        avg_call_iv = sum(s["ce_iv"] for s in otm_calls) / len(otm_calls)
        iv_skew = round(avg_put_iv - avg_call_iv, 2)

    # Buildup classification
    net_ce_oi_chg = sum(s["ce_oi_chg"] for s in strikes)
    net_pe_oi_chg = sum(s["pe_oi_chg"] for s in strikes)

    # Strike-level detail for ATM ± 5 strikes
    atm_idx = next((i for i, s in enumerate(strikes) if s["strike"] == atm_strike), len(strikes)//2)
    nearby = strikes[max(0, atm_idx-5):atm_idx+6]

    return {
        "ok": True,
        "symbol": sym,
        "expiry": expiry,
        "spot": spot,
        "atm_strike": atm_strike,
        "atm_iv": atm_iv,
        "atm_ce": atm_ce_prem,
        "atm_pe": atm_pe_prem,
        "pcr": pcr,
        "vol_pcr": vol_pcr,
        "total_ce_oi": total_ce_oi,
        "total_pe_oi": total_pe_oi,
        "max_pain": max_pain,
        "max_pain_dist_pct": round((spot - max_pain) / spot * 100, 2) if spot else 0,
        "ce_walls": ce_walls,
        "pe_walls": pe_walls,
        "ce_oi_building": [{"strike": s["strike"], "oi_chg": s["ce_oi_chg"]} for s in ce_build],
        "pe_oi_building": [{"strike": s["strike"], "oi_chg": s["pe_oi_chg"]} for s in pe_build],
        "iv_skew_25d": iv_skew,
        "net_ce_oi_chg": net_ce_oi_chg,
        "net_pe_oi_chg": net_pe_oi_chg,
        "nearby_strikes": nearby,
        "total_strikes": len(strikes),
    }


def _compute_max_pain(strikes, spot):
    """Compute max pain strike."""
    min_pain = float("inf")
    max_pain_strike = spot
    for candidate in strikes:
        total_pain = 0
        for s in strikes:
            if s["strike"] < candidate["strike"]:
                total_pain += s["ce_oi"] * (candidate["strike"] - s["strike"])
            elif s["strike"] > candidate["strike"]:
                total_pain += s["pe_oi"] * (s["strike"] - candidate["strike"])
        if total_pain < min_pain:
            min_pain = total_pain
            max_pain_strike = candidate["strike"]
    return max_pain_strike


async def fetch_candles(session, instrument_key, interval, token, days=5):
    """Fetch candles — intraday (v3 preferred) or historical."""
    h = _headers(token)
    key_enc = urllib.parse.quote(instrument_key, safe="")
    candles = []

    intraday_intervals = {"1minute": 1, "5minute": 5, "15minute": 15, "30minute": 30}
    if interval in intraday_intervals:
        mins = intraday_intervals[interval]
        url = f"{UPSTOX_BASE}/v3/historical-candle/intraday/{key_enc}/minutes/{mins}"
        async with session.get(url, headers=h, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status == 200:
                candles = (await resp.json()).get("data", {}).get("candles", [])
            else:
                # v2 fallback
                url2 = f"{UPSTOX_BASE}/v2/historical-candle/intraday/{key_enc}/{interval}"
                async with session.get(url2, headers=h, timeout=aiohttp.ClientTimeout(total=20)) as resp2:
                    if resp2.status == 200:
                        candles = (await resp2.json()).get("data", {}).get("candles", [])
    else:
        # Historical
        today = datetime.now().strftime("%Y-%m-%d")
        from_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        url = f"{UPSTOX_BASE}/v2/historical-candle/{key_enc}/{interval}/{today}/{from_date}"
        async with session.get(url, headers=h, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status == 200:
                candles = (await resp.json()).get("data", {}).get("candles", [])

    # Parse: [ts, o, h, l, c, vol, oi]
    result = []
    for c in candles:
        if len(c) < 6:
            continue
        result.append({
            "ts": c[0],
            "open": c[1],
            "high": c[2],
            "low": c[3],
            "close": c[4],
            "volume": c[5],
            "oi": c[6] if len(c) > 6 else None,
        })
    # Sort chronologically (Upstox returns newest first)
    result.sort(key=lambda x: x["ts"])
    return result


async def fetch_quote(session, instrument_keys, token):
    """Fetch full quote for one or more instruments."""
    h = _headers(token)
    keys_str = ",".join(instrument_keys)
    url = f"{UPSTOX_BASE}/v2/market-quote/quotes"
    async with session.get(url, headers=h, params={"instrument_key": keys_str},
                           timeout=aiohttp.ClientTimeout(total=15)) as resp:
        if resp.status != 200:
            return {}
        data = (await resp.json()).get("data", {})
    return data


# ============================================================
# TECHNICAL INDICATORS
# ============================================================

def ema(values, period):
    """Compute EMA."""
    out = [None] * len(values)
    if len(values) < period:
        return out
    k = 2.0 / (period + 1)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    for i in range(period, len(values)):
        prev = out[i - 1]
        if prev is None:
            out[i] = None
        else:
            out[i] = values[i] * k + prev * (1.0 - k)
    return out


def rsi(closes, period=14):
    """Compute RSI (Wilder smoothing)."""
    n = len(closes)
    out = [None] * n
    if n <= period:
        return out
    gains, losses = [], []
    for i in range(1, n):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def _calc(ag, al):
        if al == 0:
            return 100.0
        return 100.0 - (100.0 / (1.0 + ag / al))

    out[period] = _calc(avg_gain, avg_loss)
    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
        out[i] = _calc(avg_gain, avg_loss)
    return out


def macd(closes, fast=12, slow=26, signal=9):
    """Compute MACD line, signal line, histogram."""
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    macd_line = []
    for f, s in zip(ema_fast, ema_slow):
        if f is not None and s is not None:
            macd_line.append(f - s)
        else:
            macd_line.append(None)
    # Signal line (EMA of MACD values that are not None)
    valid_macd = [v for v in macd_line if v is not None]
    sig = ema(valid_macd, signal) if len(valid_macd) >= signal else [None] * len(valid_macd)
    # Align back
    signal_line = [None] * len(macd_line)
    j = 0
    for i, v in enumerate(macd_line):
        if v is not None:
            signal_line[i] = sig[j] if j < len(sig) else None
            j += 1
    histogram = []
    for m, s in zip(macd_line, signal_line):
        if m is not None and s is not None:
            histogram.append(m - s)
        else:
            histogram.append(None)
    return macd_line, signal_line, histogram


def compute_technicals(candles):
    """Compute full technical analysis from candle data."""
    if len(candles) < 20:
        return {"error": "Insufficient candle data"}

    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    volumes = [c["volume"] for c in candles]

    # RSI
    rsi_values = rsi(closes, 14)
    current_rsi = next((v for v in reversed(rsi_values) if v is not None), None)

    # EMA
    ema_9 = ema(closes, 9)
    ema_21 = ema(closes, 21)
    ema_50 = ema(closes, 50) if len(closes) >= 50 else [None] * len(closes)

    current_ema9 = next((v for v in reversed(ema_9) if v is not None), None)
    current_ema21 = next((v for v in reversed(ema_21) if v is not None), None)
    current_ema50 = next((v for v in reversed(ema_50) if v is not None), None)

    # MACD
    macd_line, signal_line, histogram = macd(closes)
    current_macd = next((v for v in reversed(macd_line) if v is not None), None)
    current_signal = next((v for v in reversed(signal_line) if v is not None), None)
    current_hist = next((v for v in reversed(histogram) if v is not None), None)

    # MACD crossover detection
    macd_cross = "none"
    valid_pairs = [(m, s) for m, s in zip(macd_line, signal_line) if m is not None and s is not None]
    if len(valid_pairs) >= 2:
        prev_m, prev_s = valid_pairs[-2]
        curr_m, curr_s = valid_pairs[-1]
        if prev_m <= prev_s and curr_m > curr_s:
            macd_cross = "bullish_crossover"
        elif prev_m >= prev_s and curr_m < curr_s:
            macd_cross = "bearish_crossover"

    # EMA trend
    ema_trend = "neutral"
    if current_ema9 and current_ema21:
        if current_ema9 > current_ema21:
            ema_trend = "bullish"
        elif current_ema9 < current_ema21:
            ema_trend = "bearish"

    # Support/Resistance from recent price action
    recent = candles[-20:]
    recent_high = max(c["high"] for c in recent)
    recent_low = min(c["low"] for c in recent)
    current_price = closes[-1]

    # Volume analysis
    avg_vol = sum(volumes[-20:]) / min(len(volumes), 20) if volumes else 0
    current_vol = volumes[-1] if volumes else 0
    vol_ratio = round(current_vol / avg_vol, 2) if avg_vol > 0 else 0

    # VWAP (intraday approximation)
    vwap = None
    if len(candles) > 1:
        cum_vol = 0
        cum_tp_vol = 0
        for c in candles:
            tp = (c["high"] + c["low"] + c["close"]) / 3
            cum_vol += c["volume"]
            cum_tp_vol += tp * c["volume"]
        vwap = round(cum_tp_vol / cum_vol, 2) if cum_vol > 0 else None

    return {
        "current_price": current_price,
        "rsi_14": round(current_rsi, 2) if current_rsi else None,
        "ema_9": round(current_ema9, 2) if current_ema9 else None,
        "ema_21": round(current_ema21, 2) if current_ema21 else None,
        "ema_50": round(current_ema50, 2) if current_ema50 else None,
        "ema_trend": ema_trend,
        "macd": round(current_macd, 4) if current_macd else None,
        "macd_signal": round(current_signal, 4) if current_signal else None,
        "macd_histogram": round(current_hist, 4) if current_hist else None,
        "macd_crossover": macd_cross,
        "vwap": vwap,
        "price_vs_vwap": "above" if (vwap and current_price > vwap) else "below" if (vwap and current_price < vwap) else "at",
        "vol_ratio": vol_ratio,
        "recent_high": recent_high,
        "recent_low": recent_low,
        "range_position_pct": round((current_price - recent_low) / (recent_high - recent_low) * 100, 1) if recent_high != recent_low else 50,
        "candle_count": len(candles),
    }


# ============================================================
# OI BUILDUP ANALYSIS
# ============================================================

def classify_buildup(price_chg, oi_chg):
    """Classify OI buildup pattern."""
    if price_chg >= 0 and oi_chg >= 0:
        return "LONG_BUILD"
    if price_chg < 0 and oi_chg >= 0:
        return "SHORT_BUILD"
    if price_chg < 0 and oi_chg < 0:
        return "LONG_UNWIND"
    return "SHORT_COVER"


def analyze_oi_pattern(chain_data):
    """Deeper OI pattern analysis from chain data."""
    if not chain_data.get("ok"):
        return {}

    nearby = chain_data.get("nearby_strikes", [])
    if not nearby:
        return {}

    # CE vs PE OI distribution around ATM
    atm = chain_data.get("atm_strike", 0)
    itm_ce_oi = sum(s["ce_oi"] for s in nearby if s["strike"] < atm)
    otm_ce_oi = sum(s["ce_oi"] for s in nearby if s["strike"] > atm)
    itm_pe_oi = sum(s["pe_oi"] for s in nearby if s["strike"] > atm)
    otm_pe_oi = sum(s["pe_oi"] for s in nearby if s["strike"] < atm)

    # Where is new OI concentrating today?
    ce_build_strikes = [(s["strike"], s["ce_oi_chg"]) for s in nearby if s["ce_oi_chg"] > OI_NOISE_THRESHOLD]
    pe_build_strikes = [(s["strike"], s["pe_oi_chg"]) for s in nearby if s["pe_oi_chg"] > OI_NOISE_THRESHOLD]

    # Resistance = CE OI walls above spot, Support = PE OI walls below spot
    spot = chain_data.get("spot", 0)
    resistance_levels = sorted(
        [(s["strike"], s["ce_oi"]) for s in nearby if s["strike"] > spot and s["ce_oi"] > 0],
        key=lambda x: x[1], reverse=True
    )[:3]
    support_levels = sorted(
        [(s["strike"], s["pe_oi"]) for s in nearby if s["strike"] < spot and s["pe_oi"] > 0],
        key=lambda x: x[1], reverse=True
    )[:3]

    return {
        "resistance_from_oi": [{"strike": r[0], "ce_oi": r[1]} for r in resistance_levels],
        "support_from_oi": [{"strike": s[0], "pe_oi": s[1]} for s in support_levels],
        "ce_building_at": [{"strike": s[0], "oi_chg": s[1]} for s in ce_build_strikes],
        "pe_building_at": [{"strike": s[0], "oi_chg": s[1]} for s in pe_build_strikes],
        "net_ce_oi_chg": chain_data.get("net_ce_oi_chg", 0),
        "net_pe_oi_chg": chain_data.get("net_pe_oi_chg", 0),
        "oi_interpretation": _interpret_oi(chain_data),
    }


def _interpret_oi(chain):
    """Natural language interpretation of OI patterns."""
    parts = []
    pcr = chain.get("pcr", 0)
    if pcr > 1.2:
        parts.append(f"PCR {pcr} is strongly PUT-heavy — bulls have support")
    elif pcr > 0.9:
        parts.append(f"PCR {pcr} is balanced-to-bullish")
    elif pcr > 0.6:
        parts.append(f"PCR {pcr} is mildly bearish — CALL writers dominating")
    else:
        parts.append(f"PCR {pcr} is very bearish — heavy CALL writing")

    mp_dist = chain.get("max_pain_dist_pct", 0)
    if abs(mp_dist) > 1:
        direction = "above" if mp_dist > 0 else "below"
        parts.append(f"Spot is {abs(mp_dist):.1f}% {direction} max pain {chain.get('max_pain')} — gravity pull {'down' if mp_dist > 0 else 'up'}")

    iv_skew = chain.get("iv_skew_25d", 0)
    if iv_skew > 3:
        parts.append(f"IV skew +{iv_skew}% — puts are expensive (downside fear)")
    elif iv_skew < -3:
        parts.append(f"IV skew {iv_skew}% — calls are expensive (upside demand)")

    return "; ".join(parts)


# ============================================================
# HIGH-LEVEL ANALYSIS FUNCTIONS
# ============================================================

async def deep_analyze_stock(symbol, token=None):
    """Full deep analysis of a single stock: chain + technicals + OI."""
    if not token:
        token = get_upstox_token()
    if not token:
        return {"error": "No Upstox token configured"}

    sym = symbol.upper().strip()

    async with aiohttp.ClientSession() as session:
        # Resolve instrument key
        eq_key, lot_size = await resolve_instrument_key(session, sym)

        # Parallel fetch: chain + intraday candles + daily candles
        tasks = [fetch_option_chain(session, sym, token)]
        if eq_key:
            tasks.append(fetch_candles(session, eq_key, "15minute", token))
            tasks.append(fetch_candles(session, eq_key, "day", token, days=60))
        else:
            tasks.append(asyncio.coroutine(lambda: [])())
            tasks.append(asyncio.coroutine(lambda: [])())

        results = await asyncio.gather(*tasks, return_exceptions=True)

    chain = results[0] if not isinstance(results[0], Exception) else {"error": str(results[0])}
    intraday_candles = results[1] if not isinstance(results[1], Exception) else []
    daily_candles = results[2] if not isinstance(results[2], Exception) else []

    if chain.get("error") == "token_expired":
        return {"error": "Upstox token expired — refresh needed"}

    analysis = {
        "symbol": sym,
        "lot_size": lot_size,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M IST"),
    }

    # Chain analysis
    if chain.get("ok"):
        analysis["chain"] = {
            "expiry": chain["expiry"],
            "spot": chain["spot"],
            "atm_strike": chain["atm_strike"],
            "atm_iv": chain["atm_iv"],
            "atm_ce": chain["atm_ce"],
            "atm_pe": chain["atm_pe"],
            "pcr": chain["pcr"],
            "vol_pcr": chain["vol_pcr"],
            "max_pain": chain["max_pain"],
            "max_pain_dist_pct": chain["max_pain_dist_pct"],
            "ce_walls": chain["ce_walls"],
            "pe_walls": chain["pe_walls"],
            "ce_oi_building": chain["ce_oi_building"],
            "pe_oi_building": chain["pe_oi_building"],
            "iv_skew_25d": chain["iv_skew_25d"],
            "total_ce_oi": chain["total_ce_oi"],
            "total_pe_oi": chain["total_pe_oi"],
        }
        analysis["oi_analysis"] = analyze_oi_pattern(chain)
        # Nearby strikes detail
        analysis["strike_detail"] = []
        for s in chain.get("nearby_strikes", []):
            analysis["strike_detail"].append({
                "strike": s["strike"],
                "ce_oi": s["ce_oi"], "ce_oi_chg": s["ce_oi_chg"], "ce_ltp": s["ce_ltp"],
                "pe_oi": s["pe_oi"], "pe_oi_chg": s["pe_oi_chg"], "pe_ltp": s["pe_ltp"],
            })
    else:
        analysis["chain_error"] = chain.get("error", "Unknown")

    # Technical analysis (intraday)
    if intraday_candles and len(intraday_candles) >= 20:
        analysis["technicals_15m"] = compute_technicals(intraday_candles)

    # Technical analysis (daily)
    if daily_candles and len(daily_candles) >= 20:
        analysis["technicals_daily"] = compute_technicals(daily_candles)

    return analysis


async def scan_for_setups(dashboard_context, direction="both", token=None):
    """Use dashboard context to identify top candidates, then deep analyze top 3.

    direction: "bullish", "bearish", or "both"
    """
    if not token:
        token = get_upstox_token()
    if not token:
        return {"error": "No Upstox token configured"}

    picks = dashboard_context.get("trade_ready_picks", [])
    if not picks:
        return {"summary": "No trade-ready stocks in current dashboard data."}

    # Filter by direction
    if direction == "bearish":
        candidates = [p for p in picks if p.get("direction") == "PE"]
        label = "BEARISH/SELLING"
    elif direction == "bullish":
        candidates = [p for p in picks if p.get("direction") == "CE"]
        label = "BULLISH/BUYING"
    else:
        candidates = picks
        label = "ALL"

    if not candidates:
        return {"summary": f"No {label} setups found among trade-ready stocks."}

    # Take top 3 by poised_score
    top = sorted(candidates, key=lambda p: p.get("poised_score", 0), reverse=True)[:3]

    # Deep analyze each
    analyses = []
    for pick in top:
        sym = pick.get("symbol", "")
        if sym:
            try:
                a = await deep_analyze_stock(sym, token)
                a["dashboard_score"] = pick.get("score")
                a["poised_score"] = pick.get("poised_score")
                a["direction"] = pick.get("direction")
                a["buildup"] = pick.get("buildup")
                analyses.append(a)
            except Exception as e:
                log.error(f"Analysis failed for {sym}: {e}")
                analyses.append({"symbol": sym, "error": str(e)})

    return {
        "scan_type": label,
        "total_trade_ready": dashboard_context.get("trade_ready_count", len(picks)),
        "direction_matched": len(candidates),
        "deep_analyzed": len(analyses),
        "analyses": analyses,
    }


# ============================================================
# QUERY CLASSIFICATION
# ============================================================

def classify_query(message, focused_stock=None):
    """Classify user's chat query to determine what analysis to run.

    Returns: (query_type, params)
      query_type: "stock_analysis", "market_scan", "general"
      params: dict with relevant info
    """
    msg = message.lower().strip()

    # Single stock analysis patterns
    stock_patterns = [
        r'\b(analyze|analysis|setup|look at|check|review|how is|what about)\b.*\b([A-Z]{3,20})\b',
        r'\b([A-Z]{3,20})\b.*(setup|chain|oi|technical|analysis|trade|option)',
    ]

    # Market scan patterns
    sell_patterns = ["sell", "short", "put", "pe ", "bearish", "poised for selling", "short build"]
    buy_patterns = ["buy", "long", "call", "ce ", "bullish", "poised for buying", "long build"]
    scan_patterns = ["best", "top", "which", "what.*poised", "recommend", "pick", "scan", "screen",
                     "most", "strongest", "weakest"]

    # Check for market-wide scan
    is_scan = any(p in msg for p in scan_patterns)
    is_bearish = any(p in msg for p in sell_patterns)
    is_bullish = any(p in msg for p in buy_patterns)

    if is_scan:
        if is_bearish and not is_bullish:
            return "market_scan", {"direction": "bearish"}
        elif is_bullish and not is_bearish:
            return "market_scan", {"direction": "bullish"}
        else:
            return "market_scan", {"direction": "both"}

    # Check for specific stock mention
    # Extract uppercase words that look like stock symbols
    words = re.findall(r'\b[A-Z]{3,20}\b', message)
    # Filter out common non-symbol words
    non_symbols = {"THE", "FOR", "AND", "BUT", "NOT", "CAN", "ARE", "WAS", "HAS", "HAD",
                   "RSI", "MACD", "EMA", "ATM", "OTM", "ITM", "PCR", "FNO", "NSE",
                   "NIFTY", "BANKNIFTY", "WHAT", "WHICH", "HOW", "WHY", "WHEN"}
    stock_mentions = [w for w in words if w not in non_symbols and len(w) >= 3]

    if stock_mentions:
        return "stock_analysis", {"symbol": stock_mentions[0]}

    # Check focused stock
    if focused_stock:
        # If the question seems about a stock (mentions chain, OI, etc.)
        stock_keywords = ["chain", "oi", "option", "technical", "setup", "premium", "strike",
                          "trade", "entry", "exit", "target", "stoploss", "sl ", "iv ", "pcr"]
        if any(k in msg for k in stock_keywords):
            return "stock_analysis", {"symbol": focused_stock}

    # Index-level queries
    if "nifty" in msg and any(k in msg for k in ["chain", "oi", "analysis", "setup", "outlook"]):
        return "stock_analysis", {"symbol": "NIFTY"}

    return "general", {}


# ============================================================
# FORMAT FOR LLM
# ============================================================

def format_analysis_for_llm(analysis):
    """Format analysis dict into structured text for LLM context injection."""
    if not analysis or analysis.get("error"):
        return f"[Analysis Error: {analysis.get('error', 'Unknown')}]"

    # Single stock analysis
    if "chain" in analysis or "technicals_15m" in analysis:
        return _format_stock_analysis(analysis)

    # Market scan
    if "analyses" in analysis:
        return _format_scan_results(analysis)

    return json.dumps(analysis, indent=2, default=str)


def _format_stock_analysis(a):
    """Format single stock deep analysis."""
    parts = []
    sym = a.get("symbol", "?")
    parts.append(f"=== DEEP ANALYSIS: {sym} (Lot: {a.get('lot_size', '?')}) ===")
    parts.append(f"Timestamp: {a.get('timestamp', 'now')}")

    if a.get("chain"):
        c = a["chain"]
        parts.append(f"\n--- OPTION CHAIN ---")
        parts.append(f"Expiry: {c.get('expiry')} | Spot: {c.get('spot')} | ATM: {c.get('atm_strike')}")
        parts.append(f"ATM IV: {c.get('atm_iv')}% | ATM CE: Rs {c.get('atm_ce')} | ATM PE: Rs {c.get('atm_pe')}")
        parts.append(f"PCR: {c.get('pcr')} | Vol PCR: {c.get('vol_pcr')}")
        parts.append(f"Max Pain: {c.get('max_pain')} ({c.get('max_pain_dist_pct'):+.1f}% from spot)")
        parts.append(f"IV Skew (25d): {c.get('iv_skew_25d'):+.1f}%")
        parts.append(f"Total CE OI: {c.get('total_ce_oi'):,} | Total PE OI: {c.get('total_pe_oi'):,}")

        if c.get("ce_walls"):
            walls = " | ".join(f"{w['strike']}({w['oi']:,})" for w in c["ce_walls"])
            parts.append(f"CE OI Walls (resistance): {walls}")
        if c.get("pe_walls"):
            walls = " | ".join(f"{w['strike']}({w['oi']:,})" for w in c["pe_walls"])
            parts.append(f"PE OI Walls (support): {walls}")
        if c.get("ce_oi_building"):
            bld = " | ".join(f"{b['strike']}(+{b['oi_chg']:,})" for b in c["ce_oi_building"])
            parts.append(f"CE OI Building Today: {bld}")
        if c.get("pe_oi_building"):
            bld = " | ".join(f"{b['strike']}(+{b['oi_chg']:,})" for b in c["pe_oi_building"])
            parts.append(f"PE OI Building Today: {bld}")

    if a.get("oi_analysis"):
        oi = a["oi_analysis"]
        parts.append(f"\n--- OI PATTERN ANALYSIS ---")
        if oi.get("resistance_from_oi"):
            r = " | ".join(f"{x['strike']}" for x in oi["resistance_from_oi"])
            parts.append(f"OI Resistance: {r}")
        if oi.get("support_from_oi"):
            s = " | ".join(f"{x['strike']}" for x in oi["support_from_oi"])
            parts.append(f"OI Support: {s}")
        if oi.get("oi_interpretation"):
            parts.append(f"Interpretation: {oi['oi_interpretation']}")

    if a.get("strike_detail"):
        parts.append(f"\n--- STRIKE-BY-STRIKE (ATM ± 5) ---")
        parts.append(f"{'Strike':>8} | {'CE_OI':>8} {'CE_Chg':>8} {'CE_LTP':>7} | {'PE_OI':>8} {'PE_Chg':>8} {'PE_LTP':>7}")
        for s in a["strike_detail"]:
            parts.append(
                f"{s['strike']:>8.0f} | {s['ce_oi']:>8,} {s['ce_oi_chg']:>+8,} {s['ce_ltp']:>7.1f} | "
                f"{s['pe_oi']:>8,} {s['pe_oi_chg']:>+8,} {s['pe_ltp']:>7.1f}"
            )

    for tf_key, tf_label in [("technicals_15m", "15-MIN TECHNICALS"), ("technicals_daily", "DAILY TECHNICALS")]:
        t = a.get(tf_key)
        if t and not t.get("error"):
            parts.append(f"\n--- {tf_label} ---")
            parts.append(f"Price: {t.get('current_price')} | RSI(14): {t.get('rsi_14')}")
            parts.append(f"EMA 9: {t.get('ema_9')} | EMA 21: {t.get('ema_21')} | EMA 50: {t.get('ema_50')} | Trend: {t.get('ema_trend')}")
            parts.append(f"MACD: {t.get('macd')} | Signal: {t.get('macd_signal')} | Hist: {t.get('macd_histogram')} | Cross: {t.get('macd_crossover')}")
            parts.append(f"VWAP: {t.get('vwap')} | Price vs VWAP: {t.get('price_vs_vwap')}")
            parts.append(f"Vol Ratio: {t.get('vol_ratio')}x | Range Position: {t.get('range_position_pct')}%")
            parts.append(f"Recent High: {t.get('recent_high')} | Recent Low: {t.get('recent_low')}")

    if a.get("dashboard_score"):
        parts.append(f"\n--- DASHBOARD CONTEXT ---")
        parts.append(f"Confluence Score: {a.get('dashboard_score')} | Poised Score: {a.get('poised_score')} | Direction: {a.get('direction')} | Buildup: {a.get('buildup')}")

    return "\n".join(parts)


def _format_scan_results(scan):
    """Format market scan results."""
    parts = []
    parts.append(f"=== MARKET SCAN: {scan.get('scan_type', 'ALL')} ===")
    parts.append(f"Trade-ready stocks: {scan.get('total_trade_ready', 0)} | Direction matches: {scan.get('direction_matched', 0)} | Deep analyzed: {scan.get('deep_analyzed', 0)}")

    for i, a in enumerate(scan.get("analyses", []), 1):
        if a.get("error"):
            parts.append(f"\n--- #{i} {a.get('symbol', '?')} — ANALYSIS ERROR: {a['error']} ---")
            continue
        parts.append(f"\n{'='*60}")
        parts.append(_format_stock_analysis(a))

    return "\n".join(parts)


# ============================================================
# MAIN ENTRY POINT (called by auth_proxy)
# ============================================================

async def run_analysis(message, focused_stock=None, dashboard_context=None, token=None):
    """Main entry: classify query, run appropriate analysis, return formatted text.

    Returns: (analysis_text, query_type) or (None, "general") if no analysis needed.
    """
    query_type, params = classify_query(message, focused_stock)
    log.info(f"Query classified as: {query_type}, params: {params}")

    if query_type == "general":
        return None, "general"

    if not token:
        token = get_upstox_token()
    if not token:
        return "[Cannot run deep analysis — Upstox token not configured on server]", query_type

    try:
        if query_type == "stock_analysis":
            symbol = params.get("symbol", focused_stock or "NIFTY")
            result = await deep_analyze_stock(symbol, token)
            return format_analysis_for_llm(result), query_type

        elif query_type == "market_scan":
            if not dashboard_context:
                return "[No dashboard data available for scan — open the dashboard first]", query_type
            result = await scan_for_setups(
                dashboard_context,
                direction=params.get("direction", "both"),
                token=token,
            )
            return format_analysis_for_llm(result), query_type

    except Exception as e:
        log.error(f"Analysis error: {e}", exc_info=True)
        return f"[Analysis error: {str(e)}]", query_type

    return None, "general"
