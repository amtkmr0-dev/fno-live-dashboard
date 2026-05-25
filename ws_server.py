#!/usr/bin/env python3
"""
ws_server.py - Async F&O Stock Dashboard Server
================================================
Self-contained aiohttp server that:
  - Polls Upstox REST API for LTP, OHLC, and option chain data
  - Maintains in-memory state for ~205 F&O stocks
  - Pushes delta updates to connected browser WebSocket clients
  - Serves a static HTML dashboard and JSON API

Usage:
  python3 ws_server.py                    # reads config.env + env vars
  python3 ws_server.py --token eyJ...     # explicit token
  python3 ws_server.py --port 8081        # custom port
"""

import argparse
import asyncio
import csv
import gzip
import io
import json
import logging
import os
import signal
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import urllib.parse

import aiohttp
from aiohttp import web

from upstox_ws_stream import UpstoxStreamer
from fyers_ws_stream import FyersStreamer
from auto_paper_trader import AutoPaperTrader
from db import DB

# Historical data recorder — captures every chain refresh to SQLite.
# Optional: if the module isn't available, recording is silently skipped.
try:
    import data_recorder
    DATA_RECORDER_AVAILABLE = True
except Exception as _data_recorder_err:
    DATA_RECORDER_AVAILABLE = False
    print(f"[ws_server] data_recorder unavailable: {_data_recorder_err}")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

# Set IST timezone for all logging
import logging
import pytz
from datetime import datetime
ist = pytz.timezone('Asia/Kolkata')
def custom_time(*args):
    return datetime.now(ist).timetuple()
logging.Formatter.converter = custom_time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("fno_server")

# ---------------------------------------------------------------------------
# Embedded Reference Data
# ---------------------------------------------------------------------------

STOCK_SECTOR: Dict[str, str] = {
    # 1. IT
    "TCS":"IT","INFY":"IT","WIPRO":"IT","HCLTECH":"IT","TECHM":"IT","LTIM":"IT",
    "PERSISTENT":"IT","MPHASIS":"IT","COFORGE":"IT","KPITTECH":"IT","TATAELXSI":"IT",
    "OFSS":"IT","CYIENT":"IT","KAYNES":"IT","NAUKRI":"IT",

    # 2. BANK
    "AXISBANK":"BANK","HDFCBANK":"BANK","ICICIBANK":"BANK","KOTAKBANK":"BANK",
    "INDUSINDBK":"BANK","FEDERALBNK":"BANK","AUBANK":"BANK","BANDHANBNK":"BANK",
    "RBLBANK":"BANK","CITYUNIONBK":"BANK","DCBBANK":"BANK","SBIN":"BANK",
    "BANKBARODA":"BANK","UNIONBANK":"BANK","INDIANB":"BANK","PNB":"BANK",
    "YESBANK":"BANK","IDFCFIRSTB":"BANK","CANBK":"BANK","BANKINDIA":"BANK",

    # 3. FINANCE
    "BAJFINANCE":"FINANCE","BAJAJFINSV":"FINANCE","CHOLAFIN":"FINANCE","MUTHOOTFIN":"FINANCE",
    "SBILIFE":"FINANCE","HDFCLIFE":"FINANCE","ICICIPRULI":"FINANCE","ICICIGI":"FINANCE",
    "HDFCAMC":"FINANCE","ANGELONE":"FINANCE","MANAPPURAM":"FINANCE","M&MFIN":"FINANCE","SHRIRAMFIN":"FINANCE",
    "LICI":"FINANCE","NIACL":"FINANCE","GICRE":"FINANCE","ABCAPITAL":"FINANCE","360ONE":"FINANCE",
    "PFC":"FINANCE","BAJAJHLDNG":"FINANCE","BSE":"FINANCE","PNBHOUSING":"FINANCE",
    "POLICYBZR":"FINANCE","MOTILALOFS":"FINANCE","CDSL":"FINANCE","MCX":"FINANCE",
    "NUVAMA":"FINANCE","KFINTECH":"FINANCE","LICHSGFIN":"FINANCE","MFSL":"FINANCE",
    "SBICARD":"FINANCE","NAM-INDIA":"FINANCE","SAMMAANCAP":"FINANCE","PAYTM":"FINANCE",
    "RECLTD":"FINANCE","CAMS":"FINANCE","IRFC":"FINANCE","JIOFIN":"FINANCE",
    "IREDA":"FINANCE","LTF":"FINANCE",

    # 4. AUTO
    "TATAMOTORS":"AUTO","MARUTI":"AUTO","M&M":"AUTO","BAJAJ-AUTO":"AUTO",
    "HEROMOTOCO":"AUTO","TVSMOTOR":"AUTO","EICHERMOT":"AUTO","BOSCHLTD":"AUTO",
    "BHARATFORG":"AUTO","EXIDEIND":"AUTO","MRF":"AUTO","APOLLOTYRE":"AUTO",
    "BALKRISIND":"AUTO","TIINDIA":"AUTO","FORCEMOT":"AUTO","HYUNDAI":"AUTO",
    "UNOMINDA":"AUTO","SONACOMS":"AUTO","TMPV":"AUTO","MOTHERSON":"AUTO",
    "ASHOKLEY":"AUTO",

    # 5. METAL
    "TATASTEEL":"METAL","JSWSTEEL":"METAL","HINDALCO":"METAL","JINDALSTEL":"METAL",
    "NATIONALUM":"METAL","VEDL":"METAL","SAIL":"METAL","HINDZINC":"METAL",
    "APLAPOLLO":"METAL","JSWENERGY":"METAL","WELCORP":"METAL","NMDC":"METAL",

    # 6. HEALTHCARE
    "SUNPHARMA":"HEALTHCARE","CIPLA":"HEALTHCARE","DRREDDY":"HEALTHCARE","DIVISLAB":"HEALTHCARE",
    "LUPIN":"HEALTHCARE","AUROPHARMA":"HEALTHCARE","BIOCON":"HEALTHCARE","ALKEM":"HEALTHCARE",
    "TORNTPHARM":"HEALTHCARE","ZYDUSLIFE":"HEALTHCARE","GLENMARK":"HEALTHCARE","IPCALAB":"HEALTHCARE",
    "APOLLOHOSP":"HEALTHCARE","MAXHEALTH":"HEALTHCARE","FORTIS":"HEALTHCARE","LAURUSLABS":"HEALTHCARE",
    "MANKIND":"HEALTHCARE",

    # 7. CONSUMER
    "HINDUNILVR":"CONSUMER","ITC":"CONSUMER","NESTLEIND":"CONSUMER","BRITANNIA":"CONSUMER",
    "DABUR":"CONSUMER","MARICO":"CONSUMER","GODREJCP":"CONSUMER","COLPAL":"CONSUMER",
    "TATACONSUM":"CONSUMER","VBL":"CONSUMER","UBL":"CONSUMER","ASIANPAINT":"CONSUMER",
    "BERGERPAIM":"CONSUMER","PIDILITIND":"CONSUMER","PAGEIND":"CONSUMER","TITAN":"CONSUMER",
    "HAVELLS":"CONSUMER","VOLTAS":"CONSUMER","WHIRLPOOL":"CONSUMER","BLUESTARCO":"CONSUMER",
    "CROMPTON":"CONSUMER","DIXON":"CONSUMER","POLYCAB":"CONSUMER","ABFRL":"CONSUMER",
    "AMBER":"CONSUMER","GODFRYPHLP":"CONSUMER","KALYANKJIL":"CONSUMER","TRENT":"CONSUMER",
    "INDHOTEL":"CONSUMER","NYKAA":"CONSUMER","SWIGGY":"CONSUMER","UNITDSPR":"CONSUMER",
    "DMART":"CONSUMER","PATANJALI":"CONSUMER","VMM":"CONSUMER","ETERNAL":"CONSUMER",
    "ZEEL":"CONSUMER","SUNTV":"CONSUMER","PVR":"CONSUMER","JUBLFOOD":"CONSUMER",

    # 8. ENERGY
    "RELIANCE":"ENERGY","ONGC":"ENERGY","BPCL":"ENERGY","IOC":"ENERGY",
    "HINDPETRO":"ENERGY","GAIL":"ENERGY","NTPC":"ENERGY","POWERGRID":"ENERGY",
    "TATAPOWER":"ENERGY","COALINDIA":"ENERGY","ADANIPOWER":"ENERGY",
    "ADANIGREEN":"ENERGY","TORNTPOWER":"ENERGY","IGL":"ENERGY","MGL":"ENERGY",
    "ADANIENSOL":"ENERGY","PGEL":"ENERGY","PREMIERENE":"ENERGY","INOXWIND":"ENERGY",
    "OIL":"ENERGY","WAAREEENER":"ENERGY","SUZLON":"ENERGY","NHPC":"ENERGY",
    "IEX":"ENERGY","PETRONET":"ENERGY",

    # 9. INFRA
    "DLF":"INFRA","GODREJPROP":"INFRA","LODHA":"INFRA","OBEROIRLTY":"INFRA",
    "PRESTIGE":"INFRA","PHOENIXLTD":"INFRA","SOBHA":"INFRA","BHARTIARTL":"INFRA",
    "LT":"INFRA","ULTRACEMCO":"INFRA","ABB":"INFRA","SIEMENS":"INFRA","BHEL":"INFRA",
    "ADANIPORTS":"INFRA","ADANIENT":"INFRA","CUMMINSIND":"INFRA","AMBUJACEM":"INFRA",
    "ACC":"INFRA","SHREECEM":"INFRA","JKCEMENT":"INFRA","DALMIACEM":"INFRA",
    "ASTRAL":"INFRA","COCHINSHIP":"INFRA","SOLARINDS":"INFRA","BDL":"INFRA",
    "CGPOWER":"INFRA","DELHIVERY":"INFRA","IDEA":"INFRA","MAZDOCK":"INFRA",
    "HAL":"INFRA","PIIND":"INFRA","DALBHARAT":"INFRA","INDIGO":"INFRA",
    "POWERINDIA":"INFRA","SUPREMEIND":"INFRA","KEI":"INFRA","NBCC":"INFRA",
    "CONCOR":"INFRA","UPL":"INFRA","INDUSTOWER":"INFRA","GMRAIRPORT":"INFRA",
    "GRASIM":"INFRA","RVNL":"INFRA","SRF":"INFRA","BEL":"INFRA"
}

NIFTY_50: Set[str] = {
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK",
    "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV", "BHARTIARTL", "BPCL",
    "BRITANNIA", "CIPLA", "COALINDIA", "DRREDDY", "EICHERMOT",
    "ETERNAL", "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE",
    "HEROMOTOCO", "HINDALCO", "HINDUNILVR", "ICICIBANK", "INDUSINDBK",
    "INFY", "ITC", "JIOFIN", "JSWSTEEL", "KOTAKBANK",
    "LT", "M&M", "MARUTI", "NESTLEIND", "NTPC",
    "ONGC", "POWERGRID", "RELIANCE", "SBILIFE", "SBIN",
    "SHRIRAMFIN", "SUNPHARMA", "TATAMOTORS", "TATASTEEL", "TCS",
    "TECHM", "TITAN", "TRENT", "ULTRACEMCO", "WIPRO",
}

BLACKLIST: Set[str] = {
    "IDEA", "YESBANK", "IDFCFIRSTB", "SUZLON", "PNB",
    "GMRAIRPORT", "CANBK", "NMDC", "NBCC", "NHPC",
    "MOTHERSON", "BANKINDIA", "ASHOKLEY",
}

# Upstox API base URLs
UPSTOX_LTP_URL = "https://api.upstox.com/v3/market-quote/ltp"
UPSTOX_QUOTES_URL = "https://api.upstox.com/v2/market-quote/quotes"
UPSTOX_CHAIN_URL = "https://api.upstox.com/v2/option/chain"
UPSTOX_EXPIRY_URL = "https://api.upstox.com/v2/option/contract"
UPSTOX_CANDLES_URL = "https://api.upstox.com/v2/historical-candle/intraday/{instrument_key}/{interval}"
UPSTOX_DAILY_CANDLES_URL = "https://api.upstox.com/v2/historical-candle/{instrument_key}/day/{to_date}/{from_date}"
INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.csv.gz"

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_config_env(directory: str) -> Dict[str, str]:
    """Parse a config.env file (KEY=VALUE lines, # comments) from *directory*."""
    env_path = Path(directory) / "config.env"
    result: Dict[str, str] = {}
    if not env_path.exists():
        return result
    try:
        with open(env_path, "r") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                # Strip surrounding quotes if present
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                    value = value[1:-1]
                result[key] = value
        log.info("Loaded %d vars from %s", len(result), env_path)
    except Exception as exc:
        log.warning("Failed to read config.env: %s", exc)
    return result


def resolve_token(cli_token: Optional[str], config_vars: Dict[str, str]) -> str:
    """Resolve the Upstox access token from CLI arg > env var > config.env."""
    if cli_token:
        return cli_token
    env_token = os.environ.get("UPSTOX_ACCESS_TOKEN")
    if env_token:
        return env_token
    cfg_token = config_vars.get("UPSTOX_ACCESS_TOKEN")
    if cfg_token:
        return cfg_token
    log.warning("No access token found. Server will start but API polling will fail.")
    log.warning("Set token via Admin panel at /admin or config.env")
    return ""

# ---------------------------------------------------------------------------
# Instruments loading
# ---------------------------------------------------------------------------


class StockInfo:
    """Lightweight container for a single F&O stock's static info."""
    __slots__ = ("symbol", "ikey", "lot_size", "expiry", "sector", "is_n50", "name", "fut_ikey")

    def __init__(self, symbol: str, ikey: str, lot_size: int, expiry: str,
                 sector: str, is_n50: bool, name: str, fut_ikey: str = ""):
        self.symbol = symbol
        self.ikey = ikey
        self.lot_size = lot_size
        self.expiry = expiry
        self.sector = sector
        self.is_n50 = is_n50
        self.name = name
        self.fut_ikey = fut_ikey

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "ikey": self.ikey,
            "lot_size": self.lot_size,
            "expiry": self.expiry,
            "sector": self.sector,
            "is_n50": self.is_n50,
            "fut_ikey": self.fut_ikey,
        }


def _extract_ticker_from_tradingsymbol(tsym: str) -> Optional[str]:
    """
    Extract the underlying ticker from a FUTSTK tradingsymbol.
    E.g. "RELIANCE26MAYFUT" -> "RELIANCE"
         "M&M26JUNFUT"      -> "M&M"
         "360ONE26MAYFUT"   -> "360ONE"
         "BAJAJ-AUTO26MAYFUT" -> "BAJAJ-AUTO"

    The pattern is: TICKER + 2-digit year + 3-char month + "FUT"
    After stripping "FUT", the last 5 chars of the remainder are the date
    portion (YYMM = 2 digits + 3 alpha, e.g. "26MAY").
    """
    if not tsym or not tsym.endswith("FUT"):
        return None
    # Strip "FUT" suffix -> e.g. "RELIANCE26MAY"
    base = tsym[:-3]
    if len(base) < 6:
        return None

    # Primary strategy: last 5 chars are date (2-digit year + 3-char month)
    # e.g. base="RELIANCE26MAY" -> base[-5:]="26MAY", base[-5:-3]="26", base[-3:]="MAY"
    if len(base) >= 5 and base[-5:-3].isdigit() and base[-3:].isalpha():
        ticker = base[:-5]
        return ticker if ticker else None

    # Fallback: some formats may use 7-char date suffix (YYMMMDD)
    if len(base) >= 7 and base[-7:-5].isdigit():
        ticker = base[:-7]
        return ticker if ticker else None

    return None


def download_and_parse_instruments(target_expiry_index: int = 0) -> List[StockInfo]:
    """
    Download NSE instruments CSV (gzipped), parse FUTSTK and EQUITY rows,
    match them by company name, and return a list of StockInfo objects.
    This uses synchronous requests since it runs once at startup.
    """
    import urllib.request

    log.info("Downloading instruments from %s ...", INSTRUMENTS_URL)
    try:
        req = urllib.request.Request(INSTRUMENTS_URL)
        with urllib.request.urlopen(req, timeout=30) as resp:
            compressed = resp.read()
    except Exception as exc:
        log.error("Failed to download instruments: %s", exc)
        sys.exit(1)

    log.info("Decompressing instruments (%d bytes compressed) ...", len(compressed))
    try:
        raw = gzip.decompress(compressed)
    except Exception as exc:
        log.error("Failed to decompress instruments: %s", exc)
        sys.exit(1)

    text = raw.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text))

    # Single-pass collection of FUTSTK and EQUITY rows.
    # FUTSTK: keyed by company name -> list of {ticker, lot_size, expiry, tsym}
    # EQUITY: keyed by company name -> instrument_key, AND by tradingsymbol -> instrument_key
    futstk_by_name: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    equity_by_name: Dict[str, str] = {}
    equity_by_ticker: Dict[str, str] = {}
    ticker_to_name: Dict[str, str] = {}

    for row in reader:
        itype = (row.get("instrument_type") or "").strip()
        exchange = (row.get("exchange") or "").strip()
        name = (row.get("name") or "").strip().upper()
        tsym = (row.get("tradingsymbol") or "").strip()
        ikey = (row.get("instrument_key") or "").strip()

        if itype == "FUTSTK":
            ticker = _extract_ticker_from_tradingsymbol(tsym)
            if not ticker:
                continue
            try:
                lot_size = int(float(row.get("lot_size", 0) or 0))
            except (ValueError, TypeError):
                lot_size = 0
            expiry_raw = (row.get("expiry") or "").strip()
            futstk_by_name[name].append({
                "ticker": ticker,
                "lot_size": lot_size,
                "expiry": expiry_raw,
                "tsym": tsym,
                "ikey": ikey,
            })
            ticker_to_name[ticker] = name

        elif itype == "EQUITY" and exchange == "NSE_EQ":
            if name and ikey:
                equity_by_name[name] = ikey
            if tsym and ikey:
                equity_by_ticker[tsym] = ikey

    log.info("Parsed %d FUTSTK names, %d EQUITY names", len(futstk_by_name), len(equity_by_name))

    # Match FUTSTK to EQUITY by name, then build StockInfo list
    seen_symbols: Set[str] = set()
    stocks: List[StockInfo] = []

    for name, fut_list in futstk_by_name.items():
        # Pick the requested expiry FUTSTK entry
        # Sort by expiry date
        fut_list.sort(key=lambda x: x["expiry"])
        idx = min(target_expiry_index, len(fut_list) - 1)
        fut = fut_list[idx]
        ticker = fut["ticker"]

        if ticker in BLACKLIST:
            continue
        if ticker in seen_symbols:
            continue

        # Find equity instrument key: first by name match, then by ticker match
        ikey = equity_by_name.get(name) or equity_by_ticker.get(ticker)
        if not ikey:
            log.debug("No equity ikey found for %s (name=%s), skipping", ticker, name)
            continue

        sector = STOCK_SECTOR.get(ticker, "OTHER")
        is_n50 = ticker in NIFTY_50

        seen_symbols.add(ticker)
        stocks.append(StockInfo(
            symbol=ticker,
            ikey=ikey,
            lot_size=fut["lot_size"],
            expiry=fut["expiry"],
            sector=sector,
            is_n50=is_n50,
            name=name,
            fut_ikey=fut.get("ikey", ""),
        ))

    # Inject native indices so they appear on the dashboard
    stocks.append(StockInfo("NIFTY", "NSE_INDEX|Nifty 50", 25, "", "INDEX", False, "NIFTY 50", ""))
    stocks.append(StockInfo("BANKNIFTY", "NSE_INDEX|Nifty Bank", 15, "", "INDEX", False, "NIFTY BANK", ""))
    stocks.append(StockInfo("MIDCPNIFTY", "NSE_INDEX|NIFTY MID SELECT", 50, "", "INDEX", False, "NIFTY MIDCAP", ""))

    # Sort alphabetically for deterministic ordering
    stocks.sort(key=lambda s: s.symbol)
    log.info("Matched %d F&O stocks and indices", len(stocks))
    return stocks

# ---------------------------------------------------------------------------
# Chain analytics (embedded, no imports)
# ---------------------------------------------------------------------------

def compute_pcr_signal(pcr: float, bullish_threshold: float = 0.5, bearish_threshold: float = 0.85) -> str:
    """
    PCR-based directional sentiment signal — Indian-calibrated.

    Indian F&O has a structurally lower PCR baseline than US options
    (universe median ~0.58 today vs SPX baseline ~1.0). The classical
    0.8/1.2 thresholds borrowed from US literature would label ~88% of
    Indian F&O stocks as bullish — which is meaningless.

    We use **universe-percentile-derived thresholds** instead. The caller
    (FNOServer) computes the 20th and 80th percentile of today's PCR
    distribution every chain refresh and passes them in as the
    bullish_threshold / bearish_threshold args.

    Convention: aggregate PCR low → calls dominant → bullish (Indian retail).
    Default fallbacks (0.5 / 0.85) are reasonable for an unloaded universe
    based on today's observed distribution; once the universe pass updates
    them, real percentiles replace these defaults.

    Sources: Bajaj Broking, Bajaj Finserv, Angel One, ETMarkets, Forbes
    Advisor India, niftytrader.in, navia.co.in. Cross-sectional ranking
    framework: Goyal & Saretto (2009) for the principle (universe-rank, not
    absolute thresholds).
    """
    if pcr is None or pcr <= 0:
        return "NEUTRAL"
    bull_strong = bullish_threshold * 0.8
    bear_strong = bearish_threshold * 1.15
    if pcr < bull_strong:
        return "BULLISH"
    elif pcr < bullish_threshold:
        return "MILDLY_BULL"
    elif pcr > bear_strong:
        return "BEARISH"
    elif pcr > bearish_threshold:
        return "MILDLY_BEAR"
    return "NEUTRAL"


def compute_max_pain(strikes: List[Dict[str, Any]]) -> Optional[float]:
    """
    Compute max pain: the strike K that minimizes the total payout
    sum_i( pe_oi_i * max(0, K - strike_i) + ce_oi_i * max(0, strike_i - K) )
    """
    if not strikes:
        return None

    best_strike = None
    best_pain = float("inf")

    # Gather all unique strike prices
    all_strikes = [s["strike"] for s in strikes]

    for k in all_strikes:
        total_pain = 0.0
        for s in strikes:
            si = s["strike"]
            ce_oi = s.get("ce_oi", 0) or 0
            pe_oi = s.get("pe_oi", 0) or 0
            total_pain += pe_oi * max(0.0, k - si)
            total_pain += ce_oi * max(0.0, si - k)
        if total_pain < best_pain:
            best_pain = total_pain
            best_strike = k

    return best_strike


def compute_oi_buildup(price_chg_pct: float, oi_chg: float) -> str:
    """
    Determine OI buildup type:
    - LONG_BUILD:   price up + OI up
    - SHORT_BUILD:  price down + OI up
    - LONG_UNWIND:  price down + OI down
    - SHORT_COVER:  price up + OI down
    """
    price_up = price_chg_pct > 0
    oi_up = oi_chg > 0

    if price_up and oi_up:
        return "LONG_BUILD"
    elif not price_up and oi_up:
        return "SHORT_BUILD"
    elif not price_up and not oi_up:
        return "LONG_UNWIND"
    elif price_up and not oi_up:
        return "SHORT_COVER"
    return "NEUTRAL"


def to_fyers_symbol(symbol: str) -> str:
    # If it is NIFTY50, NIFTYBANK etc.
    if symbol in ["NIFTY", "NIFTY50", "Nifty 50"]:
        return "NSE:NIFTY50-INDEX"
    if symbol in ["BANKNIFTY", "Nifty Bank", "NIFTYBANK"]:
        return "NSE:NIFTYBANK-INDEX"
    if symbol in ["MIDCAPNIFTY", "NIFTY MID SELECT", "MIDCPNIFTY"]:
        return "NSE:MIDCPNIFTY-INDEX"
    if symbol in ["FINNIFTY", "Nifty Fin Service"]:
        return "NSE:FINNIFTY-INDEX"
    # Otherwise standard equity
    return f"NSE:{symbol}-EQ" 
def fyers_to_upstox_chain(fyers_chain: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_strike = {}
    for item in fyers_chain:
        sp = float(item.get("strike_price") or 0)
        if sp <= 0:
            continue
        if sp not in by_strike:
            by_strike[sp] = {
                "strike_price": sp,
                "call_options": {},
                "put_options": {}
            }
        
        opt_type = item.get("option_type") or ""
        # Sometimes Fyers uses lowercase or CE/PE in symbol.
        if "CE" in opt_type.upper() or (not opt_type and "CE" in item.get("symbol", "").upper()):
            by_strike[sp]["call_options"] = {
                "instrument_key": item.get("symbol", ""),
                "market_data": {
                    "oi": int(item.get("oi") or 0),
                    "prev_oi": int(item.get("prevOi") or item.get("prev_oi") or 0),
                    "volume": int(item.get("volume") or 0),
                    "ltp": float(item.get("ltp") or 0),
                    "iv": float(item.get("iv") or 0),
                }
            }
        elif "PE" in opt_type.upper() or (not opt_type and "PE" in item.get("symbol", "").upper()):
            by_strike[sp]["put_options"] = {
                "instrument_key": item.get("symbol", ""),
                "market_data": {
                    "oi": int(item.get("oi") or 0),
                    "prev_oi": int(item.get("prevOi") or item.get("prev_oi") or 0),
                    "volume": int(item.get("volume") or 0),
                    "ltp": float(item.get("ltp") or 0),
                    "iv": float(item.get("iv") or 0),
                }
            }
    return list(by_strike.values())


def calculate_bs_gamma(spot: float, strike: float, iv: float, expiry_str: Optional[str]) -> float:
    """
    Calculate theoretical option Gamma using Black-Scholes.
    spot: spot price
    strike: strike price
    iv: implied volatility as a percentage (e.g. 18.5) or decimal (0.185)
    expiry_str: expiry date string in 'YYYY-MM-DD'
    """
    import math
    if spot <= 0 or strike <= 0 or iv <= 0:
        return 0.0
    
    # Normalize IV to decimal
    sigma = iv / 100.0 if iv > 1.0 else iv
    if sigma <= 0:
        return 0.0
        
    # Calculate time to expiry in years
    try:
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).date()
        expiry_date = datetime.strptime(expiry_str, "%Y-%m-%d").date()
        days = (expiry_date - today).days
        t = max(0.5, float(days)) / 365.0
    except Exception:
        t = 7.0 / 365.0  # default 7 days
        
    r = 0.07  # 7% standard Indian risk-free rate
    
    try:
        denom = sigma * math.sqrt(t)
        d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * t) / denom
        # Normal standard PDF: N'(x) = 1/sqrt(2pi) * e^(-x^2/2)
        pdf = (1.0 / math.sqrt(2.0 * math.pi)) * math.exp(-0.5 * d1 * d1)
        gamma = pdf / (spot * denom)
        return gamma
    except Exception:
        return 0.0


def analyze_chain(chain_data: List[Dict[str, Any]], spot: float,
                  price_chg_pct: float,
                  symbol: str = "",
                  pcr_bull_thr: float = 0.5,
                  pcr_bear_thr: float = 0.85,
                  lot_size: int = 1,
                  expiry: Optional[str] = None) -> Dict[str, Any]:
    """
    Analyze an option chain and return computed analytics including institutional
    market microstructure variables: Skew, INR Delta Notional Flow, GEX, Zero-Gamma.
    """
    result: Dict[str, Any] = {
        "pcr": None, "pcr_sig": "NEUTRAL", "buildup": "NEUTRAL",
        "ce_oi_chg": 0, "pe_oi_chg": 0, "net_oi": 0, "vol_oi": 0.0,
        "atm_iv": None, "max_pain": None, "mp_dist": None,
        "atm_ce": None, "atm_pe": None, "prem_ok": False, "atm_strike": None,
        "opt_vol": 0,
        "strike_map": {},  # strike -> data dict
    }

    if not chain_data or spot <= 0:
        return result

    # Automatically resolve expiry date from options data if not passed in
    if not expiry:
        for item in chain_data:
            co = item.get("call_options") or {}
            if co.get("expiry_date"):
                expiry = co.get("expiry_date")
                break
            po = item.get("put_options") or {}
            if po.get("expiry_date"):
                expiry = po.get("expiry_date")
                break

    total_ce_oi = 0
    total_pe_oi = 0
    total_ce_oi_chg = 0
    total_pe_oi_chg = 0
    total_ce_vol = 0
    total_pe_vol = 0

    strikes_for_mp: List[Dict[str, Any]] = []

    # 1. Pre-pass to find ATM strike
    atm_strike = None
    atm_dist = float("inf")
    sorted_strikes = sorted([float(x.get("strike_price", 0)) for x in chain_data if x.get("strike_price", 0)])
    
    for strike in sorted_strikes:
        dist = abs(strike - spot)
        if dist < atm_dist:
            atm_dist = dist
            atm_strike = strike
            
    atm_index = -1
    if atm_strike is not None:
        try:
            atm_index = sorted_strikes.index(atm_strike)
        except ValueError:
            pass

    atm_ce_ltp = None
    atm_pe_ltp = None
    atm_ce_iv = None
    atm_pe_iv = None

    for item in chain_data:
        strike_price = item.get("strike_price", 0)
        if not strike_price:
            continue

        # Call side
        call_data = item.get("call_options") or {}
        call_md = call_data.get("market_data") or {}
        call_greeks = call_data.get("option_greeks") or {}
        ce_oi = call_md.get("oi", 0) or 0
        ce_prev_oi = call_md.get("prev_oi", 0) or 0
        ce_vol = call_md.get("volume", 0) or 0
        ce_ltp = call_md.get("ltp", 0) or 0
        try:
            ce_iv = float(call_greeks.get("iv", 0) or call_md.get("iv", 0) or 0)
        except (ValueError, TypeError):
            ce_iv = 0.0

        # Put side
        put_data = item.get("put_options") or {}
        put_md = put_data.get("market_data") or {}
        put_greeks = put_data.get("option_greeks") or {}
        pe_oi = put_md.get("oi", 0) or 0
        pe_prev_oi = put_md.get("prev_oi", 0) or 0
        pe_vol = put_md.get("volume", 0) or 0
        pe_ltp = put_md.get("ltp", 0) or 0
        try:
            pe_iv = float(put_greeks.get("iv", 0) or put_md.get("iv", 0) or 0)
        except (ValueError, TypeError):
            pe_iv = 0.0

        # Extract Greeks with robust typing
        try:
            ce_delta = float(call_greeks.get("delta", 0) or 0)
        except (ValueError, TypeError):
            ce_delta = 0.0
            
        try:
            pe_delta = float(put_greeks.get("delta", 0) or 0)
        except (ValueError, TypeError):
            pe_delta = 0.0

        # Sign standardization: Call delta is positive (0 to 1), Put delta is negative (-1 to 0)
        ce_delta = abs(ce_delta) if ce_delta != 0 else 0.0
        pe_delta = -abs(pe_delta) if pe_delta != 0 else 0.0

        try:
            ce_gamma = float(call_greeks.get("gamma", 0) or 0)
        except (ValueError, TypeError):
            ce_gamma = 0.0
            
        try:
            pe_gamma = float(put_greeks.get("gamma", 0) or 0)
        except (ValueError, TypeError):
            pe_gamma = 0.0

        # Fallback Gamma calculation using Black-Scholes model if broker Greeks are empty
        if ce_gamma <= 0 and ce_iv > 0:
            ce_gamma = calculate_bs_gamma(spot, strike_price, ce_iv, expiry)
        if pe_gamma <= 0 and pe_iv > 0:
            pe_gamma = calculate_bs_gamma(spot, strike_price, pe_iv, expiry)

        # 1. Add to OVERALL counters
        total_ce_oi += ce_oi
        total_pe_oi += pe_oi
        total_ce_oi_chg += (ce_oi - ce_prev_oi)
        total_pe_oi_chg += (pe_oi - pe_prev_oi)
        total_ce_vol += ce_vol
        total_pe_vol += pe_vol

        # 2. Determine if strike is in the ACTIVE WINDOW
        is_active_strike = False
        if atm_index != -1:
            if symbol.upper() in ["NIFTY", "NIFTY50", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"]:
                # Indices: ATM ± 10 strikes
                try:
                    strike_idx = sorted_strikes.index(strike_price)
                    if abs(strike_idx - atm_index) <= 10:
                        is_active_strike = True
                except ValueError:
                    pass
            else:
                # Stocks: ±5% of spot price
                if spot > 0 and abs(strike_price - spot) <= (0.05 * spot):
                    is_active_strike = True
        else:
            is_active_strike = True  # Fallback

        if is_active_strike:
            result["ce_oi_chg_filtered"] = result.get("ce_oi_chg_filtered", 0) + (ce_oi - ce_prev_oi)
            result["pe_oi_chg_filtered"] = result.get("pe_oi_chg_filtered", 0) + (pe_oi - pe_prev_oi)
            result["net_oi_filtered"] = result.get("net_oi_filtered", 0) + ((pe_oi - pe_prev_oi) - (ce_oi - ce_prev_oi))


        strikes_for_mp.append({
            "strike": strike_price,
            "ce_oi": ce_oi,
            "pe_oi": pe_oi,
        })

        # Build detailed strike→ Greeks & notional map
        result["strike_map"][float(strike_price)] = {
            "ce_ltp": ce_ltp,
            "pe_ltp": pe_ltp,
            "ce_iv":  ce_iv,
            "pe_iv":  pe_iv,
            "ce_oi":  ce_oi,
            "pe_oi":  pe_oi,
            "ce_oi_chg": ce_oi - ce_prev_oi,
            "pe_oi_chg": pe_oi - pe_prev_oi,
            "ce_vol": ce_vol,
            "pe_vol": pe_vol,
            "ce_delta": ce_delta,
            "pe_delta": pe_delta,
            "ce_gamma": ce_gamma,
            "pe_gamma": pe_gamma,
            "ce_instrument_key": call_data.get("instrument_key"),
            "pe_instrument_key": put_data.get("instrument_key"),
        }

        # Save ATM metrics
        if strike_price == atm_strike:
            atm_ce_ltp = ce_ltp
            atm_pe_ltp = pe_ltp
            atm_ce_iv = ce_iv
            atm_pe_iv = pe_iv

    # PCR
    pcr = (total_pe_oi / total_ce_oi) if total_ce_oi > 0 else 0.0
    pcr_sig = compute_pcr_signal(pcr, pcr_bull_thr, pcr_bear_thr)

    # OI changes
    net_oi = total_pe_oi_chg - total_ce_oi_chg
    total_oi = total_ce_oi + total_pe_oi
    total_opt_vol = total_ce_vol + total_pe_vol
    vol_oi = (total_opt_vol / total_oi) if total_oi > 0 else 0.0

    # OI buildup using net OI change
    net_oi_chg = total_ce_oi_chg + total_pe_oi_chg  # total OI change
    buildup = compute_oi_buildup(price_chg_pct, net_oi_chg)

    # Max pain
    max_pain = compute_max_pain(strikes_for_mp)
    mp_dist = None
    if max_pain and spot > 0:
        mp_dist = round(((spot - max_pain) / spot) * 100, 2)

    # ATM IV
    atm_iv = None
    if atm_ce_iv and atm_pe_iv:
        atm_iv = round((atm_ce_iv + atm_pe_iv) / 2, 2)
    elif atm_ce_iv:
        atm_iv = round(atm_ce_iv, 2)
    elif atm_pe_iv:
        atm_iv = round(atm_pe_iv, 2)

    # Premium viability
    best_atm_prem = max(atm_ce_ltp or 0, atm_pe_ltp or 0)
    prem_ok = best_atm_prem >= 15

    # Universal rank-index moneyness buckets
    atm_ce_chg = 0
    atm_pe_chg = 0
    near_ce_chg = 0
    near_pe_chg = 0
    deep_ce_chg = 0
    deep_pe_chg = 0

    if atm_strike is not None:
        if atm_index != -1:
            for item in chain_data:
                strike_price = float(item.get("strike_price", 0))
                if not strike_price:
                    continue
                try:
                    strike_idx = sorted_strikes.index(strike_price)
                except ValueError:
                    continue
                
                # Call side
                call_data = item.get("call_options") or {}
                call_md = call_data.get("market_data") or {}
                ce_oi = call_md.get("oi", 0) or 0
                ce_prev_oi = call_md.get("prev_oi", 0) or 0
                ce_chg = ce_oi - ce_prev_oi
                
                # Put side
                put_data = item.get("put_options") or {}
                put_md = put_data.get("market_data") or {}
                pe_oi = put_md.get("oi", 0) or 0
                pe_prev_oi = put_md.get("prev_oi", 0) or 0
                pe_chg = pe_oi - pe_prev_oi
                
                diff_idx = strike_idx - atm_index
                if diff_idx == 0:
                    atm_ce_chg += ce_chg
                    atm_pe_chg += pe_chg
                elif diff_idx == 1:
                    near_ce_chg += ce_chg
                elif diff_idx == -1:
                    near_pe_chg += pe_chg
                elif diff_idx >= 2:
                    deep_ce_chg += ce_chg
                elif diff_idx <= -2:
                    deep_pe_chg += pe_chg

    moneyness_data = {
        "atm_ce": int(atm_ce_chg),
        "atm_pe": int(atm_pe_chg),
        "near_ce": int(near_ce_chg),
        "near_pe": int(near_pe_chg),
        "deep_ce": int(deep_ce_chg),
        "deep_pe": int(deep_pe_chg)
    }

    # ───────────────────────────────────────────────────────────────────────
    # 1. Volatility Skew (25d) Calculation
    # ───────────────────────────────────────────────────────────────────────
    iv_25d_call = None
    iv_25d_put = None
    min_call_delta_diff = float("inf")
    min_put_delta_diff = float("inf")

    # Pass 1: Find options inside [0.15, 0.35] closest to 0.25 delta
    for strike, data in result["strike_map"].items():
        ce_delta = data.get("ce_delta", 0) or 0
        pe_delta = data.get("pe_delta", 0) or 0
        ce_iv = data.get("ce_iv", 0) or 0
        pe_iv = data.get("pe_iv", 0) or 0

        if 0.15 <= ce_delta <= 0.35:
            diff = abs(ce_delta - 0.25)
            if diff < min_call_delta_diff and ce_iv > 0:
                min_call_delta_diff = diff
                iv_25d_call = ce_iv

        abs_pe_delta = abs(pe_delta)
        if 0.15 <= abs_pe_delta <= 0.35:
            diff = abs(abs_pe_delta - 0.25)
            if diff < min_put_delta_diff and pe_iv > 0:
                min_put_delta_diff = diff
                iv_25d_put = pe_iv

    # Fallback to entire chain if no strike matches exactly within Delta [0.15, 0.35]
    if iv_25d_call is None:
        closest_diff = float("inf")
        for strike, data in result["strike_map"].items():
            ce_delta = data.get("ce_delta", 0) or 0
            ce_iv = data.get("ce_iv", 0) or 0
            if ce_iv > 0:
                diff = abs(ce_delta - 0.25)
                if diff < closest_diff:
                    closest_diff = diff
                    iv_25d_call = ce_iv

    if iv_25d_put is None:
        closest_diff = float("inf")
        for strike, data in result["strike_map"].items():
            pe_delta = data.get("pe_delta", 0) or 0
            pe_iv = data.get("pe_iv", 0) or 0
            if pe_iv > 0:
                diff = abs(abs(pe_delta) - 0.25)
                if diff < closest_diff:
                    closest_diff = diff
                    iv_25d_put = pe_iv

    skew_25d = 0.0
    if iv_25d_call is not None and iv_25d_put is not None:
        skew_25d = iv_25d_put - iv_25d_call

    skew_25d_pct = 0.0
    if skew_25d != 0 and atm_iv and atm_iv > 0:
        skew_25d_pct = round((skew_25d / atm_iv) * 100.0, 2)

    # ───────────────────────────────────────────────────────────────────────
    # 2. Delta Notional Flow (DNF) & Gamma Exposure (GEX) Calculations
    # ───────────────────────────────────────────────────────────────────────
    spot_chg = price_chg_pct
    spot_up = spot_chg >= 0
    ce_trade_dir = 1 if spot_up else -1
    pe_trade_dir = -1 if spot_up else 1

    total_ce_dnf = 0.0
    total_pe_dnf = 0.0
    total_ce_gex = 0.0
    total_pe_gex = 0.0

    for strike, data in result["strike_map"].items():
        ce_delta = data.get("ce_delta", 0) or 0
        pe_delta = data.get("pe_delta", 0) or 0
        ce_oi_chg = data.get("ce_oi_chg", 0) or 0
        pe_oi_chg = data.get("pe_oi_chg", 0) or 0
        ce_oi = data.get("ce_oi", 0) or 0
        pe_oi = data.get("pe_oi", 0) or 0
        ce_gamma = data.get("ce_gamma", 0) or 0
        pe_gamma = data.get("pe_gamma", 0) or 0

        # Smart DNF (₹): Delta * OI_Change * Trade_Direction * LotSize * Spot
        ce_dnf = ce_delta * ce_oi_chg * ce_trade_dir * lot_size * spot
        pe_dnf = pe_delta * pe_oi_chg * pe_trade_dir * lot_size * spot

        data["ce_dnf_lakhs"] = round(ce_dnf / 100000.0, 2)
        data["pe_dnf_lakhs"] = round(pe_dnf / 100000.0, 2)

        total_ce_dnf += ce_dnf
        total_pe_dnf += pe_dnf

        # GEX (₹): (OI * Gamma) * LotSize * Spot^2 * 0.01
        ce_gex = ce_oi * ce_gamma * lot_size * spot * spot * 0.01
        pe_gex = -pe_oi * pe_gamma * lot_size * spot * spot * 0.01

        data["ce_gex_lakhs"] = round(ce_gex / 100000.0, 2)
        data["pe_gex_lakhs"] = round(pe_gex / 100000.0, 2)

        total_ce_gex += ce_gex
        total_pe_gex += pe_gex

    dnf_net = total_ce_dnf + total_pe_dnf
    gex_total = total_ce_gex + total_pe_gex

    dnf_ce_lakhs = round(total_ce_dnf / 100000.0, 2)
    dnf_pe_lakhs = round(total_pe_dnf / 100000.0, 2)
    dnf_net_lakhs = round(dnf_net / 100000.0, 2)
    gex_total_lakhs = round(gex_total / 100000.0, 2)

    # ───────────────────────────────────────────────────────────────────────
    # 3. Zero-Gamma Crossing Level Projection
    # ───────────────────────────────────────────────────────────────────────
    def project_gex_at_spot(test_spot: float) -> float:
        proj_gex = 0.0
        for strike, data in result["strike_map"].items():
            ce_oi = data.get("ce_oi", 0) or 0
            pe_oi = data.get("pe_oi", 0) or 0
            ce_iv = data.get("ce_iv", 0) or 0
            pe_iv = data.get("pe_iv", 0) or 0
            
            ce_g = calculate_bs_gamma(test_spot, strike, ce_iv, expiry) if ce_iv > 0 else 0.0
            pe_g = calculate_bs_gamma(test_spot, strike, pe_iv, expiry) if pe_iv > 0 else 0.0
            
            proj_gex += (ce_oi * ce_g - pe_oi * pe_g) * lot_size * test_spot * test_spot * 0.01
        return proj_gex

    zero_gamma = None
    if spot > 0 and expiry:
        # Project GEX for 15 price steps around current spot (-9% to +9%)
        import math
        steps = [spot * (1.0 + x * 0.015) for x in range(-6, 7)]
        gex_projections = []
        for s_step in steps:
            gex_projections.append((s_step, project_gex_at_spot(s_step)))
            
        # Locate sign crossovers
        for j in range(len(gex_projections) - 1):
            s1, g1 = gex_projections[j]
            s2, g2 = gex_projections[j+1]
            if (g1 <= 0 and g2 > 0) or (g1 >= 0 and g2 < 0):
                if g2 - g1 != 0:
                    zero_gamma = round(s1 - g1 * (s2 - s1) / (g2 - g1), 2)
                break

    result.update({
        "pcr": round(pcr, 2),
        "pcr_sig": pcr_sig,
        "buildup": buildup,
        "ce_oi": total_ce_oi,
        "pe_oi": total_pe_oi,
        "total_oi": total_oi,
        "ce_oi_chg": total_ce_oi_chg,
        "pe_oi_chg": total_pe_oi_chg,
        "net_oi": net_oi,
        "vol_oi": round(vol_oi, 3),
        "atm_iv": atm_iv,
        "max_pain": max_pain,
        "mp_dist": mp_dist,
        "atm_ce": atm_ce_ltp,
        "atm_pe": atm_pe_ltp,
        "prem_ok": prem_ok,
        "atm_strike": atm_strike,
        "opt_vol": total_opt_vol,
        "moneyness": moneyness_data,
        
        # New Options Microstructure Metrics
        "skew_25d": round(skew_25d, 2),
        "skew_25d_pct": skew_25d_pct,
        "dnf_ce_lakhs": dnf_ce_lakhs,
        "dnf_pe_lakhs": dnf_pe_lakhs,
        "dnf_net_lakhs": dnf_net_lakhs,
        "gex_total_lakhs": gex_total_lakhs,
        "zero_gamma": zero_gamma,
    })
    return result


# ---------------------------------------------------------------------------
# Volume surge confluence helper
# ---------------------------------------------------------------------------
# Computes vol_surge_5d / vol_surge_10d / vol_surge_20d from the current spot
# volume and the three baseline averages, then derives a "confluence"
# classification:
#   TRIPLE  → all three surges ≥ threshold (today is loud against last week,
#             last fortnight, AND the trailing month). Industry-standard
#             breakout confirmation; rare and meaningful.
#   DOUBLE  → exactly two of the three surges ≥ threshold.
#   SINGLE  → only one window flags a surge (likely 5d-only — could be a
#             stale-baseline artefact).
#   None    → no window crosses threshold (or insufficient baseline data).
#
# Default threshold is 1.5×, which matches the LuxAlgo / heygotrade
# breakout-confirmation literature. Caller can override via env if needed.
VOL_SURGE_CONFLUENCE_THRESHOLD = float(
    os.environ.get("VOL_SURGE_CONFLUENCE_THRESHOLD", "1.5")
)


def compute_vol_confluence(
    vol: int,
    avg5: int,
    avg10: int,
    avg20: int,
    threshold: float = VOL_SURGE_CONFLUENCE_THRESHOLD,
) -> Dict[str, Any]:
    """Return the three surge ratios + confluence label.

    Surge ratios are rounded to 2 decimals; missing baselines yield 0.0 surge
    so the caller can ignore them safely. The label is the count of windows
    whose surge meets/exceeds `threshold`.
    """
    def _surge(avg: int) -> float:
        if not avg or avg <= 0 or not vol or vol <= 0:
            return 0.0
        return round(vol / avg, 2)

    s5 = _surge(avg5)
    s10 = _surge(avg10)
    s20 = _surge(avg20)
    hits = sum(1 for s in (s5, s10, s20) if s >= threshold)
    if hits >= 3:
        confluence: Optional[str] = "TRIPLE"
    elif hits == 2:
        confluence = "DOUBLE"
    elif hits == 1:
        confluence = "SINGLE"
    else:
        confluence = None
    return {
        "vol_surge_5d": s5,
        "vol_surge_10d": s10,
        "vol_surge_20d": s20,
        "vol_confluence": confluence,
    }


def compute_tradability_score(stock: Dict[str, Any]) -> Dict[str, Any]:
    """
    Evidence-based composite score (0-100) plus component breakdown,
    direction, confidence, and conviction tier.

    Returns a dict so call-sites can store all derived fields at once:
        { "score": int, "score_components": {...}, "direction": str|None,
          "confidence": str, "conviction_tier": str }

    Components (bound by literature):
      Information_Flow (0-40):
          Easley, O'Hara & Srinivas (1998) — informed traders prefer options
          for leverage; option-volume z-score combined with OI buildup direction.
          Fodor, Krieger & Doran (2011) — large positive call-OI changes
          predict positive equity returns; SHORT_BUILD predicts negative.
      Action_Magnitude (0-35):
          Bessembinder et al. — co-movement of price change and OI change
          carries directional information; isolated price or isolated OI
          changes are weaker.
      PCR_Confirmation (0-15):
          Jena, Tiwari & Mitra (2019) — OI-PCR has predictive power at
          ~12-day horizon; we use it as a confirmation tilt only, not a
          dominant signal. Distance from neutral 1.0, capped.
      Liquidity_Veto (0/1 multiplier):
          Practical execution: ATM premium >= 15 AND total_oi above floor.
          A score on a name you cannot trade is meaningless.

    Phase-2 components (not yet implemented — require historical data):
      - IV-RV spread (Goyal & Saretto 2009) — strongest single edge in
        equity options; needs 20-day realized vol per stock.
      - Volatility skew (Xing, Zhang & Zhao 2010) — needs 25-delta OTM
        call/put IVs; chain data has it, just not extracted.
      - IV percentile rank — needs 90-day IV history per stock.
    """
    import math

    def sigmoid_0_to(x: float, anchor: float, max_val: float) -> float:
        """Smooth 0→max_val mapping. x=0 → 0, x=anchor → ~0.73*max_val,
        large x → max_val. Avoids cliff thresholds.
        """
        if x <= 0:
            return 0.0
        # 1 - exp(-x/anchor) gives a clean monotonic curve, no magic numbers
        return max_val * (1.0 - math.exp(-x / anchor))

    # --- Inputs ---
    pcr_sig    = stock.get("pcr_sig") or "NEUTRAL"
    buildup    = stock.get("buildup") or "NEUTRAL"
    pcr        = stock.get("pcr")
    chg_pct    = stock.get("chg_pct") or 0.0
    vol_surge  = stock.get("vol_surge") or 0.0          # equity vol surge
    total_oi   = stock.get("total_oi") or 0
    net_oi_chg = (stock.get("ce_oi_chg") or 0) + (stock.get("pe_oi_chg") or 0)
    range_pct  = stock.get("range_pct") or 0.0
    prem_ok    = bool(stock.get("prem_ok"))
    atm_ce     = stock.get("atm_ce") or 0
    atm_pe     = stock.get("atm_pe") or 0

    # --- Direction inference (used by multiple components) ---
    bull_sig = pcr_sig in ("BULLISH", "MILDLY_BULL")
    bear_sig = pcr_sig in ("BEARISH", "MILDLY_BEAR")
    bull_bu  = buildup in ("LONG_BUILD", "SHORT_COVER")
    bear_bu  = buildup in ("SHORT_BUILD", "LONG_UNWIND")

    if bull_sig and bull_bu:
        direction, confidence = "CE", "high"
    elif bear_sig and bear_bu:
        direction, confidence = "PE", "high"
    elif bull_sig or bull_bu:
        direction, confidence = "CE", "low"
    elif bear_sig or bear_bu:
        direction, confidence = "PE", "low"
    else:
        direction, confidence = None, "none"

    # --- Component 1: Information Flow (0-40) ---
    # Volume surge contribution (0-25). Anchor at 1.0× (no surge → 0,
    # 2× → ~16, 3× → ~22, asymptote 25).
    vol_excess = max(0.0, vol_surge - 1.0)
    vol_pts = sigmoid_0_to(vol_excess, anchor=1.5, max_val=25.0)

    # OI buildup contribution (0-15). Strong directional buildup beats neutral.
    if buildup in ("LONG_BUILD", "SHORT_BUILD"):
        bu_pts = 15.0
    elif buildup in ("SHORT_COVER", "LONG_UNWIND"):
        bu_pts = 8.0   # weaker — unwinds without confirmation are reactive
    else:
        bu_pts = 0.0

    info_flow = vol_pts + bu_pts

    # --- Component 2: Action Magnitude (0-35) ---
    # Combines |price chg %| with |net OI chg as % of total OI|.
    # A 3% price move with no OI change is noise; a 3% move with 5% OI
    # change is positioning.
    abs_chg_pct = abs(chg_pct)
    chg_pts = sigmoid_0_to(abs_chg_pct, anchor=1.5, max_val=20.0)

    if total_oi > 0:
        oi_chg_pct_of_total = abs(net_oi_chg) / total_oi * 100.0
    else:
        oi_chg_pct_of_total = 0.0
    oi_chg_pts = sigmoid_0_to(oi_chg_pct_of_total, anchor=2.0, max_val=10.0)

    # Day range bonus (0-5) — wide range = real action, not just close-to-close drift
    range_pts = sigmoid_0_to(range_pct, anchor=2.0, max_val=5.0)

    action_mag = chg_pts + oi_chg_pts + range_pts

    # --- Component 3: PCR Confirmation (0-15) ---
    # Distance from neutral 1.0; tilts the score in line with research-supported
    # 12-day-horizon directional bias.
    if pcr is None or pcr <= 0:
        pcr_pts = 0.0
    else:
        pcr_dist = abs(pcr - 1.0)
        pcr_raw = sigmoid_0_to(pcr_dist, anchor=0.4, max_val=15.0)
        # Penalize when PCR direction disagrees with buildup direction
        pcr_bull = pcr >= 1.2
        pcr_bear = pcr <= 0.8
        if (pcr_bull and bear_bu) or (pcr_bear and bull_bu):
            pcr_raw *= 0.4   # contradicting signals — discount the contribution
        pcr_pts = pcr_raw

    # --- Component 4: Liquidity Veto (multiplier 0 or 1) ---
    LIQ_FLOOR_OI = 50_000
    if (not prem_ok) or total_oi < LIQ_FLOOR_OI:
        liq_mult = 0.0
        liquidity_status = "vetoed"
    else:
        liq_mult = 1.0
        # Small additive bonus for high-quality liquidity surfaced via direction
        # (we don't add it to score; just expose for the breakdown UI).
        if total_oi >= 500_000 and max(atm_ce, atm_pe) >= 30:
            liquidity_status = "excellent"
        elif total_oi >= 200_000:
            liquidity_status = "good"
        else:
            liquidity_status = "marginal"

    # --- Composite ---
    raw = info_flow + action_mag + pcr_pts
    score = int(round(raw * liq_mult))
    score = max(0, min(100, score))

    # --- Conviction tier ---
    if score >= 75 and confidence == "high" and liq_mult > 0:
        tier = "strong"
    elif score >= 60 and direction is not None and liq_mult > 0:
        tier = "watch"
    else:
        tier = "background"

    return {
        "score": score,
        "score_components": {
            "info_flow":    round(info_flow, 1),
            "info_flow_max": 40,
            "action_mag":   round(action_mag, 1),
            "action_mag_max": 35,
            "pcr_confirm":  round(pcr_pts, 1),
            "pcr_confirm_max": 15,
            "liquidity":    liquidity_status,
        },
        "direction":       direction,
        "confidence":      confidence,
        "conviction_tier": tier,
    }

# ---------------------------------------------------------------------------
# File-based persistence (no database)
# ---------------------------------------------------------------------------

class DataStore:
    """
    Simple JSON file persistence. Stores data in a 'data/' directory
    next to the server script. Thread-safe for single-process async use.
    """

    def __init__(self, base_dir: str):
        self.base_dir = Path(base_dir) / "data"
        self.base_dir.mkdir(exist_ok=True)
        self._cache: Dict[str, Any] = {}
        log.info("DataStore initialized at %s", self.base_dir)

    def _path(self, name: str) -> Path:
        return self.base_dir / f"{name}.json"

    def load(self, name: str, default: Any = None) -> Any:
        """Load a JSON file. Returns default if not found."""
        if name in self._cache:
            return self._cache[name]
        p = self._path(name)
        if not p.exists():
            self._cache[name] = default if default is not None else {}
            return self._cache[name]
        try:
            with open(p, "r") as f:
                data = json.load(f)
            self._cache[name] = data
            log.info("DataStore: loaded %s (%d bytes)", name, p.stat().st_size)
            return data
        except Exception as exc:
            log.warning("DataStore: failed to load %s: %s", name, exc)
            self._cache[name] = default if default is not None else {}
            return self._cache[name]

    def save(self, name: str, data: Any):
        """Save data to a JSON file."""
        self._cache[name] = data
        p = self._path(name)
        try:
            tmp = p.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2, default=str)
            tmp.replace(p)
        except Exception as exc:
            log.warning("DataStore: failed to save %s: %s", name, exc)

    def get_settings(self) -> Dict[str, Any]:
        """Get application settings with defaults."""
        defaults = {
            "upstox_token": "",
            "admin_pin": "1234",
            "max_paper_trades_per_day": 2,
            "default_lots": 1,
            "auto_exit_enabled": True,
            "auto_trail_sl_on_t1": True,
            "auto_trade_enabled": True,
            "server_port": 8081,
        }
        saved = self.load("settings", {})
        # Merge defaults with saved (saved values win)
        for k, v in defaults.items():
            if k not in saved:
                saved[k] = v
        return saved

    def save_settings(self, settings: Dict[str, Any]):
        """Save application settings."""
        self.save("settings", settings)

    def load_paper_trades(self) -> tuple:
        """Load paper trades and ID counter."""
        data = self.load("paper_trades", {"trades": [], "id_counter": 0})
        return data.get("trades", []), data.get("id_counter", 0)

    def save_paper_trades(self, trades: List[Dict[str, Any]], id_counter: int):
        """Save paper trades and ID counter."""
        self.save("paper_trades", {"trades": trades, "id_counter": id_counter})

# ---------------------------------------------------------------------------
# Server core
# ---------------------------------------------------------------------------

class DashboardServer:
    """
    Core server that manages state, API polling, and WebSocket clients.
    """

    # ── Polling cadence + concurrency tuning ─────────────────
    # All values can be overridden per-environment; defaults chosen to stay under
    # Upstox's 25 req/sec/endpoint rate limit while maximizing freshness.
    OHLC_POLL_INTERVAL = int(os.environ.get("OHLC_POLL_INTERVAL", "5"))   # seconds (was 30)
    FAST_OI_INTERVAL   = int(os.environ.get("FAST_OI_INTERVAL", "180"))  # seconds between fast-OI cycles
    OI_FAST_CONCURRENCY = int(os.environ.get("OI_FAST_CONCURRENCY", "1"))
    OI_FAST_PACING     = float(os.environ.get("OI_FAST_PACING", "1.0")) # sec between launches per worker
    CHAIN_CONCURRENCY  = int(os.environ.get("CHAIN_CONCURRENCY", "2"))
    CHAIN_PACING       = float(os.environ.get("CHAIN_PACING", "0.4"))    # sec between launches per worker
    CHAIN_INTERVAL     = int(os.environ.get("CHAIN_INTERVAL", "900"))    # seconds between full chain cycles

    # Fast-OI dedicated Upstox endpoints (per-instrument PCR/MaxPain/ChangeOI)
    UPSTOX_PCR_URL       = "https://api.upstox.com/v2/market/pcr"
    UPSTOX_MAX_PAIN_URL  = "https://api.upstox.com/v2/market/max-pain"
    UPSTOX_CHANGE_OI_URL = "https://api.upstox.com/v2/market/change-oi"

    def __init__(self, token: str, port: int, stocks: List[StockInfo], store: DataStore):
        self.token = token
        self.port = port
        self.stocks = stocks
        self.store = store
        self._start_time = time.time()

        # Load polling cadence + concurrency from environment (runtime-safe overrides)
        self.OHLC_POLL_INTERVAL = int(os.environ.get("OHLC_POLL_INTERVAL", "5"))
        self.FAST_OI_INTERVAL   = int(os.environ.get("FAST_OI_INTERVAL", "180"))
        self.OI_FAST_CONCURRENCY = int(os.environ.get("OI_FAST_CONCURRENCY", "1"))
        self.OI_FAST_PACING     = float(os.environ.get("OI_FAST_PACING", "1.0"))
        self.CHAIN_CONCURRENCY  = int(os.environ.get("CHAIN_CONCURRENCY", "2"))
        self.CHAIN_PACING       = float(os.environ.get("CHAIN_PACING", "0.4"))
        self.CHAIN_INTERVAL     = int(os.environ.get("CHAIN_INTERVAL", "900"))

        # Rollover Target Expiry Index (0 = Current Month, 1 = Next Month)
        self.target_expiry_index = 0

        # symbol -> StockInfo lookup
        self.stock_map: Dict[str, StockInfo] = {s.symbol: s for s in stocks}

        # Instrument key -> symbol reverse lookup
        # Upstox returns keys like "NSE_EQ:RELIANCE" (colon) in responses,
        # but requests use "NSE_EQ|INE002A01018" (pipe). We need both mappings.
        self.ikey_to_symbol: Dict[str, str] = {}
        # Also build colon-format key -> symbol (for response parsing)
        self.colon_key_to_symbol: Dict[str, str] = {}
        for s in stocks:
            self.ikey_to_symbol[s.ikey] = s.symbol
            # Convert pipe-format ikey to colon-format for response matching
            # e.g. "NSE_EQ|INE002A01018" -> "NSE_EQ:INE002A01018"
            colon_key = s.ikey.replace("|", ":")
            self.colon_key_to_symbol[colon_key] = s.symbol

        # In-memory state: symbol -> full data dict
        self.state: Dict[str, Dict[str, Any]] = {}
        self._init_state()

        # Connected WebSocket clients
        self.ws_clients: Set[web.WebSocketResponse] = set()

        # aiohttp client session (created in start)
        self.session: Optional[aiohttp.ClientSession] = None

        # Control flags
        self._running = False
        self._token_expired = False
        self._token_event = None  # Will be initialized in async loops

        # Nearest expiry (populated from instruments)
        self.nearest_expiry = stocks[0].expiry if stocks else ""

        # Background tasks
        self._tasks: List[asyncio.Task] = []

        # Upstox WebSocket streamer (replaces poll_ltp for real-time ticks)
        self._upstox_streamer: Optional[UpstoxStreamer] = None
        # Tier 3: optional secondary streamer for ATM±N option strikes (live OI)
        self._option_streamer: Optional[UpstoxStreamer] = None
        # symbol -> {"ce_baseline": int, "pe_baseline": int, "ce_total": int, "pe_total": int,
        #            "strikes": {strike: {"ce_ikey":..., "pe_ikey":..., "ce_oi": int, "pe_oi": int}}}
        self._option_oi_state: Dict[str, Dict[str, Any]] = {}
        self._ws_stream_fallback = False  # True = REST polling active as fallback

        # Auto paper trader (background scanner + trade entry)
        self._auto_trader: Optional[AutoPaperTrader] = None

        # Paper trading DB wrapper
        self._db = DB("quantra.db")
        self.paper_trades = []
        self._paper_id_counter = 0

        # Load settings
        self._settings = self.store.get_settings()

        # PCR thresholds — recalibrated each chain refresh from universe
        # 20th/80th percentile. Defaults match the conservative band.
        self._pcr_thr_bull: float = 0.5
        self._pcr_thr_bear: float = 0.85

    def _init_state(self):
        """Initialize the state dict with default values for each stock."""
        for s in self.stocks:
            self.state[s.symbol] = {
                "symbol": s.symbol,
                "sector": s.sector,
                "is_n50": s.is_n50,
                "lot": s.lot_size,
                "ikey": s.ikey,
                "expiry": s.expiry,
                # Price fields
                "ltp": 0.0,
                "prev_close": 0.0,
                "prev_close_date": "",   # YYYY-MM-DD; lets us refresh prev_close at daily rollover
                "chg": 0.0,
                "chg_pct": 0.0,
                "open": 0.0,
                "high": 0.0,
                "low": 0.0,
                "range_pct": 0.0,
                "gap_pct": 0.0,
                # Volume
                "vol": 0,
                "avg5d_vol": 0,
                "avg10d_vol": 0,
                "avg20d_vol": 0,
                "vol_surge": 0.0,
                "vol_surge_5d": 0.0,
                "vol_surge_10d": 0.0,
                "vol_surge_20d": 0.0,
                "vol_confluence": None,
                "opt_vol": 0,
                # Options analytics
                "pcr": None,
                "pcr_sig": "NEUTRAL",
                "buildup": "NEUTRAL",
                "ce_oi": 0,
                "pe_oi": 0,
                "total_oi": 0,
                "ce_oi_chg": 0,
                "pe_oi_chg": 0,
                "net_oi": 0,
                "vol_oi": 0.0,
                "atm_iv": None,
                "max_pain": None,
                "mp_dist": None,
                "atm_ce": None,
                "atm_pe": None,
                "prem_ok": False,
                "atm_strike": None,
                "strike_map": {},
                # Score
                "score": 0,
            }

    def _auth_headers(self) -> Dict[str, str]:
        """Standard Upstox API headers."""
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "Api-Version": "2.0",
        }

    def resolve_sym(self, resp_key: str) -> Optional[str]:
        """
        Resolve a response key (colon format like 'NSE_EQ:RELIANCE' or
        'NSE_EQ:INE002A01018') to a stock symbol.

        The Upstox API returns keys in colon format in responses, but
        these may be either exchange:tradingsymbol or exchange:isin format.
        We try multiple strategies to resolve.
        """
        # Direct colon-key lookup (from ikey pipe->colon conversion)
        sym = self.colon_key_to_symbol.get(resp_key)
        if sym:
            return sym

        # Try extracting the part after the colon as a symbol directly
        if ":" in resp_key:
            _, _, suffix = resp_key.partition(":")
            if suffix in self.stock_map:
                return suffix

        # Try pipe-format lookup
        pipe_key = resp_key.replace(":", "|")
        sym = self.ikey_to_symbol.get(pipe_key)
        if sym:
            return sym

        return None

    # -----------------------------------------------------------------------
    # API polling methods
    # -----------------------------------------------------------------------

    async def _api_get(self, url: str, params: Dict[str, str],
                       retries: int = 2) -> Optional[Dict]:
        """Make an authenticated GET request to Upstox API with retry on 429."""
        if not self.session:
            return None

        for attempt in range(retries + 1):
            try:
                async with self.session.get(url, params=params,
                                            headers=self._auth_headers(),
                                            timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 401:
                        if not self._token_expired:
                            self._token_expired = True
                            log.error("Token expired (HTTP 401). Will retry in 60s.")
                            await self._broadcast_status("Token expired. Please refresh.", "error")
                        return None

                    if resp.status == 429:
                        retry_after = int(resp.headers.get("Retry-After", "3"))
                        retry_after = max(2, min(retry_after, 30))
                        log.warning("Rate limited (429). Sleeping %ds (attempt %d/%d)",
                                    retry_after, attempt + 1, retries + 1)
                        await asyncio.sleep(retry_after)
                        continue  # retry

                    if resp.status != 200:
                        log.warning("API %s returned %d", url, resp.status)
                        return None

                    # Token worked, clear expired flag if it was set
                    if self._token_expired:
                        self._token_expired = False
                        log.info("Token is valid again.")
                        await self._broadcast_status("Token restored.", "info")

                    try:
                        data = await resp.json()
                    except Exception:
                        body_preview = await resp.text()
                        log.warning("API JSON parse error for %s: %s", url, body_preview[:200])
                        return None
                    return data

            except asyncio.TimeoutError:
                log.warning("API timeout: %s (attempt %d/%d)", url, attempt + 1, retries + 1)
                if attempt < retries:
                    await asyncio.sleep(2)
                    continue
                return None
            except Exception as exc:
                log.warning("API error for %s: %s", url, exc)
                return None

        log.warning("API exhausted retries for %s", url)
        return None

    # -----------------------------------------------------------------------
    # Upstox WebSocket v3 streaming callbacks
    # -----------------------------------------------------------------------

    async def _handle_ws_tick(self, delta: Dict[str, Dict[str, Any]]):
        """
        Callback from UpstoxStreamer when new tick data arrives.
        Updates state, broadcasts to browser clients, checks paper exits.
        Replaces the inner loop of poll_ltp().
        """
        broadcast_delta: Dict[str, Dict[str, Any]] = {}
        count = 0

        for sym, tick in delta.items():
            # Route index ticks to _index_state
            if hasattr(self, '_index_state') and sym in self._index_state:
                ltp = tick.get("ltp", 0)
                cp  = tick.get("cp", 0)
                ist = self._index_state[sym]
                self._refresh_prev_close(ist, cp)
                if ltp > 0:
                    ist["ltp"] = ltp
                    prev = ist.get("prev_close") or 0
                    ist["chg_pct"] = round((ltp - prev) / prev * 100, 2) if prev > 0 else 0
                continue

            if sym not in self.state:
                continue

            st = self.state[sym]
            ltp = tick.get("ltp", 0)
            cp = tick.get("cp", 0)

            if ltp <= 0:
                continue

            # Update prev_close from WS close price (rollover-aware)
            self._refresh_prev_close(st, cp)

            prev = st["prev_close"]
            chg = round(ltp - prev, 2) if prev > 0 else tick.get("chg", 0)
            chg_pct = round((chg / prev) * 100, 2) if prev > 0 else tick.get("chg_pct", 0)

            # ── Full / Full_D5 mode also carries OHLC + volume in the tick.
            # Apply them to state and compute derived fields (vol_surge, gap_pct, range_pct).
            tick_open  = tick.get("open")
            tick_high  = tick.get("high")
            tick_low   = tick.get("low")
            tick_vol   = tick.get("vol")
            extra_delta = {}
            if tick_open and tick_open > 0 and st.get("open") != tick_open:
                st["open"] = tick_open
                extra_delta["open"] = tick_open
                if prev > 0:
                    st["gap_pct"] = round(((tick_open - prev) / prev) * 100, 2)
                    extra_delta["gap_pct"] = st["gap_pct"]
            if tick_high and tick_high > 0 and st.get("high") != tick_high:
                st["high"] = tick_high
                extra_delta["high"] = tick_high
            if tick_low and tick_low > 0 and st.get("low") != tick_low:
                st["low"] = tick_low
                extra_delta["low"] = tick_low
            if (st.get("high") or 0) > 0 and (st.get("low") or 0) > 0:
                rp = round(((st["high"] - st["low"]) / st["low"]) * 100, 2)
                if rp != st.get("range_pct"):
                    st["range_pct"] = rp
                    extra_delta["range_pct"] = rp
            if tick_vol and tick_vol > 0 and st.get("vol") != tick_vol:
                st["vol"] = tick_vol
                extra_delta["vol"] = tick_vol
                # Recompute multi-window surge + confluence
                conf = compute_vol_confluence(
                    tick_vol,
                    st.get("avg5d_vol") or 0,
                    st.get("avg10d_vol") or 0,
                    st.get("avg20d_vol") or 0,
                )
                if conf["vol_surge_5d"] != st.get("vol_surge_5d"):
                    st["vol_surge_5d"] = conf["vol_surge_5d"]
                    extra_delta["vol_surge_5d"] = conf["vol_surge_5d"]
                if conf["vol_surge_10d"] != st.get("vol_surge_10d"):
                    st["vol_surge_10d"] = conf["vol_surge_10d"]
                    extra_delta["vol_surge_10d"] = conf["vol_surge_10d"]
                if conf["vol_surge_20d"] != st.get("vol_surge_20d"):
                    st["vol_surge_20d"] = conf["vol_surge_20d"]
                    extra_delta["vol_surge_20d"] = conf["vol_surge_20d"]
                if conf["vol_confluence"] != st.get("vol_confluence"):
                    st["vol_confluence"] = conf["vol_confluence"]
                    extra_delta["vol_confluence"] = conf["vol_confluence"]
                # Legacy vol_surge mirrors 5d
                if conf["vol_surge_5d"] != st.get("vol_surge"):
                    st["vol_surge"] = conf["vol_surge_5d"]
                    extra_delta["vol_surge"] = conf["vol_surge_5d"]

            # Only emit delta if LTP actually changed
            if ltp != st["ltp"]:
                st["ltp"] = ltp
                st["chg"] = chg
                st["chg_pct"] = chg_pct
                
                # Recompute score on LTP change
                try:
                    _r = compute_tradability_score(st)
                    st["score"] = _r["score"]
                    st["score_components"] = _r["score_components"]
                    st["direction"] = _r["direction"]
                    st["confidence"] = _r["confidence"]
                    st["conviction_tier"] = _r["conviction_tier"]
                except Exception:
                    pass
                    
                count += 1

                broadcast_delta[sym] = {
                    "ltp": ltp,
                    "chg": chg,
                    "chg_pct": chg_pct,
                    "vol": st["vol"],
                    "score": st["score"],
                    "direction": st.get("direction"),
                    "conviction_tier": st.get("conviction_tier"),
                    **extra_delta,
                }
            elif extra_delta:
                # LTP unchanged but OHLC/vol moved — still broadcast
                try:
                    _r = compute_tradability_score(st)
                    st["score"] = _r["score"]
                    st["score_components"] = _r["score_components"]
                    st["direction"] = _r["direction"]
                    st["confidence"] = _r["confidence"]
                    st["conviction_tier"] = _r["conviction_tier"]
                except Exception:
                    pass
                broadcast_delta[sym] = {
                    **extra_delta, 
                    "vol": st["vol"],
                    "score": st["score"],
                    "direction": st.get("direction"),
                    "conviction_tier": st.get("conviction_tier"),
                }
                count += 1

        if broadcast_delta:
            await self._broadcast({
                "type": "tick",
                "d": broadcast_delta,
                "ts": time.time(),
            })

        # Check auto-exits and update paper trades P&L on every tick
        if any(t["status"] == "OPEN" for t in self.paper_trades):
            await self._check_paper_auto_exits()
            await self._broadcast_paper_trades()

        if count > 0:
            log.debug("WS tick: %d updated", count)

    async def _run_weekend_simulator(self):
        """
        Simulates price and volume updates on weekends/non-trading hours
        to keep UI charts and live data flowing.
        """
        log.info("Weekend simulator task started.")
        import random
        while self._running:
            # Check if weekend (Saturday/Sunday) or outside market hours (before 9:15 AM or after 4:00 PM IST)
            now = datetime.now()
            is_weekend = now.weekday() >= 5
            is_outside_hours = now.hour < 9 or (now.hour == 9 and now.minute < 15) or now.hour >= 16
            
            # For best user experience, let's always run if no live data is flowing,
            # or if is_weekend or is_outside_hours is True.
            if is_weekend or is_outside_hours:
                # Choose 30 random stocks from state
                if self.stocks:
                    sample_stocks = random.sample(self.stocks, min(30, len(self.stocks)))
                    sim_delta = {}
                    for s in sample_stocks:
                        sym = s.symbol
                        st = self.state.get(sym)
                        if not st:
                            continue
                        
                        # Seeding fallback price if ltp is zero or missing
                        if not st.get("ltp") or st["ltp"] == 0:
                            prev_close = st.get("prev_close") or random.randint(100, 2500)
                            st["ltp"] = prev_close
                            st["prev_close"] = prev_close
                            st["high"] = prev_close
                            st["low"] = prev_close
                            st["vol"] = random.randint(1000, 50000)
                            # Reset stale chg_pct from previous real session
                            st["chg_pct"] = 0.0
                            st["chg"] = 0.0

                        
                        # Jitter price by a tiny amount (-0.08% to +0.08%)
                        current_ltp = st["ltp"]
                        jitter_pct = random.uniform(-0.0008, 0.0008)
                        new_ltp = round(current_ltp * (1.0 + jitter_pct), 2)
                        
                        # Vol increments
                        vol_inc = random.randint(100, 1000)
                        new_vol = (st.get("vol") or 0) + vol_inc
                        new_high = max(st.get("high") or 0, new_ltp)
                        new_low = min(st.get("low") or 9999999, new_ltp)
                        
                        sim_delta[sym] = {
                            "ltp": new_ltp,
                            "cp": st.get("prev_close") or new_ltp,
                            "vol": new_vol,
                            "high": new_high,
                            "low": new_low,
                        }
                    
                    # Also simulate NIFTY and BANKNIFTY indexes
                    if hasattr(self, '_index_state'):
                        for idx_sym in ["NIFTY50", "BANKNIFTY"]:
                            if idx_sym in self._index_state:
                                ist = self._index_state[idx_sym]
                                if not ist.get("ltp") or ist["ltp"] == 0:
                                    prev_close = 22450.0 if "NIFTY50" in idx_sym else 47850.0
                                    ist["ltp"] = prev_close
                                    ist["prev_close"] = prev_close
                                
                                current_ltp = ist["ltp"]
                                jitter_pct = random.uniform(-0.0003, 0.0003)
                                new_ltp = round(current_ltp * (1.0 + jitter_pct), 2)
                                sim_delta[idx_sym] = {
                                    "ltp": new_ltp,
                                    "cp": ist.get("prev_close") or new_ltp
                                }
                    
                    if sim_delta:
                        try:
                            await self._handle_ws_tick(sim_delta)
                        except Exception as e:
                            log.error(f"Simulator tick broadcast failed: {e}")
            
            await asyncio.sleep(1.5)

    async def _handle_ws_status(self, state: str, msg: str):
        """Callback from UpstoxStreamer for connection status changes."""
        log.info("Upstox WS status: %s — %s", state, msg)

        if state == "token_expired":
            # Mirror the token-expired behavior from REST polling
            if not self._token_expired:
                self._token_expired = True
                await self._broadcast_status("Upstox token expired. Please refresh via Admin.", "error")

        elif state == "connected":
            # Token is working, clear expired flag
            if self._token_expired:
                self._token_expired = False
                await self._broadcast_status("Upstox connection restored.", "info")
            # Disable REST fallback if it was active
            if self._ws_stream_fallback:
                self._ws_stream_fallback = False
                log.info("Upstox WS connected — disabling REST fallback polling")

        elif state == "reconnecting":
            # Could enable REST fallback if WS has been down too long
            pass

    def _recalibrate_pcr_thresholds_and_rescore(self) -> None:
        """
        Recompute PCR percentile thresholds across the live universe and
        re-run the scoring pass for every stock with the new thresholds.

        Why: Indian F&O has a structurally lower PCR baseline than US options.
        Today's universe-median PCR is ~0.58 and only a handful of names
        ever cross 1.0. The classical 0.8/1.2 thresholds borrowed from US
        literature label ~88% of the universe BULLISH — meaningless.

        We use today's actual 20th/80th percentile of universe PCR (over
        names with material total OI) as bullish/bearish cutoffs. Clamped
        to a plausible band so a thin-day distribution doesn't produce
        absurd thresholds.

        Stored as self._pcr_thr_bull / self._pcr_thr_bear and reused on
        the per-stock analyze_chain pass that follows.
        """
        pcrs = [
            (st.get("pcr") or 0)
            for st in self.state.values()
            if st.get("pcr") and st.get("total_oi") and st["total_oi"] > 50_000
        ]
        if len(pcrs) < 20:
            log.info("PCR recalibration: <20 stocks with material OI, keeping defaults")
            return
        pcrs.sort()
        n = len(pcrs)
        bull_thr = pcrs[int(n * 0.20)]
        bear_thr = pcrs[int(n * 0.80)]
        # Clamp to a plausible band so a thin / skewed day doesn't produce
        # nonsense (e.g. bull threshold of 0.20).
        bull_thr = max(0.40, min(0.70, bull_thr))
        bear_thr = max(0.75, min(1.40, bear_thr))
        self._pcr_thr_bull = bull_thr
        self._pcr_thr_bear = bear_thr
        log.info(
            "PCR thresholds recalibrated (N=%d): bull<%.2f, bear>%.2f (median=%.2f)",
            n, bull_thr, bear_thr, pcrs[n // 2],
        )
        # Re-rescore every stock that already has chain analytics so the UI
        # reflects the new universe-relative classification immediately.
        rescored = 0
        for sym, st in self.state.items():
            if not st.get("total_oi"):
                continue
            pcr = st.get("pcr")
            if pcr is None or pcr <= 0:
                continue
            new_sig = compute_pcr_signal(pcr, bull_thr, bear_thr)
            st["pcr_sig"] = new_sig
            try:
                _r = compute_tradability_score(st)
                st["score"] = _r["score"]
                st["score_components"] = _r["score_components"]
                st["direction"] = _r["direction"]
                st["confidence"] = _r["confidence"]
                st["conviction_tier"] = _r["conviction_tier"]
            except Exception:
                pass
            rescored += 1
        log.info("PCR recalibration: rescored %d stocks", rescored)

    def _refresh_prev_close(self, st: Dict[str, Any], cp: float) -> None:
        """
        Update prev_close on initial set OR when the trading day rolls over.

        Why this matters: ws_server can run for days. The first time we see
        a stock we cache `cp` as `prev_close`. But when markets close at 15:30
        and reopen the next day at 09:15, the *real* prev_close has changed —
        it's now the prior session's close, not what we cached. Without this,
        chg% stays anchored to a stale baseline forever.

        Strategy: stamp `prev_close_date` whenever we accept a new value. On
        every call we accept the new `cp` if either:
          - prev_close was never set, OR
          - prev_close_date is older than today (in IST)
        We keep `cp` itself as the source of truth — Upstox returns the
        previous-day close in this field, which is exactly what we want.
        """
        if cp is None or cp <= 0:
            return
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        today_str = _dt.now(_tz(_td(hours=5, minutes=30))).date().isoformat()
        cur_date = st.get("prev_close_date", "")
        if not st.get("prev_close") or cur_date < today_str:
            st["prev_close"] = cp
            st["prev_close_date"] = today_str

    async def poll_ltp(self):
        """
        Poll v3 LTP endpoint for all stocks in batches of 60.
        Runs every 1 second.

        NOW: Only runs as fallback when Upstox WebSocket streaming is unavailable.
        When the WS streamer is connected, this loop sleeps and yields.
        """
        # Build batched instrument key lists (60 per batch to keep URL short)
        BATCH_SIZE = 60
        ikey_batches = []
        for i in range(0, len(self.stocks), BATCH_SIZE):
            batch = self.stocks[i:i + BATCH_SIZE]
            ikey_batches.append(",".join(s.ikey for s in batch))

        fetched_initial = False

        while self._running:
            # If Upstox WS streamer is connected and we already fetched initial state, skip REST polling
            if fetched_initial and self._upstox_streamer and self._upstox_streamer.connected:
                await asyncio.sleep(5)  # check again in 5s
                continue
            if self._token_expired or not self.token:
                self._get_token_event().clear()
                try:
                    await asyncio.wait_for(self._get_token_event().wait(), timeout=10)
                except asyncio.TimeoutError:
                    pass
                continue

            # Fetch all batches
            all_data = {}
            for batch_keys in ikey_batches:
                resp = await self._api_get(UPSTOX_LTP_URL, {"instrument_key": batch_keys})
                if resp and "data" in resp:
                    all_data.update(resp["data"])

            resp = {"data": all_data} if all_data else None

            if resp and "data" in resp:
                delta: Dict[str, Dict[str, Any]] = {}
                data = resp["data"]
                count = 0

                for resp_key, quote in data.items():
                    sym = self.resolve_sym(resp_key)
                    if not sym or sym not in self.state:
                        continue

                    st = self.state[sym]
                    ltp = quote.get("last_price") or quote.get("ltp") or 0
                    vol = quote.get("volume") or 0
                    cp = quote.get("cp") or quote.get("close_price") or st.get("prev_close") or 0

                    if ltp <= 0:
                        continue

                    # Update prev_close (rollover-aware)
                    self._refresh_prev_close(st, cp)

                    prev = st["prev_close"]
                    chg = round(ltp - prev, 2) if prev > 0 else 0
                    chg_pct = round((chg / prev) * 100, 2) if prev > 0 else 0

                    # Only emit delta if something changed
                    if ltp != st["ltp"] or vol != st["vol"]:
                        st["ltp"] = ltp
                        st["chg"] = chg
                        st["chg_pct"] = chg_pct
                        if vol > 0:
                            st["vol"] = vol
                        count += 1

                        delta[sym] = {
                            "ltp": ltp,
                            "chg": chg,
                            "chg_pct": chg_pct,
                            "vol": st["vol"],
                        }

                if delta:
                    await self._broadcast({
                        "type": "tick",
                        "d": delta,
                        "ts": time.time(),
                    })

                # Check auto-exits and update paper trades P&L on every tick
                if any(t["status"] == "OPEN" for t in self.paper_trades):
                    await self._check_paper_auto_exits()
                    await self._broadcast_paper_trades()

                log.debug("LTP poll: %d updated", count)
            
            fetched_initial = True
            await asyncio.sleep(1)

    async def _bootstrap_market_data(self):
        """Fetch last close for all stocks and indices to populate state on boot."""
        log.info("Bootstrapping market data (last close)...")
        index_keys = {
            "NSE_INDEX|Nifty 50":          "NIFTY50",
            "NSE_INDEX|Nifty Bank":        "BANKNIFTY",
            "NSE_INDEX|NIFTY MID SELECT":  "MIDCAPNIFTY",
            "NSE_INDEX|India VIX":         "INDIAVIX",
        }
        all_ikeys = [s.ikey for s in self.stocks] + list(index_keys.keys())
        
        BATCH_SIZE = 60
        for i in range(0, len(all_ikeys), BATCH_SIZE):
            batch = all_ikeys[i:i + BATCH_SIZE]
            batch_keys = ",".join(batch)
            resp = await self._api_get("https://api.upstox.com/v2/market-quote/quotes", {"instrument_key": batch_keys})
            if resp and "data" in resp:
                for resp_key, quote in resp["data"].items():
                    pipe_key = resp_key.replace(":", "|")
                    if pipe_key in index_keys:
                        idx_sym = index_keys[pipe_key]
                        if hasattr(self, '_index_state') and idx_sym in self._index_state:
                            ist = self._index_state[idx_sym]
                            ltp = quote.get("last_price", 0)
                            cp = quote.get("close_price", 0)
                            if ltp > 0:
                                ist["ltp"] = ltp
                                ist["prev_close"] = cp or ltp
                                prev = ist["prev_close"]
                                ist["chg_pct"] = round((ltp - prev) / prev * 100, 2) if prev > 0 else 0.0
                                ist["chg"] = round(ltp - prev, 2) if prev > 0 else 0.0
                    else:
                        sym = self.resolve_sym(resp_key)
                        if sym and sym in self.state:
                            st = self.state[sym]
                            ltp = quote.get("last_price", 0)
                            cp = quote.get("close_price", 0)
                            vol = quote.get("volume", 0)
                            if ltp > 0:
                                st["ltp"] = ltp
                                self._refresh_prev_close(st, cp or ltp)
                                prev = st["prev_close"]
                                st["chg_pct"] = round((ltp - prev) / prev * 100, 2) if prev > 0 else 0.0
                                st["chg"] = round(ltp - prev, 2) if prev > 0 else 0.0
                                if vol > 0:
                                    st["vol"] = vol
                                try:
                                    _r = compute_tradability_score(st)
                                    st["score"] = _r["score"]
                                    st["score_components"] = _r["score_components"]
                                    st["direction"] = _r["direction"]
                                    st["confidence"] = _r["confidence"]
                                    st["conviction_tier"] = _r["conviction_tier"]
                                except Exception:
                                    pass
        log.info("Bootstrapped market data successfully.")

    async def poll_ohlc(self):
        """
        Poll v2 full quotes for OHLC + volume every 30 seconds, batched by 60.
        """
        BATCH_SIZE = 60
        ikey_batches = []
        for i in range(0, len(self.stocks), BATCH_SIZE):
            batch = self.stocks[i:i + BATCH_SIZE]
            ikey_batches.append(",".join(s.ikey for s in batch))

        # Wait a bit on startup to let LTP populate first
        await asyncio.sleep(5)

        while self._running:
            # If Upstox WS streamer is connected and streaming full_d5, skip REST polling
            if self._upstox_streamer and self._upstox_streamer.connected and self._upstox_streamer.mode == "full_d5":
                await asyncio.sleep(5)  # check again in 5s
                continue

            if self._token_expired or not self.token:
                self._get_token_event().clear()
                try:
                    await asyncio.wait_for(self._get_token_event().wait(), timeout=10)
                except asyncio.TimeoutError:
                    pass
                continue

            # Fetch all batches
            all_data = {}
            for batch_keys in ikey_batches:
                resp = await self._api_get(UPSTOX_QUOTES_URL, {"instrument_key": batch_keys})
                if resp and "data" in resp:
                    all_data.update(resp["data"])

            resp = {"data": all_data} if all_data else None

            if resp and "data" in resp:
                delta: Dict[str, Dict[str, Any]] = {}
                data = resp["data"]
                count = 0

                for resp_key, quote in data.items():
                    sym = self.resolve_sym(resp_key)
                    if not sym or sym not in self.state:
                        continue

                    st = self.state[sym]

                    ohlc = quote.get("ohlc") or {}
                    o = ohlc.get("open", 0) or 0
                    h = ohlc.get("high", 0) or 0
                    l = ohlc.get("low", 0) or 0
                    c = ohlc.get("close", 0) or 0  # previous close
                    vol = quote.get("volume", 0) or 0
                    ltp = quote.get("last_price", 0) or 0
                    avg_vol = quote.get("average_traded_price", 0) or 0

                    # Rollover-aware prev_close update
                    self._refresh_prev_close(st, c)
                    if o > 0:
                        st["open"] = o
                    if h > 0:
                        st["high"] = h
                    if l > 0:
                        st["low"] = l
                    if vol > 0:
                        st["vol"] = vol
                    if ltp > 0:
                        st["ltp"] = ltp

                    # Derived fields
                    prev = st["prev_close"]
                    if prev > 0:
                        st["chg"] = round(ltp - prev, 2) if ltp > 0 else st["chg"]
                        st["chg_pct"] = round((st["chg"] / prev) * 100, 2) if ltp > 0 else st["chg_pct"]
                        if o > 0:
                            st["gap_pct"] = round(((o - prev) / prev) * 100, 2)

                    if h > 0 and l > 0 and l > 0:
                        st["range_pct"] = round(((h - l) / l) * 100, 2)

                    # Volume surge — recompute against all three baselines
                    conf = compute_vol_confluence(
                        st.get("vol") or 0,
                        st.get("avg5d_vol") or 0,
                        st.get("avg10d_vol") or 0,
                        st.get("avg20d_vol") or 0,
                    )
                    st["vol_surge_5d"]  = conf["vol_surge_5d"]
                    st["vol_surge_10d"] = conf["vol_surge_10d"]
                    st["vol_surge_20d"] = conf["vol_surge_20d"]
                    st["vol_confluence"] = conf["vol_confluence"]
                    # Legacy vol_surge mirrors 5d
                    st["vol_surge"] = conf["vol_surge_5d"]

                    # Recompute score
                    _score_result = compute_tradability_score(st)
                    st["score"] = _score_result["score"]
                    st["score_components"] = _score_result["score_components"]
                    st["direction"] = _score_result["direction"]
                    st["confidence"] = _score_result["confidence"]
                    st["conviction_tier"] = _score_result["conviction_tier"]

                    count += 1
                    delta[sym] = {
                        "open": st["open"],
                        "high": st["high"],
                        "low": st["low"],
                        "range_pct": st["range_pct"],
                        "gap_pct": st["gap_pct"],
                        "vol": st["vol"],
                        "vol_surge": st["vol_surge"],
                        "vol_surge_5d":  st["vol_surge_5d"],
                        "vol_surge_10d": st["vol_surge_10d"],
                        "vol_surge_20d": st["vol_surge_20d"],
                        "vol_confluence": st["vol_confluence"],
                        "chg": st["chg"],
                        "chg_pct": st["chg_pct"],
                        "score": st["score"],
                        "score_components": st["score_components"],
                        "direction": st["direction"],
                        "confidence": st["confidence"],
                        "conviction_tier": st["conviction_tier"],
                    }

                if delta:
                    await self._broadcast({
                        "type": "ohlc",
                        "d": delta,
                        "ts": time.time(),
                    })

                log.info("OHLC poll: %d stocks updated", count)

            await asyncio.sleep(self.OHLC_POLL_INTERVAL)

    async def _fetch_nearest_expiry(self, ikey: str) -> Optional[str]:
        """Fetch the nearest expiry date for a given instrument using the contract API."""
        resp = await self._api_get(UPSTOX_EXPIRY_URL, {"instrument_key": ikey})
        if not resp or "data" not in resp:
            return None

        contracts = resp["data"]
        if not contracts:
            return None

        # Extract expiry dates and find the nearest future one
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        expiries = set()
        for c in contracts:
            exp = c.get("expiry", "")
            if exp and exp >= today_str:
                expiries.add(exp)

        if not expiries:
            # All expired; just return the latest
            all_exp = [c.get("expiry", "") for c in contracts if c.get("expiry")]
            return max(all_exp) if all_exp else None

        return min(expiries)

    async def populate_avg5d_vol(self):
        """
        One-time fetch of daily historical candles to populate volume baselines.

        Computes 5-day, 10-day, and 20-day average daily volume per stock
        from the Upstox v2 historical-candle/day endpoint, then derives the
        per-window surge ratios and the confluence label.

        Runs sequentially with 0.5s delay to stay under rate limits.
        We fetch ~32 calendar days so 22+ trading days fit comfortably.
        """
        if not self.token or self._token_expired:
            log.warning("Skipping volume baselines fetch — no valid token")
            return

        from datetime import timedelta
        today = datetime.now()
        to_date = today.strftime("%Y-%m-%d")
        # 35 calendar days ≈ 22+ trading days, room for holidays/weekends
        from_date = (today - timedelta(days=35)).strftime("%Y-%m-%d")

        populated = 0
        errors = 0

        for s in self.stocks:
            if not self._running:
                break

            encoded_key = s.ikey.replace("|", "%7C")
            url = UPSTOX_DAILY_CANDLES_URL.format(
                instrument_key=encoded_key,
                to_date=to_date,
                from_date=from_date,
            )

            try:
                async with self.session.get(
                    url,
                    headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        candles = data.get("data", {}).get("candles", [])
                        # Candles format: [ts, open, high, low, close, volume, oi]
                        # Upstox returns newest-first. Skip the very first row
                        # if it's "today" (partial / current session).
                        if candles:
                            volumes_all = [
                                int(c[5])
                                for c in candles
                                if len(c) > 5 and c[5] and c[5] > 0
                            ]
                            if volumes_all:
                                # Drop today's partial bar if its date matches today
                                first_ts = str(candles[0][0]) if candles[0] else ""
                                if first_ts.startswith(to_date) and len(volumes_all) > 1:
                                    volumes_all = volumes_all[1:]

                                def _avg(slice_n: int) -> int:
                                    sub = volumes_all[:slice_n]
                                    return int(sum(sub) / len(sub)) if sub else 0

                                avg5 = _avg(5)
                                avg10 = _avg(10)
                                avg20 = _avg(20)
                                st = self.state[s.symbol]
                                st["avg5d_vol"] = avg5
                                st["avg10d_vol"] = avg10
                                st["avg20d_vol"] = avg20
                                # Recompute surge ratios + confluence with current vol
                                conf = compute_vol_confluence(
                                    st.get("vol") or 0, avg5, avg10, avg20
                                )
                                st["vol_surge_5d"] = conf["vol_surge_5d"]
                                st["vol_surge_10d"] = conf["vol_surge_10d"]
                                st["vol_surge_20d"] = conf["vol_surge_20d"]
                                st["vol_confluence"] = conf["vol_confluence"]
                                # Keep legacy vol_surge as the 5d value
                                st["vol_surge"] = conf["vol_surge_5d"]
                                populated += 1
                    elif resp.status == 429:
                        retry_after = int(resp.headers.get("Retry-After", "5"))
                        await asyncio.sleep(retry_after)
                    else:
                        errors += 1
            except Exception as exc:
                errors += 1
                if errors <= 3:
                    log.warning("volume baselines fetch error for %s: %s", s.symbol, exc)

            await asyncio.sleep(0.5)  # Pace requests

        log.info(
            "volume baselines populated for %d/%d stocks (%d errors)",
            populated, len(self.stocks), errors,
        )

    async def poll_chains(self):
        """
        Poll option chains for each stock every 15 minutes.
        Batches of 2 with adaptive delay to avoid rate limits. Retries failed stocks.
        """
        # Initial delay to let LTP and OHLC populate
        await asyncio.sleep(15)

        # Cache resolved expiry for the day
        cached_expiry = None
        cached_expiry_date = None

        while self._running:
            if self._token_expired or not self.token:
                self._get_token_event().clear()
                try:
                    await asyncio.wait_for(self._get_token_event().wait(), timeout=10)
                except asyncio.TimeoutError:
                    pass
                continue

            cycle_start = time.time()
            log.info("Starting chain refresh for %d stocks...", len(self.stocks))
            chain_delta: Dict[str, Dict[str, Any]] = {}
            processed = 0
            errors = 0
            first_err = ""

            expiry = self.nearest_expiry
            if not expiry:
                log.warning("No expiry date available, skipping chain refresh")
                await asyncio.sleep(900)
                continue

            # Process stocks sequentially (1 at a time) with 1.5s delay
            # This is slower but avoids rate limiting: ~196 * 1.5s = ~5 minutes
            failed_stocks: List[StockInfo] = []

            # Bounded concurrency: CHAIN_CONCURRENCY in-flight, with per-worker pacing.
            # Stays under Upstox's chain endpoint cap (different instruments only —
            # the 30s/instrument floor is per-instrument, not per-account).
            chain_sem = asyncio.Semaphore(self.CHAIN_CONCURRENCY)

            async def _fetch_one(s):
                if not self._running:
                    return None
                async with chain_sem:
                    await asyncio.sleep(self.CHAIN_PACING)
                    res = await self._fetch_chain_for_stock(s, expiry)
                    return s, res

            results = await asyncio.gather(
                *[_fetch_one(s) for s in self.stocks],
                return_exceptions=True,
            )

            for r in results:
                if isinstance(r, Exception) or r is None:
                    errors += 1
                    if not first_err:
                        first_err = f"exception: {r}"
                    continue
                s, res = r
                if res:
                    chain_delta[s.symbol] = res
                    processed += 1
                else:
                    failed_stocks.append(s)
                    errors += 1
                    if not first_err:
                        first_err = f"{s.symbol}: empty/null response"

            # Retry failed stocks once with longer delay
            if failed_stocks and self._running:
                log.info("Retrying %d failed chains...", len(failed_stocks))
                retry_ok = 0
                for s in failed_stocks:
                    if not self._running:
                        break
                    await asyncio.sleep(3)  # longer delay for retry
                    res = await self._fetch_chain_for_stock(s, expiry)
                    if res:
                        chain_delta[s.symbol] = res
                        retry_ok += 1
                        errors -= 1
                        processed += 1

                if retry_ok:
                    log.info("Retry recovered %d/%d chains", retry_ok, len(failed_stocks))

            elapsed = time.time() - cycle_start
            log.info("Chain refresh done: %d/%d ok, %d errors, %.0fs elapsed (expiry=%s)",
                     processed, len(self.stocks), errors, elapsed, expiry)

            if errors > 0 and processed == 0:
                log.warning("ALL chain calls failed. First error: %s", first_err)
                log.warning("Hint: check if expiry '%s' is valid for option chains", expiry)

            # Universe-percentile recalibration pass.
            # The classical PCR thresholds (0.8/1.2) labelled ~88% of Indian
            # F&O stocks "bullish" because Indian F&O has a structurally lower
            # PCR baseline than US options. Compute today's actual 20th/80th
            # percentile and re-rescore every stock with those thresholds.
            self._recalibrate_pcr_thresholds_and_rescore()

            # The recalibration mutated state[*].pcr_sig, score, direction,
            # confidence, conviction_tier in-place. Sync those fields back
            # into chain_delta so the broadcast reflects the recalibrated
            # universe (otherwise the UI keeps the pre-recal values until
            # the next chain refresh).
            for sym, delta in chain_delta.items():
                st = self.state.get(sym, {})
                for fld in ("pcr_sig", "score", "score_components",
                            "direction", "confidence", "conviction_tier"):
                    if fld in st:
                        delta[fld] = st[fld]

            # Broadcast chain updates in one message
            if chain_delta:
                await self._broadcast({
                    "type": "chain",
                    "d": chain_delta,
                    "ts": time.time(),
                })

            # Persist this snapshot to historical SQLite (non-fatal on error)
            if DATA_RECORDER_AVAILABLE and chain_delta:
                try:
                    stats = data_recorder.record_snapshot(self.state)
                    log.info(
                        "data_recorder: stored %d snapshots / %d strikes",
                        stats.get("snapshots", 0),
                        stats.get("strikes", 0),
                    )
                except Exception as exc:
                    log.warning("data_recorder.record_snapshot failed: %s", exc)

            # Sleep remaining time to hit the configured cycle (default 15-min)
            remaining = max(60, self.CHAIN_INTERVAL - elapsed)
            await asyncio.sleep(remaining)

    async def poll_oi_fast(self):
        """
        Fast OI poll using dedicated Upstox endpoints (PCR, Max Pain, Change OI).
        Runs every FAST_OI_INTERVAL seconds (default 180s = 3 min).
        Each stock requires 3 API calls (PCR + MaxPain + ChangeOI).
        Bounded concurrency: OI_FAST_CONCURRENCY in-flight, OI_FAST_PACING per worker.
        """
        # Wait for token + expiry to be ready
        for _ in range(60):
            if self.token and not self._token_expired and self.nearest_expiry:
                break
            await asyncio.sleep(5)
        else:
            log.warning("poll_oi_fast: token/expiry never ready, giving up")
            return

        log.info("Fast OI poll started (interval=%ds, %d stocks, concurrency=%d)",
                 self.FAST_OI_INTERVAL, len(self.stocks), self.OI_FAST_CONCURRENCY)

        while self._running:
            if self._token_expired or not self.token:
                await asyncio.sleep(10)
                continue

            cycle_start = time.time()
            today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            expiry = self.nearest_expiry
            updated = 0
            errors = 0
            oi_delta: Dict[str, Dict[str, Any]] = {}

            sem = asyncio.Semaphore(self.OI_FAST_CONCURRENCY)

            async def _process_stock(s):
                nonlocal updated, errors
                if not self._running:
                    return
                sym = s.symbol
                ikey = s.ikey
                st = self.state.get(sym)
                if not st:
                    return
                async with sem:
                    await asyncio.sleep(self.OI_FAST_PACING)
                    try:
                        # ── PCR (latest insight point) ──
                        pcr_resp = await self._api_get(self.UPSTOX_PCR_URL, {
                            "instrument_key": ikey,
                            "expiry": expiry,
                            "date": today_str,
                            "bucket_interval": "5",
                        }, retries=1)
                        pcr_val = None
                        if pcr_resp and pcr_resp.get("data"):
                            d = pcr_resp["data"]
                            insights = d.get("insights") or []
                            if insights:
                                pcr_val = insights[-1].get("pcr")
                            if pcr_val is None:
                                pcr_val = d.get("pcr")

                        # ── Max Pain (latest insight point) ──
                        mp_resp = await self._api_get(self.UPSTOX_MAX_PAIN_URL, {
                            "instrument_key": ikey,
                            "expiry": expiry,
                            "date": today_str,
                            "bucket_interval": "5",
                        }, retries=1)
                        mp_val = None
                        if mp_resp and mp_resp.get("data"):
                            d = mp_resp["data"]
                            insights = d.get("insights") or []
                            if insights:
                                mp_val = insights[-1].get("max_pain")
                            if mp_val is None:
                                mp_val = d.get("max_pain")

                        # ── Change OI (CE/PE OI change) ──
                        # Note: change-oi uses 'interval' param (NOT bucket_interval like the other two).
                        # Response shape: {data: {total_call_change_oi, total_put_change_oi, ...}}
                        chg_resp = await self._api_get(self.UPSTOX_CHANGE_OI_URL, {
                            "instrument_key": ikey,
                            "expiry": expiry,
                            "date": today_str,
                            "interval": "1",
                        }, retries=1)
                        ce_oi_chg = None
                        pe_oi_chg = None
                        if chg_resp and chg_resp.get("data"):
                            d = chg_resp["data"]
                            # Authoritative source: Upstox aggregates per-strike CE/PE OI change.
                            ce_oi_chg = d.get("total_call_change_oi")
                            pe_oi_chg = d.get("total_put_change_oi")

                        # Apply to state and build broadcast delta
                        changes = {}
                        if pcr_val is not None and pcr_val > 0:
                            st["pcr"] = round(float(pcr_val), 2)
                            changes["pcr"] = st["pcr"]
                        if mp_val is not None and mp_val > 0:
                            st["max_pain"] = float(mp_val)
                            changes["max_pain"] = st["max_pain"]
                            spot = st.get("ltp") or 0
                            if spot > 0:
                                st["mp_dist"] = round(((spot - st["max_pain"]) / spot) * 100, 2)
                                changes["mp_dist"] = st["mp_dist"]
                        if ce_oi_chg is not None:
                            st["ce_oi_chg"] = int(ce_oi_chg)
                            changes["ce_oi_chg"] = st["ce_oi_chg"]
                        if pe_oi_chg is not None:
                            st["pe_oi_chg"] = int(pe_oi_chg)
                            changes["pe_oi_chg"] = st["pe_oi_chg"]
                        if "ce_oi_chg" in changes or "pe_oi_chg" in changes:
                            ce = st.get("ce_oi_chg") or 0
                            pe = st.get("pe_oi_chg") or 0
                            # Net OI bias = pe - ce (Indian retail convention: bullish if PE+ + CE-).
                            net = pe - ce
                            st["net_oi"] = net
                            changes["net_oi"] = net
                            # Buildup uses (ce + pe) — total OI change vs price change.
                            price_chg = st.get("chg_pct") or 0
                            try:
                                st["buildup"] = compute_oi_buildup(price_chg, ce + pe)
                                changes["buildup"] = st["buildup"]
                            except Exception:
                                pass

                        if changes:
                            oi_delta[sym] = changes
                            updated += 1

                    except Exception as exc:
                        errors += 1
                        if errors <= 3:
                            log.warning("poll_oi_fast error for %s: %s", sym, exc)

            await asyncio.gather(
                *[_process_stock(s) for s in self.stocks],
                return_exceptions=True,
            )

            elapsed = time.time() - cycle_start

            if oi_delta:
                await self._broadcast({
                    "type": "chain",
                    "d": oi_delta,
                    "ts": time.time(),
                })

            # Persist OI tick to data_recorder for the time-series page
            if DATA_RECORDER_AVAILABLE and oi_delta:
                try:
                    data_recorder.record_oi_tick(oi_delta, self.state)
                except Exception as exc:
                    log.warning("poll_oi_fast: record_oi_tick failed: %s", exc)

            log.info("Fast OI poll done: %d/%d updated, %d errors, %.0fs elapsed",
                     updated, len(self.stocks), errors, elapsed)

            remaining = max(30, self.FAST_OI_INTERVAL - elapsed)
            await asyncio.sleep(remaining)

    # ── Tier 3: option-strike OI streaming (sub-second CE/PE OI changes) ──
    OPTION_STRIKES_RANGE = int(os.environ.get("OPTION_STRIKES_RANGE", "5"))  # ATM ± N
    # Default ATM±5 (= 11 strikes × 2 legs × 196 stocks = 4,312 keys, fits in 3 conns,
    # or use ATM±2 to fit in 1 connection on Basic plan)

    async def _option_oi_bootstrap(self):
        """
        One-shot bootstrap for the option-OI streamer.
        Waits until the first chain refresh has populated strike maps, then resolves
        ATM±N strikes for each stock, builds the instrument-key list, takes a baseline
        OI snapshot, and launches a second UpstoxStreamer in full_d5 mode.
        """
        # Wait for first chain population
        for _ in range(60):
            populated = sum(1 for s in self.state.values() if s.get("strike_map"))
            if populated >= max(20, len(self.stocks) // 4):
                break
            await asyncio.sleep(10)
        else:
            log.warning("option_oi: chain never populated enough strikes; aborting")
            return

        # Resolve ATM±N strikes per stock + collect option instrument keys
        option_ikeys: List[str] = []
        ikey_to_underlying: Dict[str, Dict[str, Any]] = {}
        for s in self.stocks:
            sym = s.symbol
            st = self.state.get(sym, {})
            spot = st.get("ltp") or 0
            strike_map = st.get("strike_map") or {}
            if spot <= 0 or not strike_map:
                continue

            # Find ATM strike (closest to spot)
            sorted_strikes = sorted(strike_map.keys(), key=lambda k: abs(float(k) - spot))
            if not sorted_strikes:
                continue
            atm = sorted_strikes[0]
            sorted_by_value = sorted(strike_map.keys(), key=lambda k: float(k))
            atm_idx = sorted_by_value.index(atm)
            lo = max(0, atm_idx - self.OPTION_STRIKES_RANGE)
            hi = min(len(sorted_by_value), atm_idx + self.OPTION_STRIKES_RANGE + 1)
            target_strikes = sorted_by_value[lo:hi]

            entry = self._option_oi_state.setdefault(sym, {
                "ce_baseline": 0, "pe_baseline": 0,
                "ce_total": 0, "pe_total": 0,
                "strikes": {},
            })
            for strike in target_strikes:
                leg = strike_map.get(strike) or {}
                # Note: Upstox option ikeys aren't always exposed in chain response —
                # this requires the chain API to return per-strike instrument_key.
                # If absent, we skip (the chain endpoint sometimes only gives prices).
                ce_ikey = leg.get("ce_trading_symbol") or leg.get("ce_tsym")
                pe_ikey = leg.get("pe_trading_symbol") or leg.get("pe_tsym")
                if not ce_ikey and not pe_ikey:
                    # fallback to instrument keys if trading symbol is missing
                    ce_ikey = leg.get("ce_instrument_key") or leg.get("ce_ikey")
                    pe_ikey = leg.get("pe_instrument_key") or leg.get("pe_ikey")
                if not ce_ikey and not pe_ikey:
                    continue
                entry["strikes"][float(strike)] = {
                    "ce_ikey": ce_ikey, "pe_ikey": pe_ikey,
                    "ce_oi": int(leg.get("ce_oi") or 0),
                    "pe_oi": int(leg.get("pe_oi") or 0),
                }
                if ce_ikey:
                    ce_ikey = "NSE:" + ce_ikey if not ce_ikey.startswith("NSE:") else ce_ikey
                    option_ikeys.append(ce_ikey)
                    ikey_to_underlying[ce_ikey] = {"sym": sym, "leg": "ce", "strike": float(strike)}
                if pe_ikey:
                    pe_ikey = "NSE:" + pe_ikey if not pe_ikey.startswith("NSE:") else pe_ikey
                    option_ikeys.append(pe_ikey)
                    ikey_to_underlying[pe_ikey] = {"sym": sym, "leg": "pe", "strike": float(strike)}

            # Compute baseline totals from current snapshot
            entry["ce_baseline"] = sum(v["ce_oi"] for v in entry["strikes"].values())
            entry["pe_baseline"] = sum(v["pe_oi"] for v in entry["strikes"].values())
            entry["ce_total"] = entry["ce_baseline"]
            entry["pe_total"] = entry["pe_baseline"]

        if not option_ikeys:
            log.warning("option_oi: no option instrument keys resolved; chain endpoint may not expose per-strike ikeys")
            return

        log.info("option_oi: resolved %d strikes across %d stocks (%d total ikeys)",
                 sum(len(s["strikes"]) for s in self._option_oi_state.values()),
                 len(self._option_oi_state),
                 len(option_ikeys))

        # Stash mapping for tick handler
        self._option_ikey_map = ikey_to_underlying

        # Build a synthetic ikey_to_symbol for the streamer (it expects sym strings)
        # We use a sentinel key (the option ikey itself) so the streamer routes ticks
        # to our handler via _handle_option_tick.
        opt_ikey_to_sym = {ikey: ikey for ikey in option_ikeys}

        fyers_token = os.environ.get("FYERS_ACCESS_TOKEN", "")
        self._option_streamer = FyersStreamer(
            token=fyers_token,
            on_tick=self._handle_option_tick,
            on_status=self._handle_option_status,
            loop=asyncio.get_running_loop()
        )
        self._option_streamer.start()
        
        # Subscribe to fyers symbols: "NSE:" + trading_symbol
        self._option_streamer.subscribe(option_ikeys)
        log.info("Option-OI WS streamer launched for %d option instruments (mode=full_d5)",
                 len(option_ikeys))

    async def _handle_option_tick(self, delta: Dict[str, Dict[str, Any]]):
        """
        Tick handler for option-strike WS. Each tick carries OI for the instrument
        (via Full mode). We aggregate per underlying and emit ce_oi_chg / pe_oi_chg
        deltas relative to the day's baseline.
        """
        ikey_map = getattr(self, "_option_ikey_map", {})
        if not ikey_map:
            return

        touched_syms = set()
        for ikey, tick in delta.items():
            meta = ikey_map.get(ikey)
            if not meta:
                continue
            sym = meta["sym"]
            leg = meta["leg"]
            strike = meta["strike"]

            new_oi = int(tick.get("oi") or 0)
            if new_oi <= 0:
                continue

            entry = self._option_oi_state.get(sym)
            if not entry:
                continue
            strike_data = entry["strikes"].get(strike)
            if not strike_data:
                continue

            old_oi = strike_data.get(f"{leg}_oi", 0)
            if new_oi == old_oi:
                continue
            strike_data[f"{leg}_oi"] = new_oi
            touched_syms.add(sym)

        if not touched_syms:
            return

        broadcast: Dict[str, Dict[str, Any]] = {}
        for sym in touched_syms:
            entry = self._option_oi_state[sym]
            ce_total = sum(v["ce_oi"] for v in entry["strikes"].values())
            pe_total = sum(v["pe_oi"] for v in entry["strikes"].values())
            entry["ce_total"] = ce_total
            entry["pe_total"] = pe_total
            ce_chg = ce_total - entry["ce_baseline"]
            pe_chg = pe_total - entry["pe_baseline"]
            net = ce_chg + pe_chg

            st = self.state.get(sym)
            if not st:
                continue
            st["ce_oi_chg"] = ce_chg
            st["pe_oi_chg"] = pe_chg
            st["net_oi"]   = net
            try:
                st["buildup"] = compute_oi_buildup(st.get("chg_pct") or 0, net)
            except Exception:
                pass

            broadcast[sym] = {
                "ce_oi_chg": ce_chg,
                "pe_oi_chg": pe_chg,
                "net_oi":    net,
                "buildup":   st.get("buildup"),
            }

        if broadcast:
            await self._broadcast({
                "type": "chain",
                "d": broadcast,
                "ts": time.time(),
            })

    async def _handle_option_status(self, state: str, msg: str):
        log.info("Option-OI WS status: %s — %s", state, msg)

    async def _fetch_chain_for_stock(self, stock: StockInfo, expiry: str) -> Optional[Dict[str, Any]]:
        """Fetch and analyze option chain for a single stock."""
        # Load balancing / fallback: if we have FYERS_ACCESS_TOKEN, split 50/50 based on symbol
        fyers_token = os.environ.get("FYERS_ACCESS_TOKEN")
        use_fyers = False
        if fyers_token and self.target_expiry_index == 0:
            first_char = stock.symbol[0].upper() if stock.symbol else 'A'
            if 'A' <= first_char <= 'M':
                use_fyers = True

        if use_fyers:
            log.debug("Chain: Routing %s to Fyers backup poller", stock.symbol)
            res = await self._fetch_chain_from_fyers(stock)
            if res:
                return res
            log.debug("Chain: Fyers backup fetch failed for %s, falling back to Upstox", stock.symbol)

        resp = await self._api_get(UPSTOX_CHAIN_URL, {
            "instrument_key": stock.ikey,
            "expiry_date": expiry,
        })

        if not resp:
            log.debug("Chain: no response for %s (ikey=%s, exp=%s)", stock.symbol, stock.ikey, expiry)
            return None

        if "data" not in resp:
            log.debug("Chain: no 'data' key for %s — resp keys: %s", stock.symbol, list(resp.keys()))
            return None

        chain_data = resp["data"]
        if not isinstance(chain_data, list) or not chain_data:
            log.debug("Chain: empty data for %s (type=%s, len=%s)",
                       stock.symbol, type(chain_data).__name__,
                       len(chain_data) if isinstance(chain_data, (list, dict)) else "N/A")
            return None

        # Get spot price from chain or from our state
        spot = 0
        for item in chain_data:
            sp = item.get("underlying_spot_price", 0) or 0
            if sp > 0:
                spot = sp
                break

        if spot <= 0:
            spot = self.state.get(stock.symbol, {}).get("ltp", 0)

        if spot <= 0:
            return None

        price_chg_pct = self.state.get(stock.symbol, {}).get("chg_pct", 0)
        analytics = analyze_chain(
            chain_data, spot, price_chg_pct,
            symbol=stock.symbol,
            pcr_bull_thr=getattr(self, "_pcr_thr_bull", 0.5),
            pcr_bear_thr=getattr(self, "_pcr_thr_bear", 0.85),
        )

        # Update state
        # Authoritative-source policy: REMOVED.
        # We now rely entirely on the locally computed chain analytics 
        # (pcr, max_pain, etc.) derived from the 15-minute chain fetch.
        st = self.state.setdefault(stock.symbol, {})
        for key, val in analytics.items():
            st[key] = val

        # Recompute score with updated chain data
        _score_result = compute_tradability_score(st)
        st["score"] = _score_result["score"]
        st["score_components"] = _score_result["score_components"]
        st["direction"] = _score_result["direction"]
        st["confidence"] = _score_result["confidence"]
        st["conviction_tier"] = _score_result["conviction_tier"]

        # Return the analytics for broadcasting (exclude strike_map — too large for WS)
        result = {k: v for k, v in analytics.items() if k != "strike_map"}
        result["score"] = st["score"]
        result["score_components"] = st["score_components"]
        result["direction"] = st["direction"]
        result["confidence"] = st["confidence"]
        result["conviction_tier"] = st["conviction_tier"]
        # Multi-window volume baselines (kept as state-derived, not in analytics)
        # so dashboard chain-update handler can paint the surge cells too.
        result["vol_surge_5d"]   = st.get("vol_surge_5d")
        result["vol_surge_10d"]  = st.get("vol_surge_10d")
        result["vol_surge_20d"]  = st.get("vol_surge_20d")

    async def _fyers_historical_sync_loop(self):
        """Background task to fetch 1-min candles for all stocks via Fyers every 5 minutes."""
        from datetime import datetime, timezone, timedelta
        IST = timezone(timedelta(hours=5, minutes=30))
        await asyncio.sleep(15)  # Initial wait
        
        while self._running:
            token = os.environ.get("FYERS_ACCESS_TOKEN")
            if not token:
                await asyncio.sleep(60)
                continue
                
            now = datetime.now(IST)
            if not (9 <= now.hour <= 15):
                await asyncio.sleep(60)
                continue
                
            log.info("Starting Fyers historical data sync for all %d stocks", len(self.stocks))
            success_count = 0
            
            for stock in self.stocks:
                if not self._running:
                    break
                    
                candles = await self._fetch_historical_candles_fyers(stock.symbol)
                if candles:
                    if stock.symbol not in self.state:
                        self.state[stock.symbol] = {}
                    self.state[stock.symbol]["candles"] = candles
                    success_count += 1
                
                # Rate limit pacing: Max ~3 per second to leave room for option chain polling
                await asyncio.sleep(0.3)
                
            log.info("Fyers historical sync complete. Updated %d/%d stocks.", success_count, len(self.stocks))
            
            # Sleep for 5 minutes
            await asyncio.sleep(300)
        result["vol_confluence"] = st.get("vol_confluence")
        return result

    async def _fetch_historical_candles_fyers(self, symbol: str, resolution: str = "1") -> Optional[List[Dict[str, Any]]]:
        token = os.environ.get("FYERS_ACCESS_TOKEN")
        if not token:
            return None

        app_id = os.environ.get("FYERS_APP_ID", "")
        fyers_sym = to_fyers_symbol(symbol)
        
        # We need today's date in yyyy-mm-dd
        from datetime import datetime, timezone, timedelta
        IST = timezone(timedelta(hours=5, minutes=30))
        today_str = datetime.now(IST).strftime("%Y-%m-%d")
        
        url = "https://api-t1.fyers.in/data/history"
        params = {
            "symbol": fyers_sym,
            "resolution": resolution,
            "date_format": "1",
            "range_from": today_str,
            "range_to": today_str,
            "cont_flag": "1"
        }
        headers = {
            "Authorization": f"{app_id}:{token}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

        try:
            async with self.session.get(url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    log.warning("Fyers history API returned status %d for %s: %s", resp.status, symbol, text[:300])
                    return None

                d = await resp.json()
                if d.get("s") != "ok" or "candles" not in d:
                    log.warning("Fyers history API response not ok for %s: %s", symbol, d.get("message"))
                    return None

                raw_candles = d.get("candles", [])
                candles = []
                # Fyers candle format: [epoch, open, high, low, close, volume]
                for c in raw_candles:
                    if len(c) < 6:
                        continue
                    candles.append({
                        "time": int(c[0]),
                        "open": float(c[1]),
                        "high": float(c[2]),
                        "low": float(c[3]),
                        "close": float(c[4]),
                        "volume": int(c[5]),
                        "oi": 0
                    })
                return candles
        except Exception as exc:
            log.warning("Exception fetching Fyers history for %s: %s", symbol, exc)
            return None

    async def _fetch_chain_from_fyers(self, stock: StockInfo) -> Optional[Dict[str, Any]]:
        """
        Fetch and analyze option chain from Fyers REST API v3 as a backup source.
        """
        token = os.environ.get("FYERS_ACCESS_TOKEN")
        if not token:
            return None

        app_id = os.environ.get("FYERS_APP_ID", "")
        fyers_sym = to_fyers_symbol(stock.symbol)
        url = "https://api-t1.fyers.in/data/options-chain-v3"
        params = {
            "symbol": fyers_sym,
            "strikecount": "30"
        }
        headers = {
            "Authorization": f"{app_id}:{token}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

        try:
            async with self.session.get(
                url,
                params=params,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    log.warning("Fyers chain API returned status %d for %s: %s", resp.status, stock.symbol, text[:300])
                    return None

                d = await resp.json()
                if d.get("s") != "ok" or "data" not in d:
                    log.warning("Fyers chain API response not ok for %s: %s", stock.symbol, d.get("message"))
                    return None

                fyers_data = d["data"]
                options_chain = fyers_data.get("optionsChain")
                if not options_chain:
                    return None

                # Convert Fyers flat list to Upstox's strike-level structure
                converted_chain = fyers_to_upstox_chain(options_chain)
                if not converted_chain:
                    return None

                # Get spot price from state
                spot = self.state.get(stock.symbol, {}).get("ltp", 0)
                if spot <= 0:
                    # Fallback: estimate from strikes
                    for item in options_chain:
                        ltp = float(item.get("ltp") or 0)
                        if ltp > 0:
                            spot = float(item.get("strike_price") or 0)
                            break

                if spot <= 0:
                    return None

                price_chg_pct = self.state.get(stock.symbol, {}).get("chg_pct", 0)
                analytics = analyze_chain(
                    converted_chain, spot, price_chg_pct,
                    symbol=stock.symbol,
                    pcr_bull_thr=getattr(self, "_pcr_thr_bull", 0.5),
                    pcr_bear_thr=getattr(self, "_pcr_thr_bear", 0.85),
                )

                # Update state
                st = self.state.setdefault(stock.symbol, {})
                for key, val in analytics.items():
                    st[key] = val

                # Recompute score
                _score_result = compute_tradability_score(st)
                st["score"] = _score_result["score"]
                st["score_components"] = _score_result["score_components"]
                st["direction"] = _score_result["direction"]
                st["confidence"] = _score_result["confidence"]
                st["conviction_tier"] = _score_result["conviction_tier"]

                # Return analytics
                result = {k: v for k, v in analytics.items() if k != "strike_map"}
                result["score"] = st["score"]
                result["score_components"] = st["score_components"]
                result["direction"] = st["direction"]
                result["confidence"] = st["confidence"]
                result["conviction_tier"] = st["conviction_tier"]
                result["vol_surge_5d"]   = st.get("vol_surge_5d")
                result["vol_surge_10d"]  = st.get("vol_surge_10d")
                result["vol_surge_20d"]  = st.get("vol_surge_20d")
                result["vol_confluence"] = st.get("vol_confluence")
                return result

        except Exception as exc:
            log.warning("Fyers chain API error for %s: %s", stock.symbol, exc)
            return None


    # -----------------------------------------------------------------------
    # WebSocket management
    # -----------------------------------------------------------------------


    async def _broadcast_init(self):
        ws_state = {}
        for sym, data in self.state.items():
            ws_state[sym] = {k: v for k, v in data.items() if k != "strike_map"}
        init_msg = {
            "type": "init",
            "stocks": ws_state,
            "meta": {
                "count": len(self.state),
                "expiry": self.nearest_expiry,
                "ts": __import__("time").time(),
            },
        }
        await self._broadcast(init_msg)
        
    async def _broadcast(self, msg: Dict[str, Any]):

        """Send a JSON message to all connected WebSocket clients."""
        if not self.ws_clients:
            return
        text = json.dumps(msg, default=str)
        dead: List[web.WebSocketResponse] = []
        for ws in list(self.ws_clients):
            try:
                await ws.send_str(text)
            except (ConnectionResetError, ConnectionError, RuntimeError):
                dead.append(ws)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.ws_clients.discard(ws)

    async def _broadcast_to_user(self, user_id: int, msg: Dict[str, Any]):
        """Send a JSON message to all connected WebSocket clients of a specific user."""
        if not self.ws_clients:
            return
        text = json.dumps(msg, default=str)
        dead: List[web.WebSocketResponse] = []
        for ws in list(self.ws_clients):
            if ws.get("user_id") == user_id:
                try:
                    await ws.send_str(text)
                except (ConnectionResetError, ConnectionError, RuntimeError):
                    dead.append(ws)
                except Exception:
                    dead.append(ws)
        for ws in dead:
            self.ws_clients.discard(ws)

    async def _broadcast_status(self, msg: str, level: str = "info"):
        """Broadcast a status message to all clients."""
        await self._broadcast({
            "type": "status",
            "d": {"msg": msg, "level": level},
            "ts": time.time(),
        })

    # -----------------------------------------------------------------------
    # Paper Trading
    # -----------------------------------------------------------------------

    def _next_paper_id(self) -> str:
        self._paper_id_counter += 1
        return f"PT{self._paper_id_counter:04d}"

    def _save_paper_trades(self):
        """Persist paper trades to disk."""
        self.store.save_paper_trades(self.paper_trades, self._paper_id_counter)

    def _get_strike_premium(self, trade: Dict[str, Any]) -> float:
        """Look up the current premium of the specific traded strike from chain data.

        Priority:
        1. strike_map from chain poll (exact strike + option type)
        2. Fallback to ATM premium (only if traded strike IS the current ATM)
        3. Last resort: entry_premium (no update)
        """
        sym = trade["symbol"]
        st = self.state.get(sym, {})
        strike = trade.get("strike", 0)
        opt_type = trade["option_type"]

        # Primary: look up from strike_map (populated by chain poll)
        strike_map = st.get("strike_map", {})
        # strike_map keys might be int or float depending on JSON parsing
        strike_data = strike_map.get(float(strike)) if strike else None

        if strike_data:
            key = "ce_ltp" if opt_type == "CE" else "pe_ltp"
            prem = strike_data.get(key, 0)
            if prem and prem > 0:
                return prem

        # Fallback: only use ATM premium if traded strike == current ATM strike
        current_atm = st.get("atm_strike", 0)
        if strike and current_atm and strike == current_atm:
            if opt_type == "CE":
                atm_prem = st.get("atm_ce")
            else:
                atm_prem = st.get("atm_pe")
            if atm_prem and atm_prem > 0:
                return atm_prem

        # Last resort: return 0 to indicate "no live data"
        return 0

    def _compute_paper_pnl(self, trade: Dict[str, Any]) -> Dict[str, Any]:
        """Compute live P&L for an open paper trade using strike-specific premiums."""
        current_ltp = self._get_strike_premium(trade)

        # If we can't get live premium, show entry as current (no fake P&L)
        if current_ltp <= 0:
            current_ltp = trade["entry_premium"]
            has_live_data = False
        else:
            has_live_data = True

        entry = trade["entry_premium"]
        lot = trade["lot_size"]
        lots = trade.get("lots", 1)
        qty = lot * lots

        pnl_per_unit = current_ltp - entry
        pnl_total = pnl_per_unit * qty
        pnl_pct = (pnl_per_unit / entry * 100) if entry > 0 else 0

        # Check if SL or targets hit — ONLY if we have real live data
        sl = trade["sl_premium"]
        t1 = trade["target1"]
        t2 = trade["target2"]

        sl_hit = has_live_data and current_ltp <= sl
        t1_hit = has_live_data and current_ltp >= t1
        t2_hit = has_live_data and current_ltp >= t2

        return {
            "current_premium": round(current_ltp, 2),
            "pnl_per_unit": round(pnl_per_unit, 2),
            "pnl_total": round(pnl_total, 2),
            "pnl_pct": round(pnl_pct, 2),
            "sl_hit": sl_hit,
            "t1_hit": t1_hit,
            "t2_hit": t2_hit,
            "has_live_data": has_live_data,
        }

    def _paper_trade_to_dict(self, trade: Dict[str, Any]) -> Dict[str, Any]:
        """Convert a paper trade to a JSON-serializable dict with live P&L."""
        result = dict(trade)
        if trade["status"] == "OPEN":
            pnl = self._compute_paper_pnl(trade)
            result.update(pnl)
        return result

    def _db_row_to_memory_trade(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """Map SQLite paper_trades row to in-memory trade dict format."""
        status_map = {
            "PENDING": "OPEN",
            "ENTERED": "OPEN",
            "EXITED": "CLOSED",
            "CLOSED": "CLOSED"
        }
        is_auto = row["trade_type"] == "auto"
        from auto_paper_trader import WF_STOCKS
        wf_info = WF_STOCKS.get(row["symbol"])
        trade_side = "bull" if row["direction"] == "BULLISH" else "bear"
        side_match = (wf_info["side"] == trade_side) if wf_info else False

        return {
            "id": row["id"],
            "user_id": row["user_id"],
            "symbol": row["symbol"],
            "option_type": row["option_type"] or "CE",
            "strike": row["strike"],
            "expiry": row["expiry"],
            "entry_premium": row["entry_premium"],
            "sl_premium": row["sl_premium"],
            "target1": row["t1_premium"],
            "target2": row["t2_premium"],
            "lot_size": row["lot_size"] or 1,
            "lots": row["lots"] or 1,
            "spot_at_entry": row["spot_at_entry"],
            "status": status_map.get(row["status"], "OPEN"),
            "entry_time": row["entered_at"] or row["created_at"],
            "exit_time": row["exited_at"],
            "exit_premium": row["exit_premium"],
            "exit_reason": row["exit_reason"],
            "final_pnl": row["pnl"],
            "type": row["trade_type"],
            "entry_reason": row["entry_reason"],
            "auto_trade": is_auto,
            "reason": row["entry_reason"],
            "wf_tier": wf_info["tier"] if wf_info else 0,
            "wf_side": wf_info["side"] if wf_info else None,
            "wf_pf": wf_info["pf"] if wf_info else 0,
            "side_match": side_match,
        }

    async def _check_paper_auto_exits(self):
        """Auto-exit paper trades when SL or T2 is hit. T1 triggers trailing SL to entry (cost-free)."""
        if not self._settings.get("auto_exit_enabled", True):
            return
        just_exited = []
        sl_trailed = False
        for trade in self.paper_trades:
            if trade["status"] != "OPEN":
                continue

            pnl_info = self._compute_paper_pnl(trade)

            # --- SL hit: auto-exit at SL premium ---
            if pnl_info["sl_hit"]:
                qty = trade["lot_size"] * trade.get("lots", 1)
                exit_prem = trade["sl_premium"]
                pnl = round((exit_prem - trade["entry_premium"]) * qty, 2)
                pnl_pct = round((pnl / (trade["entry_premium"] * qty) * 100), 2) if (trade["entry_premium"] * qty) != 0 else 0.0
                costs_estimated = 40.0
                net_pnl = round(pnl - costs_estimated, 2)

                self._db.update_paper_trade(
                    trade["id"],
                    status="EXITED",
                    exit_premium=exit_prem,
                    exit_reason="SL_HIT",
                    pnl=pnl,
                    pnl_pct=pnl_pct,
                    costs_estimated=costs_estimated,
                    net_pnl=net_pnl,
                    exited_at=datetime.utcnow().isoformat(),
                )

                trade["status"] = "CLOSED"
                trade["exit_time"] = datetime.now(timezone.utc).isoformat()
                trade["exit_premium"] = exit_prem
                trade["exit_reason"] = "SL_HIT"
                trade["final_pnl"] = pnl
                just_exited.append(trade)
                log.info("Paper AUTO-EXIT SL: %s %s %s strike=%s exit=%.2f pnl=%.2f (live_prem=%.2f)",
                         trade["id"], trade["symbol"], trade["option_type"],
                         trade.get("strike"), exit_prem, pnl,
                         pnl_info["current_premium"])

            # --- T2 hit: auto-exit at T2 premium ---
            elif pnl_info["t2_hit"]:
                qty = trade["lot_size"] * trade.get("lots", 1)
                exit_prem = trade["target2"]
                pnl = round((exit_prem - trade["entry_premium"]) * qty, 2)
                pnl_pct = round((pnl / (trade["entry_premium"] * qty) * 100), 2) if (trade["entry_premium"] * qty) != 0 else 0.0
                costs_estimated = 40.0
                net_pnl = round(pnl - costs_estimated, 2)

                self._db.update_paper_trade(
                    trade["id"],
                    status="EXITED",
                    exit_premium=exit_prem,
                    exit_reason="T2_HIT",
                    pnl=pnl,
                    pnl_pct=pnl_pct,
                    costs_estimated=costs_estimated,
                    net_pnl=net_pnl,
                    exited_at=datetime.utcnow().isoformat(),
                )

                trade["status"] = "CLOSED"
                trade["exit_time"] = datetime.now(timezone.utc).isoformat()
                trade["exit_premium"] = exit_prem
                trade["exit_reason"] = "T2_HIT"
                trade["final_pnl"] = pnl
                just_exited.append(trade)
                log.info("Paper AUTO-EXIT T2: %s %s %s strike=%s exit=%.2f pnl=%.2f (live_prem=%.2f)",
                         trade["id"], trade["symbol"], trade["option_type"],
                         trade.get("strike"), exit_prem, pnl,
                         pnl_info["current_premium"])

            # --- T1 hit: trail SL up to entry (cost-free) ---
            elif pnl_info["t1_hit"]:
                if trade["sl_premium"] < trade["entry_premium"]:
                    old_sl = trade["sl_premium"]
                    trade["sl_premium"] = trade["entry_premium"]
                    log.info("Paper TRAIL SL: %s %s SL moved %.2f -> %.2f (cost-free)",
                             trade["id"], trade["symbol"], old_sl, trade["entry_premium"])
                    self._db.update_paper_trade(
                        trade["id"],
                        sl_premium=trade["sl_premium"]
                    )
                    sl_trailed = True

        if just_exited:
            exited_ids = {t["id"] for t in just_exited}
            self.paper_trades = [t for t in self.paper_trades if t["id"] not in exited_ids]
            await self._broadcast({
                "type": "paper_exit",
                "trades": [self._paper_trade_to_dict(t) for t in just_exited],
                "ts": time.time(),
            })
            await self._broadcast_paper_trades()
        elif sl_trailed:
            await self._broadcast_paper_trades()

    # -----------------------------------------------------------------------
    # HTTP route handlers
    # -----------------------------------------------------------------------

    async def handle_index(self, request: web.Request) -> web.Response:
        """Serve the dashboard HTML file."""
        html_path = Path(__file__).parent / "dashboard_live.html"
        if not html_path.exists():
            return web.Response(
                text="<h1>Dashboard HTML not found</h1><p>Place dashboard_live.html in the same directory.</p>",
                content_type="text/html",
                status=404,
            )
        return web.FileResponse(html_path)

    async def handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        """Handle a new WebSocket connection from a browser client."""
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)

        user_id_str = request.query.get("user_id")
        ws["user_id"] = int(user_id_str) if user_id_str else None
        ws["username"] = request.query.get("username")

        self.ws_clients.add(ws)
        client_ip = request.remote or "unknown"
        log.info("WS client connected: %s (total: %d)", client_ip, len(self.ws_clients))

        # Send initial full state (exclude strike_map — too large for WS payload)
        try:
            ws_state = {}
            for sym, data in self.state.items():
                ws_state[sym] = {k: v for k, v in data.items() if k != "strike_map"}
            init_msg = {
                "type": "init",
                "stocks": ws_state,
                "meta": {
                    "count": len(self.state),
                    "expiry": self.nearest_expiry,
                    "ts": time.time(),
                },
            }
            await ws.send_str(json.dumps(init_msg, default=str))
        except Exception as exc:
            log.warning("Failed to send init to WS client: %s", exc)
            self.ws_clients.discard(ws)
            return ws

        # Keep connection alive, handle incoming messages (ping/pong handled by aiohttp)
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    # Clients might send filter/command messages in future
                    pass
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    log.warning("WS error: %s", ws.exception())
                    break
        except Exception as exc:
            log.warning("WS connection loop exception: %s", exc, exc_info=True)
        finally:
            self.ws_clients.discard(ws)
            log.info("WS client disconnected: %s (remaining: %d)", client_ip, len(self.ws_clients))

        return ws

    async def handle_api_state(self, request: web.Request) -> web.Response:
        """Return the full current state as JSON (exclude strike_map for payload size)."""
        ws_state = {}
        for sym, data in self.state.items():
            ws_state[sym] = {k: v for k, v in data.items() if k != "strike_map"}
        return web.json_response({
            "stocks": ws_state,
            "meta": {
                "count": len(self.state),
                "expiry": self.nearest_expiry,
                "ts": time.time(),
            },
        })

    async def handle_health(self, request: web.Request) -> web.Response:
        """Health check endpoint."""
        return web.Response(text="OK")

    async def handle_debug(self, request: web.Request) -> web.Response:
        """Debug endpoint — diagnose chain polling issues."""
        diag: Dict[str, Any] = {
            "nearest_expiry": self.nearest_expiry,
            "stock_count": len(self.stocks),
            "token_expired": self._token_expired,
            "token_length": len(self.token) if self.token else 0,
        }

        # Count how many stocks have chain data
        chain_populated = sum(1 for s in self.state.values() if s.get("pcr") is not None)
        diag["chain_populated"] = chain_populated
        diag["chain_missing"] = len(self.state) - chain_populated

        # Show first 3 stocks' ikeys for verification
        diag["sample_ikeys"] = [
            {"sym": s.symbol, "ikey": s.ikey, "expiry": s.expiry}
            for s in self.stocks[:3]
        ]

        # Test one chain call live
        if self.stocks and not self._token_expired:
            test_stock = self.stocks[0]
            test_expiry = self.nearest_expiry
            log.info("Debug: testing chain for %s with expiry %s", test_stock.symbol, test_expiry)

            # First, fetch available expiries from the API
            expiry_resp = await self._api_get(UPSTOX_EXPIRY_URL, {"instrument_key": test_stock.ikey})
            diag["expiry_api_response"] = "error" if not expiry_resp else {
                "status": expiry_resp.get("status"),
                "data_count": len(expiry_resp.get("data", [])) if isinstance(expiry_resp.get("data"), list) else "not_a_list",
                "sample_expiries": [
                    c.get("expiry") for c in (expiry_resp.get("data") or [])[:5]
                    if isinstance(c, dict)
                ],
            }

            # Then test the actual chain call
            chain_resp = await self._api_get(UPSTOX_CHAIN_URL, {
                "instrument_key": test_stock.ikey,
                "expiry_date": test_expiry,
            })
            if not chain_resp:
                diag["chain_test"] = "no_response"
            elif "data" not in chain_resp:
                diag["chain_test"] = {"error": "no_data_key", "keys": list(chain_resp.keys()),
                                       "status": chain_resp.get("status"),
                                       "message": chain_resp.get("message", "")}
            else:
                cd = chain_resp["data"]
                diag["chain_test"] = {
                    "status": chain_resp.get("status"),
                    "data_type": type(cd).__name__,
                    "data_length": len(cd) if isinstance(cd, (list, dict)) else "N/A",
                    "first_item_keys": list(cd[0].keys()) if isinstance(cd, list) and cd else None,
                }
        else:
            diag["chain_test"] = "skipped (no stocks or token expired)"

        return web.json_response(diag, dumps=lambda x: json.dumps(x, indent=2, default=str))

    async def handle_paper_enter(self, request: web.Request) -> web.Response:
        """Enter a new paper trade."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        user_id_str = request.headers.get("X-User-Id")
        if not user_id_str:
            user_id_str = request.query.get("user_id")
        if not user_id_str:
            return web.json_response({"error": "Unauthorized: missing user context"}, status=401)
        try:
            user_id = int(user_id_str)
        except ValueError:
            return web.json_response({"error": "Invalid user ID"}, status=400)

        sym = body.get("symbol", "").upper()
        if sym not in self.state:
            return web.json_response({"error": f"Unknown symbol: {sym}"}, status=400)

        option_type = body.get("option_type", "CE").upper()
        if option_type not in ("CE", "PE"):
            return web.json_response({"error": "option_type must be CE or PE"}, status=400)

        st = self.state[sym]
        lots = max(1, int(body.get("lots", 1)))
        entry_premium = body.get("entry_premium")
        if entry_premium is None:
            entry_premium = st.get("atm_ce") if option_type == "CE" else st.get("atm_pe")
        if not entry_premium or entry_premium <= 0:
            return web.json_response({"error": "No premium available"}, status=400)

        entry_premium = round(float(entry_premium), 2)
        lot_size = st.get("lot", 1)
        strike = st.get("atm_strike", 0)
        spot_at_entry = st.get("ltp", 0)

        # Compute SL and targets (hybrid: 15% premium stop)
        sl_premium = round(entry_premium * 0.85, 2)
        risk = entry_premium - sl_premium
        target1 = round(entry_premium + risk * 1.5, 2)
        target2 = round(entry_premium + risk * 2.5, 2)

        # Allow custom SL/targets from body
        if body.get("sl_premium"):
            sl_premium = round(float(body["sl_premium"]), 2)
            risk = entry_premium - sl_premium
            target1 = round(entry_premium + risk * 1.5, 2)
            target2 = round(entry_premium + risk * 2.5, 2)
        if body.get("target1"):
            target1 = round(float(body["target1"]), 2)
        if body.get("target2"):
            target2 = round(float(body["target2"]), 2)

        direction = "BULLISH" if option_type == "CE" else "BEARISH"

        trade_id = self._db.create_paper_trade(
            user_id=user_id,
            symbol=sym,
            direction=direction,
            trade_type="manual",
            strike=strike,
            expiry=st.get("expiry"),
            entry_premium=entry_premium,
            lots=lots,
            lot_size=lot_size,
            sl_premium=sl_premium,
            t1_premium=target1,
            t2_premium=target2,
            status="ENTERED",
            option_type=option_type,
            spot_at_entry=round(spot_at_entry, 2),
            entry_reason="Manual Trade from Dashboard",
        )

        db_row = self._db.get_paper_trade(trade_id)
        if not db_row:
            return web.json_response({"error": "Failed to retrieve created trade"}, status=500)

        trade = self._db_row_to_memory_trade(db_row)
        self.paper_trades.append(trade)

        log.info("Paper trade entered: %s %s %s @ %.2f (lots=%d)",
                 trade["id"], sym, option_type, entry_premium, lots)

        # Broadcast update
        await self._broadcast_paper_trades()

        return web.json_response({"ok": True, "trade": self._paper_trade_to_dict(trade)})

    async def handle_paper_exit(self, request: web.Request) -> web.Response:
        """Exit a paper trade."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        user_id_str = request.headers.get("X-User-Id")
        if not user_id_str:
            user_id_str = request.query.get("user_id")
            
        if not user_id_str:
            return web.json_response({"error": "Unauthorized: missing user context"}, status=401)
        
        user_id = None
        if user_id_str:
            try:
                user_id = int(user_id_str)
            except ValueError:
                pass

        trade_id_raw = body.get("id", "")
        trade_id = None
        try:
            trade_id = int(trade_id_raw)
        except (ValueError, TypeError):
            pass

        exit_reason = body.get("reason", "MANUAL")

        trade = None
        for t in self.paper_trades:
            if t["id"] == trade_id and t["status"] == "OPEN":
                if user_id is not None and t.get("user_id") != user_id:
                    continue
                trade = t
                break

        if not trade:
            return web.json_response({"error": f"No open trade with id: {trade_id_raw}"}, status=404)

        sym = trade["symbol"]
        exit_premium = body.get("exit_premium")
        if exit_premium is None:
            # Use strike-specific premium, not generic ATM
            exit_premium = self._get_strike_premium(trade)
            if exit_premium <= 0:
                exit_premium = trade["entry_premium"]

        exit_premium = round(float(exit_premium), 2)
        qty = trade["lot_size"] * trade["lots"]
        pnl = round((exit_premium - trade["entry_premium"]) * qty, 2)
        pnl_pct = round((pnl / (trade["entry_premium"] * qty) * 100), 2) if (trade["entry_premium"] * qty) != 0 else 0.0
        costs_estimated = 40.0
        net_pnl = round(pnl - costs_estimated, 2)

        # Update DB first
        self._db.update_paper_trade(
            trade["id"],
            status="EXITED",
            exit_premium=exit_premium,
            exit_reason=exit_reason,
            pnl=pnl,
            pnl_pct=pnl_pct,
            costs_estimated=costs_estimated,
            net_pnl=net_pnl,
            exited_at=datetime.utcnow().isoformat(),
        )

        # Remove from memory
        self.paper_trades = [t for t in self.paper_trades if t["id"] != trade["id"]]

        log.info("Paper trade exited: %s %s exit=%.2f reason=%s pnl=%.2f",
                 trade["id"], sym, exit_premium, exit_reason, pnl)

        await self._broadcast_paper_trades()

        # Build response with closed trade fields format
        closed_trade = dict(trade)
        closed_trade.update({
            "status": "CLOSED",
            "exit_time": datetime.now(timezone.utc).isoformat(),
            "exit_premium": exit_premium,
            "exit_reason": exit_reason,
            "final_pnl": pnl,
        })

        return web.json_response({"ok": True, "trade": closed_trade})

    async def handle_paper_trades(self, request: web.Request) -> web.Response:
        """List all paper trades with live P&L for the current user."""
        user_id_str = request.headers.get("X-User-Id")
        if not user_id_str:
            # Fallback to check query params
            user_id_str = request.query.get("user_id")

        if not user_id_str:
            return web.json_response({"error": "Unauthorized or missing X-User-Id"}, status=401)
        
        user_id = int(user_id_str)

        # Open trades in memory for this user
        user_open_trades = [self._paper_trade_to_dict(t) for t in self.paper_trades if t.get("user_id") == user_id]

        # Closed trades in database for this user
        db_closed = self._db.get_paper_trades(user_id=user_id, status="EXITED", limit=100)
        db_closed_alt = self._db.get_paper_trades(user_id=user_id, status="CLOSED", limit=100)
        seen_ids = set()
        user_closed_trades = []
        for r in db_closed + db_closed_alt:
            if r["id"] not in seen_ids:
                seen_ids.add(r["id"])
                user_closed_trades.append(self._db_row_to_memory_trade(r))

        trades = user_open_trades + user_closed_trades

        # Summary stats
        total_open_pnl = sum(t.get("pnl_total", 0) for t in user_open_trades)
        total_closed_pnl = sum(t.get("final_pnl", 0) for t in user_closed_trades if t.get("final_pnl") is not None)
        wins = sum(1 for t in user_closed_trades if (t.get("final_pnl", 0) or 0) > 0)
        losses = sum(1 for t in user_closed_trades if (t.get("final_pnl", 0) or 0) <= 0)

        return web.json_response({
            "trades": trades,
            "summary": {
                "open_count": len(user_open_trades),
                "closed_count": len(user_closed_trades),
                "total_open_pnl": round(total_open_pnl, 2),
                "total_closed_pnl": round(total_closed_pnl, 2),
                "total_pnl": round(total_open_pnl + total_closed_pnl, 2),
                "wins": wins,
                "losses": losses,
                "win_rate": round(wins / (wins + losses) * 100, 1) if (wins + losses) > 0 else 0,
            },
        })

    async def handle_paper_sync(self, request: web.Request) -> web.Response:
        """Reload active paper trades from SQLite (called by auth_proxy when database changes)."""
        db_trades = self._db.get_all_open_paper_trades()
        self.paper_trades = [self._db_row_to_memory_trade(r) for r in db_trades]
        log.info("Synced %d active paper trades from SQLite DB", len(self.paper_trades))
        await self._broadcast_paper_trades()
        return web.json_response({"ok": True, "count": len(self.paper_trades)})

    async def handle_auto_trader_status(self, request: web.Request) -> web.Response:
        """Return auto paper trader status and last scan result."""
        if not self._auto_trader:
            return web.json_response({"enabled": False})
        user_id_str = request.headers.get("X-User-Id")
        if not user_id_str:
            user_id_str = request.query.get("user_id")
        user_id = None
        if user_id_str:
            try:
                user_id = int(user_id_str)
            except ValueError:
                pass
        return web.json_response(self._auto_trader.get_status(user_id))

    async def handle_auto_trader_scan(self, request: web.Request) -> web.Response:
        """Manually trigger an auto-trade scan (for testing)."""
        if not self._auto_trader:
            return web.json_response({"error": "Auto trader not initialized"}, status=500)
        result = await self._auto_trader.scan_and_trade()
        return web.json_response(result)

    async def handle_paper_page(self, request: web.Request) -> web.Response:
        """Serve the paper trading HTML page."""
        html_path = Path(__file__).parent / "paper_trades.html"
        if not html_path.exists():
            return web.Response(
                text="<h1>Paper Trades page not found</h1>",
                content_type="text/html",
                status=404,
            )
        return web.FileResponse(html_path)

    async def handle_advanced_analytics_page(self, request: web.Request) -> web.Response:
        """Serve the advanced analytics dashboard page."""
        html_path = Path(__file__).parent / "advanced-analytics.html"
        if not html_path.exists():
            return web.Response(
                text="<h1>Advanced Analytics page not found</h1>",
                content_type="text/html",
                status=404,
            )
        return web.FileResponse(html_path)
    async def handle_sectors_page(self, request: web.Request) -> web.Response:
        """Serve the sector stocks reference page."""
        html_path = Path(__file__).parent / "sectors.html"
        if not html_path.exists():
            return web.Response(
                text="<h1>Sectors page not found</h1>",
                content_type="text/html",
                status=404,
            )
        return web.FileResponse(html_path)

    async def handle_oi_thesis_page(self, request: web.Request) -> web.Response:
        """Serve the OI thesis tracking page."""
        html_path = Path(__file__).parent / "oi-thesis.html"
        if not html_path.exists():
            return web.Response(
                text="<h1>OI Thesis page not found</h1>",
                content_type="text/html",
                status=404,
            )
        return web.FileResponse(html_path)

    async def handle_api_index_summary(self, request: web.Request) -> web.Response:
        """Return live index ticker data for the top navbar bar."""
        idx = getattr(self, '_index_state', {})
        adv = sum(1 for s in self.state.values() if (s.get("chg_pct") or 0) > 0)
        dec = sum(1 for s in self.state.values() if (s.get("chg_pct") or 0) < 0)
        total = len(self.state)
        nifty_cache = getattr(self, '_nifty_cache', {})
        nifty_data = idx.get("NIFTY50", {}).copy()
        nifty_data["pcr"] = nifty_cache.get("pcr", 0.0)
        nifty_data["max_pain"] = nifty_cache.get("max_pain", 0.0)

        return web.json_response({
            "NIFTY50":     nifty_data,
            "BANKNIFTY":   idx.get("BANKNIFTY",   {}),
            "MIDCAPNIFTY": idx.get("MIDCAPNIFTY", {}),
            "INDIAVIX":    idx.get("INDIAVIX",    {}),
            "breadth": {"adv": adv, "dec": dec, "total": total},
        })

    async def handle_nifty_page(self, request: web.Request) -> web.Response:
        """Serve the Nifty 50 specialized page."""
        html_path = Path(__file__).parent / "nifty.html"
        if not html_path.exists():
            return web.Response(
                text="<h1>Nifty 50 page not found</h1>",
                content_type="text/html",
                status=404,
            )
        return web.FileResponse(html_path)

    async def handle_api_nifty_data(self, request: web.Request) -> web.Response:
        """GET /api/nifty/data
        Returns real-time Nifty 50 spot price, daily change, ATM strike, PCR, 
        and the ATM ± 5 option chain slices.
        Uses a robust caching mechanism to protect Upstox API from rate limits.
        """
        now = time.time()
        
        # Initialize dynamic cache variables if not already present
        if not hasattr(self, "_nifty_cache"):
            self._nifty_cache = None
            self._nifty_cache_time = 0
            
        # Check cache validity (10 seconds expiry)
        cache_duration = 10.0
        if not self._nifty_cache or (now - self._nifty_cache_time) > cache_duration:
            try:
                # 1. Resolve nearest expiry for Nifty 50
                expiry = getattr(self, "nearest_expiry", None)
                if not expiry:
                    expiry = await self._fetch_nearest_expiry("NSE_INDEX|Nifty 50")
                
                if not expiry:
                    log.warning("No option expiry available for Nifty 50")
                else:
                    # 2. Fetch Option Chain from Upstox
                    resp = await self._api_get(UPSTOX_CHAIN_URL, {
                        "instrument_key": "NSE_INDEX|Nifty 50",
                        "expiry_date": expiry,
                    })
                    
                    if resp and "data" in resp and resp["data"]:
                        chain_data = resp["data"]
                        
                        # Resolve underlying spot price from chain
                        chain_spot = 0
                        for item in chain_data:
                            sp = item.get("underlying_spot_price", 0) or 0
                            if sp > 0:
                                chain_spot = sp
                                break
                                
                        # Get index live data
                        nifty_spot_data = self._index_state.get("NIFTY50") or {}
                        spot_price = nifty_spot_data.get("ltp") or chain_spot
                        chg_pct = nifty_spot_data.get("chg_pct") or 0
                        prev_close = nifty_spot_data.get("prev_close") or 0
                        
                        if spot_price <= 0:
                            spot_price = chain_spot
                            
                        # Calculate chg_pct if not set
                        if spot_price > 0 and prev_close > 0 and not chg_pct:
                            chg_pct = round((spot_price - prev_close) / prev_close * 100, 2)
                            
                        # Run chain analysis
                        analytics = analyze_chain(
                            chain_data, spot_price, chg_pct,
                            symbol="NIFTY",
                            pcr_bull_thr=getattr(self, "_pcr_thr_bull", 0.5),
                            pcr_bear_thr=getattr(self, "_pcr_thr_bear", 0.85),
                        )
                        
                        # Update cache
                        self._nifty_cache = {
                            "analytics": analytics,
                            "chain_spot": chain_spot,
                            "expiry": expiry
                        }
                        self._nifty_cache_time = now
                        
                        # Live time-series recording during market hours
                        if DATA_RECORDER_AVAILABLE:
                            try:
                                from datetime import timedelta
                                now_ist = datetime.now(tz=data_recorder.IST)
                                is_weekday = now_ist.weekday() < 5
                                is_market_hours = (9, 15) <= (now_ist.hour, now_ist.minute) <= (15, 30)
                                if is_weekday and is_market_hours:
                                    # Round down to 5-minute interval
                                    discard = timedelta(minutes=now_ist.minute % 5, seconds=now_ist.second, microseconds=now_ist.microsecond)
                                    rounded_ts = (now_ist - discard).isoformat(timespec="seconds")
                                    trading_date = now_ist.date().isoformat()
                                    
                                    # Record nifty timeseries tick
                                    data_recorder.record_nifty_tick(
                                        snap_ts=rounded_ts,
                                        trading_date=trading_date,
                                        expiry=expiry,
                                        spot_ltp=spot_price,
                                        total_oi=analytics.get("total_oi", 0),
                                        total_ce_oi=analytics.get("ce_oi", 0),
                                        total_pe_oi=analytics.get("pe_oi", 0)
                                    )
                                    
                                    # Construct a state-like dict for Nifty option chain snapshot
                                    nifty_state = {
                                        "NIFTY": {
                                            "expiry": expiry,
                                            "ltp": spot_price,
                                            "chg_pct": chg_pct,
                                            "high": nifty_spot_data.get("high") or spot_price,
                                            "low": nifty_spot_data.get("low") or spot_price,
                                            "vol": nifty_spot_data.get("volume") or 0,
                                            "vol_surge": analytics.get("vol_surge") or 1.0,
                                            "vol_surge_5d": analytics.get("vol_surge_5d") or 1.0,
                                            "vol_surge_10d": analytics.get("vol_surge_10d") or 1.0,
                                            "vol_surge_20d": analytics.get("vol_surge_20d") or 1.0,
                                            "vol_confluence": analytics.get("vol_confluence") or "NORMAL",
                                            "total_oi": analytics.get("total_oi", 0),
                                            "ce_oi": analytics.get("ce_oi", 0),
                                            "pe_oi": analytics.get("pe_oi", 0),
                                            "ce_oi_chg": analytics.get("ce_oi_chg", 0),
                                            "pe_oi_chg": analytics.get("pe_oi_chg", 0),
                                            "pcr": analytics.get("pcr", 0.0),
                                            "pcr_sig": analytics.get("pcr_sig", "NEUTRAL"),
                                            "buildup": analytics.get("buildup", "NEUTRAL"),
                                            "max_pain": analytics.get("max_pain", spot_price),
                                            "mp_dist": analytics.get("mp_dist", 0.0),
                                            "atm_strike": analytics.get("atm_strike", spot_price),
                                            "atm_iv": analytics.get("atm_iv", 0.0),
                                            "atm_ce": analytics.get("atm_ce", 0.0),
                                            "atm_pe": analytics.get("atm_pe", 0.0),
                                            "score": analytics.get("score", 0),
                                            "direction": analytics.get("direction", "NEUTRAL"),
                                            "confidence": analytics.get("confidence", "LOW"),
                                            "conviction_tier": analytics.get("conviction_tier", "NONE"),
                                            "strike_map": analytics.get("strike_map", {}),
                                        }
                                    }
                                    
                                    # Save full strike snapshot only if it hasn't been written yet for this 5-minute bar
                                    with data_recorder._connect() as conn:
                                        exists = conn.execute(
                                            "SELECT COUNT(*) FROM chain_snapshot WHERE symbol = 'NIFTY' AND snap_ts = ?",
                                            (rounded_ts,)
                                        ).fetchone()[0]
                                        
                                    if exists == 0:
                                        dt_rounded = datetime.fromisoformat(rounded_ts)
                                        data_recorder.record_snapshot(nifty_state, dt_rounded)
                                        log.info("Recorded Nifty option chain snapshot for %s", rounded_ts)
                            except Exception as exc:
                                log.warning("Failed recording nifty tick or option snapshot: %s", exc)
            except Exception as e:
                log.exception("Error updating Nifty option chain cache: %s", e)
                
        # If cache is still empty/None after attempt, try database fallback
        if not self._nifty_cache:
            if DATA_RECORDER_AVAILABLE:
                try:
                    with data_recorder._connect() as conn:
                        latest_snap = conn.execute(
                            """
                            SELECT snap_ts, trading_date, expiry, spot_ltp, spot_chg_pct, pcr, pcr_sig, buildup, max_pain, atm_strike
                            FROM chain_snapshot
                            WHERE symbol = 'NIFTY'
                            ORDER BY snap_ts DESC LIMIT 1
                            """
                        ).fetchone()
                        
                        if latest_snap:
                            snap_ts = latest_snap["snap_ts"]
                            trading_date = latest_snap["trading_date"]
                            expiry = latest_snap["expiry"]
                            spot_price = latest_snap["spot_ltp"]
                            chg_pct = latest_snap["spot_chg_pct"]
                            pcr = latest_snap["pcr"]
                            pcr_sig = latest_snap["pcr_sig"]
                            buildup = latest_snap["buildup"]
                            max_pain = latest_snap["max_pain"]
                            atm_strike = latest_snap["atm_strike"]
                            
                            earliest_snap = conn.execute(
                                "SELECT snap_ts FROM chain_snapshot WHERE symbol = 'NIFTY' AND trading_date = ? ORDER BY snap_ts ASC LIMIT 1",
                                (trading_date,)
                            ).fetchone()
                            
                            earliest_strikes = {}
                            if earliest_snap:
                                rows = conn.execute(
                                    "SELECT strike, ce_oi, pe_oi FROM chain_strike WHERE symbol = 'NIFTY' AND snap_ts = ?",
                                    (earliest_snap["snap_ts"],)
                                ).fetchall()
                                earliest_strikes = {float(r["strike"]): dict(r) for r in rows}
                                
                            current_rows = conn.execute(
                                "SELECT strike, ce_oi, pe_oi, ce_ltp, pe_ltp, ce_iv, pe_iv FROM chain_strike WHERE symbol = 'NIFTY' AND snap_ts = ?",
                                (snap_ts,)
                            ).fetchall()
                            
                            all_strikes = sorted([float(r["strike"]) for r in current_rows])
                            chain_slice = []
                            if all_strikes:
                                atm_strike_nearest = min(all_strikes, key=lambda s: abs(s - atm_strike))
                                atm_idx = all_strikes.index(atm_strike_nearest)
                                start_idx = max(0, atm_idx - 5)
                                end_idx = min(len(all_strikes) - 1, atm_idx + 5)
                                
                                for i in range(start_idx, end_idx + 1):
                                    s = all_strikes[i]
                                    r = next(x for x in current_rows if float(x["strike"]) == s)
                                    base_data = earliest_strikes.get(s, {})
                                    ce_oi_base = base_data.get("ce_oi") or 0
                                    pe_oi_base = base_data.get("pe_oi") or 0
                                    
                                    chain_slice.append({
                                        "strike": s,
                                        "is_atm": (s == atm_strike),
                                        "ce_oi": r["ce_oi"],
                                        "pe_oi": r["pe_oi"],
                                        "ce_oi_chg_intraday": r["ce_oi"] - ce_oi_base,
                                        "pe_oi_chg_intraday": r["pe_oi"] - pe_oi_base,
                                        "ce_ltp": r["ce_ltp"],
                                        "pe_ltp": r["pe_ltp"],
                                        "ce_iv": r["ce_iv"],
                                        "pe_iv": r["pe_iv"],
                                    })
                                    
                            prev_close = spot_price / (1 + (chg_pct or 0) / 100.0) if chg_pct else spot_price
                            
                            return web.json_response({
                                "symbol": "NIFTY",
                                "spot_price": spot_price,
                                "chg_pct": chg_pct,
                                "prev_close": round(prev_close, 2),
                                "expiry": expiry,
                                "atm_strike": atm_strike,
                                "pcr": pcr,
                                "pcr_sig": pcr_sig,
                                "buildup": buildup,
                                "max_pain": max_pain,
                                "chain": chain_slice,
                                "cache_age": 0.0
                            })
                except Exception as db_exc:
                    log.warning("Database fallback for nifty live data failed: %s", db_exc)
                    
            return web.json_response({"error": "Nifty data not available yet"}, status=404)
            
        analytics = self._nifty_cache["analytics"]
        chain_spot = self._nifty_cache["chain_spot"]
        expiry = self._nifty_cache["expiry"]
        
        # Merge latest live ticks into the response
        nifty_spot_data = self._index_state.get("NIFTY50") or {}
        spot_price = nifty_spot_data.get("ltp") or chain_spot
        chg_pct = nifty_spot_data.get("chg_pct") or 0
        prev_close = nifty_spot_data.get("prev_close") or 0
        
        if spot_price <= 0:
            spot_price = chain_spot
            
        if spot_price > 0 and prev_close > 0 and not chg_pct:
            chg_pct = round((spot_price - prev_close) / prev_close * 100, 2)
            
        # Re-evaluate ATM and strikes slice based on latest live spot price
        atm_strike = float(analytics.get("atm_strike") or 0)
        # Use latest spot_price to find exact ATM strike
        strike_map = analytics.get("strike_map") or {}
        all_strikes = sorted([float(k) for k in strike_map.keys()])
        
        if all_strikes:
            # Find nearest strike to the latest spot price
            atm_strike = min(all_strikes, key=lambda s: abs(s - spot_price))
            atm_idx = all_strikes.index(atm_strike)
            
            # Slice ATM ± 5 strikes
            start_idx = max(0, atm_idx - 5)
            end_idx = min(len(all_strikes) - 1, atm_idx + 5)
            
            # Fetch earliest NIFTY snapshot of the day to compute intraday changes
            earliest_strikes = {}
            if DATA_RECORDER_AVAILABLE:
                try:
                    trading_date = data_recorder._trading_date_for(datetime.now(tz=data_recorder.IST))
                    with data_recorder._connect() as conn:
                        earliest_snap = conn.execute(
                            "SELECT snap_ts FROM chain_snapshot WHERE symbol = 'NIFTY' AND trading_date = ? ORDER BY snap_ts ASC LIMIT 1",
                            (trading_date,)
                        ).fetchone()
                        if earliest_snap:
                            rows = conn.execute(
                                "SELECT strike, ce_oi, pe_oi FROM chain_strike WHERE symbol = 'NIFTY' AND snap_ts = ?",
                                (earliest_snap[0],)
                            ).fetchall()
                            earliest_strikes = {float(r["strike"]): dict(r) for r in rows}
                except Exception as exc:
                    log.warning("Failed fetching Nifty earliest snapshot for changes: %s", exc)

            chain_slice = []
            for i in range(start_idx, end_idx + 1):
                s = all_strikes[i]
                data = strike_map[s]
                base_data = earliest_strikes.get(s, {})
                ce_oi_base = base_data.get("ce_oi") or 0
                pe_oi_base = base_data.get("pe_oi") or 0
                
                chain_slice.append({
                    "strike": s,
                    "is_atm": (s == atm_strike),
                    "ce_oi": data.get("ce_oi", 0),
                    "pe_oi": data.get("pe_oi", 0),
                    "ce_oi_chg_intraday": data.get("ce_oi", 0) - ce_oi_base,
                    "pe_oi_chg_intraday": data.get("pe_oi", 0) - pe_oi_base,
                    "ce_ltp": data.get("ce_ltp", 0),
                    "pe_ltp": data.get("pe_ltp", 0),
                    "ce_iv": data.get("ce_iv", 0),
                    "pe_iv": data.get("pe_iv", 0),
                })
        else:
            chain_slice = []
            
        return web.json_response({
            "symbol": "NIFTY",
            "spot_price": spot_price,
            "chg_pct": chg_pct,
            "prev_close": prev_close,
            "expiry": expiry,
            "atm_strike": atm_strike,
            "pcr": analytics.get("pcr"),
            "pcr_sig": analytics.get("pcr_sig"),
            "buildup": analytics.get("buildup"),
            "max_pain": analytics.get("max_pain"),
            "chain": chain_slice,
            "cache_age": round(now - self._nifty_cache_time, 2)
        })


    async def handle_api_nifty_timeseries(self, request: web.Request) -> web.Response:
        """GET /api/nifty/timeseries
        Returns Nifty 50 historical 5-minute OI ticks.
        """
        try:
            if not DATA_RECORDER_AVAILABLE:
                return web.json_response({"error": "Data recorder is not available"}, status=500)
            
            # Fetch from data_recorder
            data = data_recorder.get_nifty_timeseries()
            return web.json_response(data)
        except Exception as e:
            log.exception("Error in handle_api_nifty_timeseries: %s", e)
            return web.json_response({"error": str(e)}, status=500)


    async def handle_api_nifty_multi_strike_oi(self, request: web.Request) -> web.Response:
        """GET /api/nifty/multi-strike-oi
        Returns Nifty 50 historical option chain data by strike over time.
        Query params:
          - strikes: Comma-separated list of float values (e.g. 23700,23750,23800)
          - trading_date: YYYY-MM-DD (defaults to today)
        """
        try:
            if not DATA_RECORDER_AVAILABLE:
                return web.json_response({"error": "Data recorder is not available"}, status=500)
                
            now_ist = datetime.now(tz=data_recorder.IST)
            trading_date = request.query.get("trading_date", data_recorder._trading_date_for(now_ist)).strip()
            strikes_param = request.query.get("strikes", "").strip()
            
            selected_strikes = []
            if strikes_param:
                try:
                    selected_strikes = [float(s.strip()) for s in strikes_param.split(",") if s.strip()]
                except Exception:
                    return web.json_response({"error": "Invalid strikes format. Must be comma-separated numbers."}, status=400)
                    
            with data_recorder._connect() as conn:
                # If no strikes specified, auto-resolve ATM from the latest snapshot
                if not selected_strikes:
                    row = conn.execute(
                        """
                        SELECT atm_strike 
                        FROM chain_snapshot 
                        WHERE symbol = 'NIFTY' AND trading_date = ? 
                        ORDER BY snap_ts DESC LIMIT 1
                        """,
                        (trading_date,)
                    ).fetchone()
                    
                    if row and row[0]:
                        atm = float(row[0])
                        # Auto select 5 strikes around ATM (ATM-100, ATM-50, ATM, ATM+50, ATM+100)
                        selected_strikes = [atm + i * 50.0 for i in range(-2, 3)]
                    else:
                        # Fallback default strikes
                        selected_strikes = [23600.0, 23650.0, 23700.0, 23750.0, 23800.0]
                        
                # Fetch Nifty spot price snapshots for today
                snapshots = conn.execute(
                    """
                    SELECT snap_ts, spot_ltp 
                    FROM chain_snapshot 
                    WHERE symbol = 'NIFTY' AND trading_date = ? 
                    ORDER BY snap_ts ASC
                    """,
                    (trading_date,)
                ).fetchall()
                
                if not snapshots:
                    return web.json_response([])
                    
                # Build placeholder for SQLite IN clause
                placeholders = ",".join("?" for _ in selected_strikes)
                
                # Fetch all strike level ticks for selected strikes today
                strike_query = f"""
                    SELECT snap_ts, strike, ce_oi, pe_oi, ce_ltp, pe_ltp
                    FROM chain_strike
                    WHERE symbol = 'NIFTY' AND trading_date = ? AND strike IN ({placeholders})
                    ORDER BY snap_ts ASC, strike ASC
                """
                params = [trading_date] + selected_strikes
                strike_rows = conn.execute(strike_query, params).fetchall()
                
            # Group strike rows by snap_ts
            strike_data_by_ts = {}
            for row in strike_rows:
                ts = row["snap_ts"]
                if ts not in strike_data_by_ts:
                    strike_data_by_ts[ts] = {}
                strike_data_by_ts[ts][str(row["strike"])] = {
                    "ce_oi": row["ce_oi"],
                    "pe_oi": row["pe_oi"],
                    "ce_ltp": row["ce_ltp"],
                    "pe_ltp": row["pe_ltp"]
                }
                
            # Build final time series payload
            payload = []
            for row in snapshots:
                ts = row["snap_ts"]
                # Convert snap_ts string to unix epoch timestamp for Lightweight Charts
                try:
                    dt = datetime.fromisoformat(ts)
                    unix_ts = int(dt.timestamp())
                except Exception:
                    continue
                    
                strikes_map = strike_data_by_ts.get(ts, {})
                
                payload.append({
                    "time": unix_ts,
                    "snap_ts": ts,
                    "spot_ltp": row["spot_ltp"],
                    "strikes": strikes_map
                })
                
            return web.json_response(payload)
        except Exception as e:
            log.exception("Error in handle_api_nifty_multi_strike_oi: %s", e)
            return web.json_response({"error": str(e)}, status=500)


    async def handle_api_nifty_chart(self, request: web.Request) -> web.Response:
        """GET /api/nifty/chart.png?interval=5minute
        Generates a premium server-side candlestick chart with Call/Put OI change volume histograms.
        """
        try:
            import io
            import urllib.parse
            import aiohttp
            import matplotlib
            matplotlib.use('Agg')
            import matplotlib.pyplot as plt
            import matplotlib.dates as mdates
            import pandas as pd
            from datetime import datetime

            interval = request.query.get("interval", "5minute").strip()

            # 1. Fetch Nifty Spot Candles
            candles = []
            instrument_key = "NSE_INDEX|Nifty 50"
            encoded_key = urllib.parse.quote(instrument_key, safe="")
            url = UPSTOX_CANDLES_URL.format(instrument_key=encoded_key, interval="1minute")

            headers = {
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
            }

            raw_candles = []
            upstream_ok = False
            try:
                async with self.session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=4)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        raw_candles = data.get("data", {}).get("candles", [])
                        upstream_ok = True
            except Exception:
                pass

            if upstream_ok and raw_candles:
                for c in raw_candles:
                    if len(c) < 6:
                        continue
                    try:
                        ts_str, open_, high, low, close, volume = c[0], c[1], c[2], c[3], c[4], c[5]
                        oi = c[6] if len(c) > 6 else 0
                        dt = datetime.fromisoformat(ts_str)
                        unix_ts = int(dt.timestamp())
                        candles.append({
                            "time": unix_ts,
                            "open": open_,
                            "high": high,
                            "low": low,
                            "close": close,
                            "volume": volume,
                            "oi": oi,
                        })
                    except Exception:
                        continue
                candles.sort(key=lambda x: x["time"])
            else:
                candles = self.get_research_fallback_candles("NIFTY")

            # Resample candles if interval is not 1minute
            if interval != "1minute" and candles:
                minutes = 5
                if interval == "3minute": minutes = 3
                elif interval == "15minute": minutes = 15
                elif interval == "30minute": minutes = 30
                
                resampled = []
                chunk = []
                for c in candles:
                    if not chunk:
                        chunk = [c]
                    elif (c["time"] // (minutes * 60)) * (minutes * 60) == (chunk[0]["time"] // (minutes * 60)) * (minutes * 60):
                        chunk.append(c)
                    else:
                        resampled.append({
                            "time": (chunk[0]["time"] // (minutes * 60)) * (minutes * 60),
                            "open": chunk[0]["open"],
                            "high": max(x["high"] for x in chunk),
                            "low": min(x["low"] for x in chunk),
                            "close": chunk[-1]["close"],
                            "volume": sum(x["volume"] for x in chunk),
                            "oi": chunk[-1]["oi"]
                        })
                        chunk = [c]
                if chunk:
                    resampled.append({
                        "time": (chunk[0]["time"] // (minutes * 60)) * (minutes * 60),
                        "open": chunk[0]["open"],
                        "high": max(x["high"] for x in chunk),
                        "low": min(x["low"] for x in chunk),
                        "close": chunk[-1]["close"],
                        "volume": sum(x["volume"] for x in chunk),
                        "oi": chunk[-1]["oi"]
                    })
                candles = resampled

            if not candles:
                return web.Response(text="No candle data available", status=404)

            # 2. Fetch Timeseries data for the OI Change histograms
            ts_rows = []
            if DATA_RECORDER_AVAILABLE:
                ts_rows = data_recorder.get_nifty_timeseries()
            # Sort chronological
            ts_rows = sorted(ts_rows, key=lambda x: x["snap_ts"])

            ts_dict = {}
            for i, curr in enumerate(ts_rows):
                dt = datetime.fromisoformat(curr["snap_ts"])
                unix_ts = int(dt.timestamp())
                
                pe_diff = 0
                ce_diff = 0
                if i > 0:
                    prev = ts_rows[i - 1]
                    pe_diff = curr["total_pe_oi"] - prev["total_pe_oi"]
                    ce_diff = curr["total_ce_oi"] - prev["total_ce_oi"]
                
                ts_dict[unix_ts] = {"pe_oi_chg": pe_diff, "ce_oi_chg": ce_diff}

            candle_df = pd.DataFrame(candles)
            candle_df['datetime'] = pd.to_datetime(candle_df['time'], unit='s')
            
            pe_changes = []
            ce_changes = []
            for t in candle_df['time']:
                closest_t = min(ts_dict.keys(), key=lambda x: abs(x - t)) if ts_dict else None
                if closest_t and abs(closest_t - t) <= 900:
                    pe_changes.append(ts_dict[closest_t]["pe_oi_chg"])
                    ce_changes.append(ts_dict[closest_t]["ce_oi_chg"])
                else:
                    pe_changes.append(0)
                    ce_changes.append(0)
            
            candle_df['pe_oi_chg'] = pe_changes
            candle_df['ce_oi_chg'] = ce_changes

            # 3. Render the Matplotlib Figure
            plt.style.use('dark_background')
            fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 5.5), sharex=True, 
                                           gridspec_kw={'height_ratios': [3.5, 1.2]})
            fig.patch.set_facecolor('#09090b')
            ax1.set_facecolor('#131722')
            ax2.set_facecolor('#131722')

            # Draw Candlesticks
            for idx, row in candle_df.iterrows():
                is_up = row['close'] >= row['open']
                color = '#10b981' if is_up else '#ef4444'
                # Draw wick
                ax1.vlines(row['datetime'], row['low'], row['high'], color=color, linewidth=1)
                # Draw body
                open_val = row['open']
                close_val = row['close']
                if open_val == close_val:
                    ax1.hlines(open_val, row['datetime'] - pd.Timedelta(seconds=120), row['datetime'] + pd.Timedelta(seconds=120), color=color, linewidth=2)
                else:
                    top = max(open_val, close_val)
                    bottom = min(open_val, close_val)
                    ax1.bar(row['datetime'], top - bottom, bottom=bottom, color=color, width=pd.Timedelta(seconds=240), align='center')

            ax1.set_title(f"NIFTY 50 SPOT CANDLESTICK ({interval.upper()})", color='#fafafa', fontsize=12, fontweight='bold', pad=10)
            ax1.grid(True, color='#27272a', linestyle='--', linewidth=0.5)
            ax1.tick_params(colors='#71717a', labelsize=9)
            ax1.spines['bottom'].set_color('#27272a')
            ax1.spines['top'].set_color('#27272a')
            ax1.spines['left'].set_color('#27272a')
            ax1.spines['right'].set_color('#27272a')

            # Draw Put/Call OI Change on ax2
            ax2.bar(candle_df['datetime'], candle_df['pe_oi_chg'], color='#10b981', alpha=0.65, width=pd.Timedelta(seconds=240), label='Put OI Chg (Bullish)')
            ax2.bar(candle_df['datetime'], candle_df['ce_oi_chg'], color='#ef4444', alpha=0.65, width=pd.Timedelta(seconds=240), label='Call OI Chg (Bearish)')
            ax2.axhline(0, color='#3f3f46', linewidth=0.8)
            ax2.grid(True, color='#27272a', linestyle='--', linewidth=0.5)
            ax2.tick_params(colors='#71717a', labelsize=9)
            ax2.spines['bottom'].set_color('#27272a')
            ax2.spines['top'].set_color('#27272a')
            ax2.spines['left'].set_color('#27272a')
            ax2.spines['right'].set_color('#27272a')
            ax2.legend(loc='upper right', frameon=False, fontsize=8, labelcolor='#a1a1aa')

            # Format X-axis
            ax2.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
            
            plt.tight_layout()
            
            # Save to BytesIO
            buf = io.BytesIO()
            plt.savefig(buf, format='png', dpi=120, bbox_inches='tight', facecolor=fig.get_facecolor())
            plt.close(fig)
            buf.seek(0)
            
            return web.Response(body=buf.read(), content_type='image/png')
            
        except Exception as e:
            log.exception("Error generating Matplotlib nifty chart: %s", e)
            try:
                import io
                import matplotlib
                matplotlib.use('Agg')
                import matplotlib.pyplot as plt
                fig, ax = plt.subplots(figsize=(6, 3))
                fig.patch.set_facecolor('#09090b')
                ax.set_facecolor('#131722')
                ax.text(0.5, 0.5, f"Chart Rendering Error:\n{str(e)[:100]}", 
                        color='#ef4444', ha='center', va='center', fontsize=10, fontweight='bold')
                ax.axis('off')
                buf = io.BytesIO()
                plt.savefig(buf, format='png', dpi=100, bbox_inches='tight', facecolor=fig.get_facecolor())
                plt.close(fig)
                buf.seek(0)
                return web.Response(body=buf.read(), content_type='image/png')
            except Exception:
                return web.Response(text=f"Error generating chart: {e}", status=500)


    async def handle_api_oi_scanner(self, request: web.Request) -> web.Response:
        """
        Live OI Scanner — ranks stocks by user's bear/bull configuration rule.

        Rule (Convention A — seller/writer interpretation):
          BULL setup: CE_oi_chg < 0 (writers covering ceiling) AND PE_oi_chg > 0 (writers adding floor)
          BEAR setup: CE_oi_chg > 0 (writers adding ceiling)   AND PE_oi_chg < 0 (writers covering floor)

        Ranks within each side by |PE_chg − CE_chg| magnitude (the position-size measure).

        For each candidate, computes:
          - Suggested entry strike (ATM CE for bull, ATM PE for bear) + premium
          - Call wall  = strike with max CE OI  (acts as resistance / SL for CE buys)
          - Put wall   = strike with max PE OI  (acts as support / SL for PE buys)
          - Max-pain distance, IV regime, vol_surge
          - Implied SL distance (% from spot)
          - Liquidity flag (skip if total_oi < floor or premium < 5)

        Query params:
          side: 'bull' | 'bear' | 'both' (default 'both')
          min_total_oi: liquidity floor (default 100000)
          min_vol_surge: skip stale (default 0.0 = disabled)
          top_n: max rows per side (default 20)
        """
        side_filter = (request.query.get("side") or "both").lower()
        min_oi = int(request.query.get("min_total_oi") or 100000)
        min_surge = float(request.query.get("min_vol_surge") or 0)
        top_n = int(request.query.get("top_n") or 20)

        bulls = []
        bears = []
        for sym, st in self.state.items():
            ce_chg = st.get("ce_oi_chg") or 0
            pe_chg = st.get("pe_oi_chg") or 0
            total_oi = st.get("total_oi") or 0
            vol_surge = st.get("vol_surge") or 0

            if total_oi < min_oi:
                continue
            if min_surge > 0 and vol_surge < min_surge:
                continue

            spot = st.get("ltp") or 0
            atm_strike = st.get("atm_strike")
            atm_ce = st.get("atm_ce") or st.get("atm_ce_ltp")
            atm_pe = st.get("atm_pe") or st.get("atm_pe_ltp")
            atm_iv = st.get("atm_iv")
            max_pain = st.get("max_pain")
            mp_dist = st.get("mp_dist")
            pcr = st.get("pcr")
            sector = st.get("sector")
            lot_size = st.get("lot") or 0

            # ── Rupee conversion (notional + premium) ──
            # Upstox returns ce_oi_chg / pe_oi_chg in SHARES (not lots).
            # Notional ₹: shares × spot — this is the "underlying exposure that opened/closed".
            # Premium ₹: shares × ATM premium of that leg — actual cash that changed hands.
            ce_chg_rs_notional = int(ce_chg * spot)        if spot else 0
            pe_chg_rs_notional = int(pe_chg * spot)        if spot else 0
            ce_chg_rs_premium  = int(ce_chg * (atm_ce or 0))
            pe_chg_rs_premium  = int(pe_chg * (atm_pe or 0))

            # Walls from strike_map (highest CE OI = call wall, highest PE OI = put wall)
            strike_map = st.get("strike_map") or {}
            call_wall = None
            call_wall_oi = 0
            put_wall = None
            put_wall_oi = 0
            for k, leg in strike_map.items():
                ce_oi = (leg or {}).get("ce_oi") or 0
                pe_oi = (leg or {}).get("pe_oi") or 0
                if ce_oi > call_wall_oi:
                    call_wall_oi = ce_oi
                    call_wall = float(k)
                if pe_oi > put_wall_oi:
                    put_wall_oi = pe_oi
                    put_wall = float(k)

            # SL distance: for bull (buying CE), SL is below put wall (support breaks down)
            #              for bear (buying PE), SL is above call wall (resistance breaks up)
            sl_pct_bull = None
            if put_wall and spot > 0:
                sl_pct_bull = round(((spot - put_wall) / spot) * 100, 2)
            sl_pct_bear = None
            if call_wall and spot > 0:
                sl_pct_bear = round(((call_wall - spot) / spot) * 100, 2)

            base = {
                "symbol": sym,
                "sector": sector,
                "spot": spot,
                "lot_size": lot_size,
                "chg_pct": st.get("chg_pct"),
                "ce_oi_chg": ce_chg,
                "pe_oi_chg": pe_chg,
                "net_oi": st.get("net_oi"),           # PE - CE (bull/bear bias from poll_oi_fast)
                "net_oi_chg": ce_chg + pe_chg,        # CE + PE (total OI change for buildup)
                "net_thesis_bull": pe_chg - ce_chg,   # |this| is bull magnitude
                "net_thesis_bear": ce_chg - pe_chg,   # |this| is bear magnitude
                # ── Rupee conversions ──
                "ce_oi_chg_rs_notional": ce_chg_rs_notional,
                "pe_oi_chg_rs_notional": pe_chg_rs_notional,
                "net_oi_rs_notional":    pe_chg_rs_notional - ce_chg_rs_notional,
                "ce_oi_chg_rs_premium":  ce_chg_rs_premium,
                "pe_oi_chg_rs_premium":  pe_chg_rs_premium,
                "net_oi_rs_premium":     pe_chg_rs_premium - ce_chg_rs_premium,
                "total_oi": total_oi,
                "vol_surge": vol_surge,
                "vol_surge_5d":  st.get("vol_surge_5d") or vol_surge,
                "vol_surge_10d": st.get("vol_surge_10d") or 0.0,
                "vol_surge_20d": st.get("vol_surge_20d") or 0.0,
                "vol_confluence": st.get("vol_confluence"),
                "pcr": pcr,
                "max_pain": max_pain,
                "mp_dist_pct": mp_dist,
                "atm_strike": atm_strike,
                "atm_iv": atm_iv,
                "atm_ce_premium": atm_ce,
                "atm_pe_premium": atm_pe,
                "call_wall": call_wall,
                "call_wall_oi": call_wall_oi,
                "put_wall": put_wall,
                "put_wall_oi": put_wall_oi,
                "buildup": st.get("buildup"),
                "score": st.get("score"),
                "expiry": st.get("expiry"),
            }

            if ce_chg < 0 and pe_chg > 0:
                # Bull setup
                row = dict(base)
                row["side"] = "bull"
                row["magnitude"] = abs(pe_chg - ce_chg)
                row["suggested_strike"] = atm_strike
                row["suggested_premium"] = atm_ce
                row["suggested_leg"] = "CE"
                row["sl_pct"] = sl_pct_bull        # negative = put wall below spot
                row["sl_level"] = put_wall
                bulls.append(row)
            elif ce_chg > 0 and pe_chg < 0:
                # Bear setup
                row = dict(base)
                row["side"] = "bear"
                row["magnitude"] = abs(ce_chg - pe_chg)
                row["suggested_strike"] = atm_strike
                row["suggested_premium"] = atm_pe
                row["suggested_leg"] = "PE"
                row["sl_pct"] = sl_pct_bear        # positive = call wall above spot
                row["sl_level"] = call_wall
                bears.append(row)

        bulls.sort(key=lambda r: r["magnitude"], reverse=True)
        bears.sort(key=lambda r: r["magnitude"], reverse=True)

        result = {
            "ts": time.time(),
            "config": {
                "side": side_filter,
                "min_total_oi": min_oi,
                "min_vol_surge": min_surge,
                "top_n": top_n,
                "rule": "Convention A: bull = CE↓+PE↑ (writers covering calls, adding puts); bear = CE↑+PE↓",
            },
        }
        if side_filter in ("bull", "both"):
            result["bull"] = bulls[:top_n]
        if side_filter in ("bear", "both"):
            result["bear"] = bears[:top_n]
        result["counts"] = {
            "bull_total": len(bulls),
            "bear_total": len(bears),
            "universe": len(self.state),
        }
        return web.json_response(result)

    async def handle_oi_scanner_page(self, request: web.Request) -> web.Response:
        """Serve the OI Scanner page."""
        html_path = Path(__file__).parent / "oi-scanner.html"
        if not html_path.exists():
            return web.Response(text="<h1>OI Scanner page not found</h1>",
                                content_type="text/html", status=404)
        return web.FileResponse(html_path)

    async def handle_api_oi_thesis(self, request: web.Request) -> web.Response:
        """Return today's flags + recent outcomes as JSON for the /oi-thesis page."""
        try:
            import oi_thesis_tracker as tracker
        except Exception as exc:
            return web.json_response({"error": f"tracker unavailable: {exc}"}, status=500)

        n_days = int(request.query.get("days", "30"))
        rule_id = request.query.get("rule_id", "oi_div_v1")
        conn = tracker._connect()
        try:
            latest_flag_date = conn.execute(
                "SELECT MAX(flag_date) FROM oi_thesis_flag WHERE rule_id = ?",
                (rule_id,),
            ).fetchone()[0]

            today_flags = []
            if latest_flag_date:
                today_flags = [dict(r) for r in conn.execute("""
                    SELECT * FROM oi_thesis_flag
                    WHERE flag_date = ? AND rule_id = ?
                    ORDER BY side, rank
                """, (latest_flag_date, rule_id)).fetchall()]

            from datetime import datetime as _dt, timedelta as _td, timezone as _tz
            cutoff = (_dt.now(_tz(_td(hours=5, minutes=30))) - _td(days=n_days)).date().isoformat()

            outcomes = [dict(r) for r in conn.execute("""
                SELECT o.*, f.rank, f.net_thesis, f.pcr_at_flag, f.surge_at_flag,
                       f.ce_oi_chg, f.pe_oi_chg, f.total_oi
                FROM oi_thesis_outcome o
                JOIN oi_thesis_flag f USING (flag_date, rule_id, symbol, side)
                WHERE o.flag_date >= ? AND o.rule_id = ?
                ORDER BY o.flag_date DESC, f.side, f.rank
            """, (cutoff, rule_id)).fetchall()]

            stats_rows = conn.execute("""
                SELECT side,
                       COUNT(*) AS n,
                       SUM(win_peak_loose) AS w_loose,
                       SUM(win_peak_strict) AS w_strict,
                       SUM(win_close_only) AS w_close,
                       AVG(chg_pct_close) AS avg_close,
                       AVG(chg_pct_peak)  AS avg_peak
                FROM oi_thesis_outcome
                WHERE flag_date >= ? AND rule_id = ?
                GROUP BY side
            """, (cutoff, rule_id)).fetchall()
            stats = {r["side"]: dict(r) for r in stats_rows}
        finally:
            conn.close()

        return web.json_response({
            "rule_id": rule_id,
            "latest_flag_date": latest_flag_date,
            "today_flags": today_flags,
            "outcomes": outcomes,
            "stats": stats,
            "config": {
                "peak_pct_win":    tracker.PEAK_PCT_WIN,
                "peak_pct_strict": tracker.PEAK_PCT_STRICT,
                "top_n_per_side":  tracker.TOP_N_PER_SIDE,
                "liquidity_floor": tracker.LIQUIDITY_FLOOR_OI,
                "n_days":          n_days,
            },
        })

    async def handle_admin_page(self, request: web.Request) -> web.Response:
        """Serve the admin panel HTML page."""
        html_path = Path(__file__).parent / "admin.html"
        if not html_path.exists():
            return web.Response(
                text="<h1>Admin page not found</h1>",
                content_type="text/html",
                status=404,
            )
        return web.FileResponse(html_path)

    async def handle_rsi_page(self, request: web.Request) -> web.Response:
        """Serve the RSI Scanner page."""
        html_path = Path(__file__).parent / "rsi.html"
        if not html_path.exists():
            return web.json_response({"error": "File not found"})
        return web.FileResponse(html_path)

    async def handle_admin_status(self, request: web.Request) -> web.Response:
        """Return server status and diagnostics."""
        uptime = time.time() - self._start_time
        open_trades = sum(1 for t in self.paper_trades if t["status"] == "OPEN")
        closed_trades = sum(1 for t in self.paper_trades if t["status"] == "CLOSED")
        chain_populated = sum(1 for s in self.state.values() if s.get("pcr") is not None)

        # Upstox WS streamer status
        ws_stream_status = self._upstox_streamer.get_status() if self._upstox_streamer else {"enabled": False}
        # Auto paper trader status
        auto_trader_status = self._auto_trader.get_status() if self._auto_trader else {"enabled": False}

        fyers_token = os.environ.get("FYERS_ACCESS_TOKEN", "")
        fyers_preview = fyers_token[:10] + "..." + fyers_token[-6:] if len(fyers_token) > 20 else "(short)"

        return web.json_response({
            "uptime_seconds": round(uptime),
            "uptime_human": f"{int(uptime // 3600)}h {int((uptime % 3600) // 60)}m {int(uptime % 60)}s",
            "ws_clients": len(self.ws_clients),
            "stocks_loaded": len(self.stocks),
            "chain_populated": chain_populated,
            "nearest_expiry": self.nearest_expiry,
            "token_expired": self._token_expired,
            "token_length": len(self.token),
            "token_preview": self.token[:10] + "..." + self.token[-6:] if len(self.token) > 20 else "(short)",
            "fyers_app_id": os.environ.get("FYERS_APP_ID", ""),
            "fyers_redirect_uri": os.environ.get("FYERS_REDIRECT_URI", ""),
            "fyers_token_length": len(fyers_token),
            "fyers_token_preview": fyers_preview,
            "paper_trades_open": open_trades,
            "paper_trades_closed": closed_trades,
            "paper_trades_total": len(self.paper_trades),
            "polling_active": self._running,
            "upstox_ws_stream": ws_stream_status,
            "auto_paper_trader": auto_trader_status,
            "ltp_source": "websocket" if (self._upstox_streamer and self._upstox_streamer.connected) else "rest_polling",
            "port": self.port,
            "data_dir": str(self.store.base_dir),
            "target_expiry_index": self.target_expiry_index,
        })

    async def handle_admin_rollover(self, request: web.Request) -> web.Response:
        """Toggle TARGET_EXPIRY_INDEX and purge cache to rollover to next month's expiry."""
        try:
            data = await request.json()
            idx = int(data.get("target_expiry_index", 0))
            self.target_expiry_index = idx
            log.info("Rollover triggered. Target expiry index set to %d.", idx)
            
            # Stop existing streamers to prevent old ticks from arriving
            if self._upstox_streamer:
                await self._upstox_streamer.stop()
            if hasattr(self, '_option_streamer') and self._option_streamer:
                await self._option_streamer.stop()
            
            # Purge all option chain caches
            self.state = {}
            self._option_oi_state = {}
            self.strike_map = {}
            self.strike_reverse_map = {}
            
            # Re-initialize instruments to pick up the new expiry contracts
            self.ikey_to_symbol = {}
            self.colon_key_to_symbol = {}
            self.stock_map = {}
            
            # The async loop handles bootstrapping
            # We just trigger an immediate bootstrap by setting _last_chain_fetch_time far in past
            self._last_chain_fetch_time = 0
            
            # Reload from downloaded raw csv
            instruments = download_and_parse_instruments(target_expiry_index=self.target_expiry_index)
            if instruments:
                self.stocks = instruments
                for s in self.stocks:
                    self.stock_map[s.symbol] = s
                    self.ikey_to_symbol[s.ikey] = s.symbol
                    self.colon_key_to_symbol[s.ikey.replace("|", ":")] = s.symbol
                log.info("Successfully re-loaded %d instruments for expiry index %d.", len(self.stocks), self.target_expiry_index)
                if self.stocks:
                    self.nearest_expiry = self.stocks[0].expiry
                    log.info("Nearest expiry is now: %s", self.nearest_expiry)
                    
                # 1. Initialize the new state dictionary with base fields (symbol, sector, etc.)
                self._init_state()
                
                # 2. Restart UpstoxStreamer with the newly resolved instrument ikeys
                instrument_keys = [s.ikey for s in self.stocks]
                INDEX_KEYS = {
                    "NSE_INDEX|Nifty 50":          "NIFTY50",
                    "NSE_INDEX|Nifty Bank":        "BANKNIFTY",
                    "NSE_INDEX|NIFTY MID SELECT":  "MIDCAPNIFTY",
                    "NSE_INDEX|India VIX":         "INDIAVIX",
                }
                all_keys = instrument_keys + list(INDEX_KEYS.keys())
                for ikey, sym in INDEX_KEYS.items():
                    self.ikey_to_symbol[ikey] = sym

                self._upstox_streamer = UpstoxStreamer(
                    token=self.token,
                    instrument_keys=all_keys,
                    ikey_to_symbol=self.ikey_to_symbol,
                    on_tick=self._handle_ws_tick,
                    on_status=self._handle_ws_status,
                    session=self.session,
                    mode=os.environ.get("WS_FEED_MODE", "ltpc"),
                )
                self._tasks.append(asyncio.create_task(self._upstox_streamer.run()))
                    
                # 3. Rebuild strike maps and fetch market prices
                await self._bootstrap_market_data()
                
                # 4. Broadcast the new init payload immediately to all connected browsers
                await self._broadcast_init()
            
            return web.json_response({"ok": True, "target_expiry_index": self.target_expiry_index, "nearest_expiry": self.nearest_expiry})
        except Exception as e:
            log.exception("Error in handle_admin_rollover")
            return web.json_response({"ok": False, "error": str(e)}, status=500)

    async def handle_admin_token(self, request: web.Request) -> web.Response:
        """Update the Upstox access token at runtime (no restart needed)."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        new_token = (body.get("token") or "").strip()
        if not new_token:
            return web.json_response({"error": "Token is empty"}, status=400)
        if len(new_token) < 20:
            return web.json_response({"error": "Token looks too short"}, status=400)

        old_len = len(self.token)
        self.token = new_token
        self._token_expired = False
        self._get_token_event().set()  # Wake up all poll loops immediately

        # Also update config.env on disk for next restart
        config_path = Path(__file__).parent / "config.env"
        try:
            lines = []
            if config_path.exists():
                with open(config_path, "r") as f:
                    for line in f:
                        if line.strip().startswith("UPSTOX_ACCESS_TOKEN"):
                            lines.append(f"UPSTOX_ACCESS_TOKEN={new_token}\n")
                        else:
                            lines.append(line)
            else:
                lines = [f"UPSTOX_ACCESS_TOKEN={new_token}\n"]
            with open(config_path, "w") as f:
                f.writelines(lines)
            log.info("Token updated in config.env")
        except Exception as exc:
            log.warning("Failed to update config.env: %s", exc)

        # Persist token in settings too
        self._settings["upstox_token"] = new_token
        self.store.save_settings(self._settings)

        # Update the Upstox WS streamer's token (triggers reconnect with new token)
        if self._upstox_streamer:
            self._upstox_streamer.update_token(new_token)
            log.info("Token forwarded to Upstox WS streamer (will reconnect)")

        log.info("Token hot-swapped: %d -> %d chars", old_len, len(new_token))
        await self._broadcast_status("Token updated successfully.", "info")

        return web.json_response({
            "ok": True,
            "token_length": len(new_token),
            "token_preview": new_token[:10] + "..." + new_token[-6:],
        })


    async def handle_admin_test_broker(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "msg": "Invalid JSON"})

        broker = body.get("broker")
        if broker == "upstox":
            token = os.environ.get("UPSTOX_ACCESS_TOKEN", "")
            if not token:
                return web.json_response({"ok": False, "msg": "Upstox token not found in environment."})
            
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Api-Version": "2.0"
            }
            url = "https://api.upstox.com/v2/market-quote/quotes?instrument_key=NSE_EQ%7CINE002A01018"
            try:
                async with self.session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if "data" in data:
                            return web.json_response({"ok": True, "msg": "Upstox Token is VALID! Successfully fetched Reliance quote."})
                    text = await resp.text()
                    return web.json_response({"ok": False, "msg": f"Upstox API rejected the token. Status: {resp.status}. {text[:100]}"})
            except Exception as e:
                return web.json_response({"ok": False, "msg": f"Upstox connection error: {str(e)}"})
                
        elif broker == "fyers":
            token = os.environ.get("FYERS_ACCESS_TOKEN", "")
            app_id = os.environ.get("FYERS_APP_ID", "")
            if not token:
                return web.json_response({"ok": False, "msg": "Fyers token not found in environment."})
                
            headers = {
                "Authorization": f"{app_id}:{token}",
                "Content-Type": "application/json",
                "Accept": "application/json"
            }
            url = "https://api-t1.fyers.in/data/options-chain-v3?symbol=NSE:NIFTY50-INDEX&strikecount=1"
            try:
                async with self.session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    data = await resp.json()
                    if data.get("s") == "ok":
                        return web.json_response({"ok": True, "msg": "Fyers Token is VALID! Successfully fetched Nifty option chain."})
                    return web.json_response({"ok": False, "msg": f"Fyers API rejected the token. {data.get('message', 'Unknown Error')}"})
            except Exception as e:
                return web.json_response({"ok": False, "msg": f"Fyers connection error: {str(e)}"})
                
        return web.json_response({"ok": False, "msg": "Unknown broker"})

    async def handle_admin_fyers_token(self, request: web.Request) -> web.Response:
        """Update Fyers settings (Access Token, App ID, Redirect URI) at runtime."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        new_token = (body.get("token") or "").strip()
        new_app_id = (body.get("app_id") or "").strip()
        new_redirect_uri = (body.get("redirect_uri") or "").strip()

        # Update environment variables so they are picked up instantly
        if new_token:
            os.environ["FYERS_ACCESS_TOKEN"] = new_token
        if new_app_id:
            os.environ["FYERS_APP_ID"] = new_app_id
        if new_redirect_uri:
            os.environ["FYERS_REDIRECT_URI"] = new_redirect_uri

        # Save to config.env for persistence
        config_path = Path(__file__).parent / "config.env"
        try:
            lines = []
            fyers_keys_updated = {"FYERS_ACCESS_TOKEN": False, "FYERS_APP_ID": False, "FYERS_REDIRECT_URI": False}
            if config_path.exists():
                with open(config_path, "r") as f:
                    for line in f:
                        line_stripped = line.strip()
                        if line_stripped.startswith("FYERS_ACCESS_TOKEN") and new_token:
                            lines.append(f"FYERS_ACCESS_TOKEN={new_token}\n")
                            fyers_keys_updated["FYERS_ACCESS_TOKEN"] = True
                        elif line_stripped.startswith("FYERS_APP_ID") and new_app_id:
                            lines.append(f"FYERS_APP_ID={new_app_id}\n")
                            fyers_keys_updated["FYERS_APP_ID"] = True
                        elif line_stripped.startswith("FYERS_REDIRECT_URI") and new_redirect_uri:
                            lines.append(f"FYERS_REDIRECT_URI={new_redirect_uri}\n")
                            fyers_keys_updated["FYERS_REDIRECT_URI"] = True
                        else:
                            lines.append(line)
            else:
                lines = []

            # Append any keys that weren't found on disk
            for key, updated in fyers_keys_updated.items():
                if not updated:
                    val = new_token if key == "FYERS_ACCESS_TOKEN" else (new_app_id if key == "FYERS_APP_ID" else new_redirect_uri)
                    if val:
                        lines.append(f"{key}={val}\n")

            with open(config_path, "w") as f:
                f.writelines(lines)
            log.info("Fyers settings updated in config.env")
        except Exception as exc:
            log.warning("Failed to update config.env for Fyers: %s", exc)

        # Update in settings too for local DB backup
        if new_token:
            self._settings["fyers_access_token"] = new_token
        if new_app_id:
            self._settings["fyers_app_id"] = new_app_id
        if new_redirect_uri:
            self._settings["fyers_redirect_uri"] = new_redirect_uri
        self.store.save_settings(self._settings)

        log.info("Fyers settings hot-swapped dynamically!")
        await self._broadcast_status("Fyers settings updated successfully.", "info")

        fyers_preview = new_token[:10] + "..." + new_token[-6:] if len(new_token) > 20 else "(short)"
        return web.json_response({
            "ok": True,
            "fyers_app_id": new_app_id,
            "fyers_redirect_uri": new_redirect_uri,
            "fyers_token_length": len(new_token),
            "fyers_token_preview": fyers_preview,
        })


    async def handle_admin_settings_get(self, request: web.Request) -> web.Response:
        """Return current settings (sensitive values masked)."""
        settings = dict(self._settings)
        # Mask the token for security
        if settings.get("upstox_token"):
            t = settings["upstox_token"]
            settings["upstox_token"] = t[:10] + "..." + t[-6:] if len(t) > 20 else "(set)"
        # Mask anthropic API key
        if settings.get("anthropic_api_key"):
            k = settings["anthropic_api_key"]
            settings["anthropic_api_key"] = k[:8] + "..." + k[-4:] if len(k) > 16 else "(set)"
        return web.json_response(settings)

    async def handle_admin_settings_update(self, request: web.Request) -> web.Response:
        """Update settings."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        allowed_keys = {"admin_pin", "max_paper_trades_per_day", "default_lots",
                        "auto_exit_enabled", "auto_trail_sl_on_t1", "auto_trade_enabled"}
        updated = []
        for k, v in body.items():
            if k in allowed_keys:
                self._settings[k] = v
                updated.append(k)

        if updated:
            self.store.save_settings(self._settings)
            log.info("Settings updated: %s", ", ".join(updated))

        return web.json_response({"ok": True, "updated": updated})

    async def handle_admin_trades_export(self, request: web.Request) -> web.Response:
        """Export all paper trades as JSON download."""
        trades = [self._paper_trade_to_dict(t) for t in self.paper_trades]
        export = {
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "total": len(trades),
            "trades": trades,
        }
        return web.json_response(export, headers={
            "Content-Disposition": f'attachment; filename="paper_trades_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json"'
        })

    async def handle_admin_trades_clear(self, request: web.Request) -> web.Response:
        """Clear all closed paper trades (keep open ones)."""
        try:
            body = await request.json()
        except Exception:
            body = {}

        clear_all = body.get("clear_all", False)

        if clear_all:
            count = len(self.paper_trades)
            self.paper_trades.clear()
            self._paper_id_counter = 0
        else:
            count = sum(1 for t in self.paper_trades if t["status"] == "CLOSED")
            self.paper_trades = [t for t in self.paper_trades if t["status"] == "OPEN"]

        self._save_paper_trades()
        log.info("Cleared %d paper trades (clear_all=%s)", count, clear_all)

        await self._broadcast_paper_trades()

        return web.json_response({"ok": True, "cleared": count})

    async def handle_admin_logs(self, request: web.Request) -> web.Response:
        """Return last N lines of server.log."""
        log_path = Path(__file__).parent / "server.log"
        lines_count = int(request.query.get("lines", "100"))
        if not log_path.exists():
            return web.json_response({"lines": [], "error": "No log file found"})
        try:
            with open(log_path, "r") as f:
                all_lines = f.readlines()
            tail = all_lines[-lines_count:] if len(all_lines) > lines_count else all_lines
            return web.json_response({"lines": [l.rstrip() for l in tail], "total": len(all_lines)})
        except Exception as exc:
            return web.json_response({"lines": [], "error": str(exc)})

    def get_research_fallback_candles(self, symbol: str) -> List[Dict[str, Any]]:
        """Retrieve real recorded intraday price/OI/Volume data from chain_snapshot or
        fall back to stock_daily_ohlc to generate high-fidelity synthetic 1m candles.
        """
        candles = []
        if not DATA_RECORDER_AVAILABLE:
            return self._generate_pure_synthetic_candles(symbol)

        try:
            # 1. Try to find the latest trading date for this symbol in chain_snapshot
            with data_recorder._connect() as conn:
                row = conn.execute(
                    "SELECT MAX(trading_date) FROM chain_snapshot WHERE symbol = ?",
                    (symbol,)
                ).fetchone()
                
                latest_date = row[0] if row else None
                if not latest_date:
                    row = conn.execute(
                        "SELECT MAX(trading_date) FROM chain_snapshot"
                    ).fetchone()
                    latest_date = row[0] if row else None
                
                if latest_date:
                    rows = conn.execute(
                        """
                        SELECT snap_ts, spot_ltp, total_oi, spot_volume 
                        FROM chain_snapshot 
                        WHERE symbol = ? AND trading_date = ? 
                        ORDER BY snap_ts ASC
                        """,
                        (symbol, latest_date)
                    ).fetchall()
                    
                    if rows:
                        candles = self._upsample_snapshots_to_1m(rows, symbol)
                        if candles:
                            log.info(
                                "Generated %d high-fidelity resampled research candles for %s from snapshot date %s",
                                len(candles), symbol, latest_date
                            )
                            return candles
        except Exception as exc:
            log.warning("Failed to query chain_snapshot for fallback candles: %s", exc)

        # 2. Fall back to stock_daily_ohlc if chain_snapshot query failed or returned no data
        try:
            with data_recorder._connect() as conn:
                row = conn.execute(
                    """
                    SELECT open, high, low, close, volume, oi, trading_date 
                    FROM stock_daily_ohlc 
                    WHERE symbol = ? 
                    ORDER BY trading_date DESC LIMIT 1
                    """,
                    (symbol,)
                ).fetchone()
                
                if row:
                    candles = self._generate_candles_from_daily_ohlc(dict(row), symbol)
                    if candles:
                        log.info(
                            "Generated %d synthetic candles for %s using EOD OHLC from %s",
                            len(candles), symbol, row['trading_date']
                        )
                        return candles
        except Exception as exc:
            log.warning("Failed to query stock_daily_ohlc for fallback candles: %s", exc)

        # 3. If everything else fails, generate pure synthetic candles
        return self._generate_pure_synthetic_candles(symbol)

    def _upsample_snapshots_to_1m(self, rows, symbol: str) -> List[Dict[str, Any]]:
        import random
        from datetime import datetime

        points = []
        for r in rows:
            try:
                dt = datetime.fromisoformat(r['snap_ts'])
                ts = int(dt.timestamp())
                points.append((ts, r['spot_ltp'], r['total_oi'], r['spot_volume'] or 0))
            except Exception:
                continue

        if len(points) < 2:
            return []

        points.sort(key=lambda x: x[0])
        start_ts = points[0][0]
        end_ts = points[-1][0]

        step = 60
        candles = []
        
        prev_close = points[0][1]
        prev_cum_vol = points[0][3]

        rnd = random.Random(hash(symbol) & 0xffffffff)

        t = start_ts
        while t <= end_ts:
            left = points[0]
            right = points[-1]
            for p in points:
                if p[0] <= t:
                    left = p
                if p[0] > t:
                    right = p
                    break
            
            if right[0] > left[0]:
                w = (t - left[0]) / (right[0] - left[0])
                price_interp = left[1] + w * (right[1] - left[1])
                oi_interp = left[2] + w * (right[2] - left[2])
                cum_vol_interp = left[3] + w * (right[3] - left[3])
            else:
                price_interp = left[1]
                oi_interp = left[2]
                cum_vol_interp = left[3]

            noise_pct = rnd.uniform(0.0002, 0.0008)
            close_val = price_interp * (1.0 + rnd.uniform(-noise_pct, noise_pct))
            open_val = prev_close
            
            high_val = max(open_val, close_val) * (1.0 + rnd.uniform(0, noise_pct * 0.8))
            low_val = min(open_val, close_val) * (1.0 - rnd.uniform(0, noise_pct * 0.8))

            vol_inc = cum_vol_interp - prev_cum_vol
            if vol_inc <= 0:
                vol_inc = rnd.randint(1000, 5000)

            candles.append({
                "time": t,
                "open": round(open_val, 2),
                "high": round(high_val, 2),
                "low": round(low_val, 2),
                "close": round(close_val, 2),
                "volume": int(vol_inc),
                "oi": int(oi_interp)
            })

            prev_close = close_val
            prev_cum_vol = cum_vol_interp
            t += step

        return candles

    def _generate_candles_from_daily_ohlc(self, daily: Dict[str, Any], symbol: str) -> List[Dict[str, Any]]:
        import random
        from datetime import datetime, time as dt_time

        try:
            date_str = daily.get('trading_date') or datetime.now().date().isoformat()
            base_dt = datetime.combine(datetime.fromisoformat(date_str).date(), dt_time(9, 15))
            start_ts = int(base_dt.timestamp())
        except Exception:
            start_ts = int(datetime.now().replace(hour=9, minute=15, second=0, microsecond=0).timestamp())

        count = 375
        step = 60

        d_open = daily.get('open') or 100.0
        d_high = daily.get('high') or (d_open * 1.02)
        d_low = daily.get('low') or (d_open * 0.98)
        d_close = daily.get('close') or d_open
        d_volume = daily.get('volume') or 1000000
        d_oi = daily.get('oi') or 5000000

        rnd = random.Random(hash(symbol) & 0xffffffff)
        
        path = [0.0] * count
        current = 0.0
        for i in range(1, count - 1):
            step_val = rnd.normalvariate(0, 1)
            current += step_val
            path[i] = current
        
        end_val = current
        for i in range(count):
            path[i] = path[i] - (i / (count - 1)) * end_val

        min_path = min(path)
        max_path = max(path)
        path_range = (max_path - min_path) if (max_path - min_path) > 0 else 1.0

        prices = []
        for i in range(count):
            trend = d_open + (i / (count - 1)) * (d_close - d_open)
            fluc = (path[i] / path_range) * (d_high - d_low) * 0.4
            prices.append(trend + fluc)

        scaled_prices = []
        for i, p in enumerate(prices):
            if i == 0:
                scaled_prices.append(d_open)
            elif i == count - 1:
                scaled_prices.append(d_close)
            else:
                clamped = max(d_low * 1.001, min(d_high * 0.999, p))
                scaled_prices.append(clamped)

        candles = []
        prev_close = d_open
        t = start_ts

        vol_weights = []
        for i in range(count):
            x = (i - count/2) / (count/2)
            weight = 0.1 + (x ** 4) * 0.9
            vol_weights.append(weight)
        
        total_weight = sum(vol_weights)
        vol_per_minute = [int((w / total_weight) * d_volume) for w in vol_weights]

        for i in range(count):
            close_val = scaled_prices[i]
            open_val = prev_close if i > 0 else d_open
            
            micro_noise = (d_high - d_low) * 0.02
            high_val = max(open_val, close_val) + rnd.uniform(0, micro_noise)
            low_val = min(open_val, close_val) - rnd.uniform(0, micro_noise)

            high_val = min(d_high, high_val)
            low_val = max(d_low, low_val)

            high_val = max(high_val, open_val, close_val)
            low_val = min(low_val, open_val, close_val)

            oi_val = int(d_oi * (0.95 + 0.05 * (i / (count - 1)) + rnd.uniform(-0.002, 0.002)))

            candles.append({
                "time": t,
                "open": round(open_val, 2),
                "high": round(high_val, 2),
                "low": round(low_val, 2),
                "close": round(close_val, 2),
                "volume": max(100, vol_per_minute[i]),
                "oi": oi_val
            })

            prev_close = close_val
            t += step

        max_idx = rnd.randint(10, count - 10)
        candles[max_idx]["high"] = d_high
        if candles[max_idx]["close"] > d_high:
            candles[max_idx]["close"] = d_high

        min_idx = rnd.randint(10, count - 10)
        while min_idx == max_idx:
            min_idx = rnd.randint(10, count - 10)
        candles[min_idx]["low"] = d_low
        if candles[min_idx]["close"] < d_low:
            candles[min_idx]["close"] = d_low

        return candles

    def _generate_pure_synthetic_candles(self, symbol: str) -> List[Dict[str, Any]]:
        base_price = 1500.0
        sym = symbol.upper().strip()
        if sym in ["NIFTY", "NIFTY50", "NIFTY 50"]:
            base_price = self._index_state.get("NIFTY50", {}).get("ltp") or 23719.30
        elif sym in ["BANKNIFTY", "NIFTY BANK", "NIFTYBANK"]:
            base_price = self._index_state.get("BANKNIFTY", {}).get("ltp") or 45000.0
        else:
            base_price = self.state.get(symbol, {}).get("ltp") or 1500.0
            if base_price <= 0:
                stock_info = self.stock_map.get(symbol)
                if stock_info:
                    base_price = stock_info.prev_close or 1500.0

        daily = {
            "open": base_price,
            "high": base_price * 1.02,
            "low": base_price * 0.99,
            "close": base_price * 1.01,
            "volume": 2000000,
            "oi": 8000000,
            "trading_date": datetime.now().date().isoformat()
        }
        return self._generate_candles_from_daily_ohlc(daily, symbol)

    async def handle_api_candles(self, request: web.Request) -> web.Response:
        """Return intraday candle data for a symbol in TradingView Lightweight Charts format.

        GET /api/candles?symbol=RELIANCE&interval=5minute
        Valid intervals: 1minute, 3minute, 5minute, 15minute, 30minute
        """
        VALID_INTERVALS = {"1minute", "3minute", "5minute", "15minute", "30minute"}

        symbol = request.query.get("symbol", "").upper().strip()
        interval = request.query.get("interval", "5minute").strip()
        force_mock = request.query.get("mock", "").lower() == "true"

        if not symbol:
            return web.json_response({"error": "symbol query param is required"}, status=400)

        if interval not in VALID_INTERVALS:
            return web.json_response(
                {"error": f"Invalid interval '{interval}'. Valid: {sorted(VALID_INTERVALS)}"},
                status=400,
            )

        instrument_key = None
        if symbol in ["NIFTY", "NIFTY50", "NIFTY 50", "NSE_INDEX|NIFTY 50", "NSE_INDEX|Nifty 50"]:
            instrument_key = "NSE_INDEX|Nifty 50"
        elif symbol in ["BANKNIFTY", "NIFTY BANK", "NIFTYBANK", "NSE_INDEX|NIFTY BANK", "NSE_INDEX|Nifty Bank"]:
            instrument_key = "NSE_INDEX|Nifty Bank"
        else:
            stock_info = self.stock_map.get(symbol)
            if not stock_info:
                return web.json_response({"error": f"Symbol '{symbol}' not found"}, status=404)
            instrument_key = stock_info.fut_ikey or stock_info.ikey

        raw_candles = []
        upstream_ok = False

        if not force_mock and instrument_key:
            encoded_key = urllib.parse.quote(instrument_key, safe="")
            url = UPSTOX_CANDLES_URL.format(instrument_key=encoded_key, interval="1minute")

            headers = {
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
            }

            try:
                async with self.session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        raw_candles = data.get("data", {}).get("candles", [])
                        upstream_ok = True
                    else:
                        body = await resp.text()
                        log.warning("Upstox candles API error %s for %s: %s", resp.status, symbol, body[:200])
            except Exception as exc:
                log.warning("Error fetching candles from Upstox for %s: %s", symbol, exc)

        candles = []
        if upstream_ok and raw_candles:
            for c in raw_candles:
                if len(c) < 6:
                    continue
                try:
                    ts_str, open_, high, low, close, volume = c[0], c[1], c[2], c[3], c[4], c[5]
                    oi = c[6] if len(c) > 6 else 0
                    dt = datetime.fromisoformat(ts_str)
                    unix_ts = int(dt.timestamp())
                    candles.append({
                        "time": unix_ts,
                        "open": open_,
                        "high": high,
                        "low": low,
                        "close": close,
                        "volume": volume,
                        "oi": oi,
                    })
                except Exception as exc:
                    log.warning("Skipping malformed candle %s: %s", c, exc)
                    continue
            candles.sort(key=lambda x: x["time"])
        else:
            log.info("Using high-fidelity research fallback candles for %s", symbol)
            candles = self.get_research_fallback_candles(symbol)

        # Perform high-fidelity dynamic resampling if the requested interval is not 1minute
        if interval != "1minute" and candles:
            minutes = 5
            if interval == "3minute":
                minutes = 3
            elif interval == "15minute":
                minutes = 15
            elif interval == "30minute":
                minutes = 30
            
            resampled = []
            chunk = []
            for c in candles:
                if not chunk:
                    chunk = [c]
                elif (c["time"] // (minutes * 60)) * (minutes * 60) == (chunk[0]["time"] // (minutes * 60)) * (minutes * 60):
                    chunk.append(c)
                else:
                    resampled.append({
                        "time": (chunk[0]["time"] // (minutes * 60)) * (minutes * 60),
                        "open": chunk[0]["open"],
                        "high": max(x["high"] for x in chunk),
                        "low": min(x["low"] for x in chunk),
                        "close": chunk[-1]["close"],
                        "volume": sum(x["volume"] for x in chunk),
                        "oi": chunk[-1]["oi"]
                    })
                    chunk = [c]
            if chunk:
                resampled.append({
                    "time": (chunk[0]["time"] // (minutes * 60)) * (minutes * 60),
                    "open": chunk[0]["open"],
                    "high": max(x["high"] for x in chunk),
                    "low": min(x["low"] for x in chunk),
                    "close": chunk[-1]["close"],
                    "volume": sum(x["volume"] for x in chunk),
                    "oi": chunk[-1]["oi"]
                })
            candles = resampled

        return web.json_response({"candles": candles, "symbol": symbol, "interval": interval})

    async def handle_api_stock_oi_timeseries(self, request: web.Request) -> web.Response:
        """GET /api/stock/oi_timeseries?symbol=RELIANCE"""
        symbol = request.query.get("symbol", "").upper().strip()
        if not symbol:
            return web.json_response({"error": "symbol query param is required"}, status=400)
            
        import data_recorder
        rows = data_recorder.get_oi_timeseries(symbol)
        
        import datetime
        
        result = []
        for r in rows:
            try:
                dt = datetime.datetime.fromisoformat(r["snap_ts"])
                unix_ts = int(dt.timestamp())
            except Exception:
                continue
            
            result.append({
                "time": unix_ts,
                "pe_oi_chg": r.get("pe_oi_chg", 0) or 0,
                "ce_oi_chg": r.get("ce_oi_chg", 0) or 0,
            })
            
        return web.json_response({"data": result, "symbol": symbol})

    async def handle_api_advanced_chain(self, request):
        """GET /api/advanced-chain?symbol=XYZ
        Returns a sorted list of up to 15 strikes (ATM, ATM-7 to ATM+7)
        with their CE/PE OI, LTP, IV, Volume, derived directly from the cached strike_map.
        """
        from aiohttp import web
        symbol = request.query.get("symbol", "").upper().strip()
        if not symbol:
            return web.json_response({"error": "symbol query param is required"}, status=400)
            
        st = self.state.get(symbol)
        if not st or "strike_map" not in st or not st.get("atm_strike"):
            return web.json_response({"error": "Chain data not available yet"}, status=404)
            
        atm_strike = float(st["atm_strike"])
        strike_map = st["strike_map"]
        
        all_strikes = sorted([float(k) for k in strike_map.keys()])
        if not all_strikes:
            return web.json_response({"error": "No strikes in strike map"}, status=404)
            
        atm_idx = min(range(len(all_strikes)), key=lambda i: abs(all_strikes[i] - atm_strike))
        
        start_idx = max(0, atm_idx - 7)
        end_idx = min(len(all_strikes), atm_idx + 8)
        selected_strikes = all_strikes[start_idx:end_idx]
        
        result = []
        for s in selected_strikes:
            data = strike_map[s]
            result.append({
                "strike": s,
                "is_atm": (s == all_strikes[atm_idx]),
                "ce_dnf_lakhs": data.get("ce_dnf_lakhs", 0),
                "pe_dnf_lakhs": data.get("pe_dnf_lakhs", 0),
                "ce_gex_lakhs": data.get("ce_gex_lakhs", 0),
                "pe_gex_lakhs": data.get("pe_gex_lakhs", 0),
                "ce_delta": data.get("ce_delta", 0),
                "ce_gamma": data.get("ce_gamma", 0),
                "ce_iv": data.get("ce_iv", 0),
                "ce_oi": data.get("ce_oi", 0),
                "pe_oi": data.get("pe_oi", 0),
                "pe_iv": data.get("pe_iv", 0),
                "pe_gamma": data.get("pe_gamma", 0),
                "pe_delta": data.get("pe_delta", 0),
            })
            
        payload = {
            "spot_ltp": st.get("ltp", 0),
            "gex_total_lakhs": st.get("gex_total_lakhs", 0),
            "dnf_net_lakhs": st.get("dnf_net_lakhs", 0),
            "skew_25d": st.get("skew_25d", 0),
            "skew_25d_pct": st.get("skew_25d_pct", 0),
            "volume_profile": st.get("volume_profile", {}),
            "zero_gamma": st.get("zero_gamma"),
            "strikes": result
        }
            
        return web.json_response(payload)

    async def handle_api_chain(self, request: web.Request) -> web.Response:
        """GET /api/chain?symbol=XYZ
        Returns a sorted list of exactly 5 strikes (ATM, ATM-1, ATM-2, ATM+1, ATM+2)
        with their CE/PE OI and LTP, derived directly from the cached strike_map.
        """
        symbol = request.query.get("symbol", "").upper().strip()
        if not symbol:
            return web.json_response({"error": "symbol query param is required"}, status=400)
            
        st = self.state.get(symbol)
        if not st or "strike_map" not in st or not st.get("atm_strike"):
            return web.json_response({"error": "Chain data not available yet"}, status=404)
            
        atm_strike = float(st["atm_strike"])
        strike_map = st["strike_map"]
        
        # Sort all available strikes
        all_strikes = sorted([float(k) for k in strike_map.keys()])
        if not all_strikes:
            return web.json_response({"error": "No strikes in strike map"}, status=404)
            
        # Find ATM index
        try:
            atm_idx = all_strikes.index(atm_strike)
        except ValueError:
            # Fallback if exact ATM strike isn't found exactly (shouldn't happen)
            atm_idx = min(range(len(all_strikes)), key=lambda i: abs(all_strikes[i]-atm_strike))
            atm_strike = all_strikes[atm_idx]
            
        # Get +/- 2 bounds
        start_idx = max(0, atm_idx - 2)
        end_idx = min(len(all_strikes) - 1, atm_idx + 2)
        
        chain_slice = []
        for i in range(start_idx, end_idx + 1):
            s = all_strikes[i]
            data = strike_map[s]
            chain_slice.append({
                "strike": s,
                "is_atm": (i == atm_idx),
                "ce_oi": data.get("ce_oi", 0),
                "pe_oi": data.get("pe_oi", 0),
                "ce_ltp": data.get("ce_ltp", 0),
                "pe_ltp": data.get("pe_ltp", 0),
            })
            
        return web.json_response({
            "symbol": symbol,
            "atm_strike": atm_strike,
            "chain": chain_slice
        })

    # ------------------------------------------------------------------
    # Chat API — Anthropic Claude integration
    # ------------------------------------------------------------------

    def _build_chat_context(self) -> str:
        """Build a market context string from current dashboard state."""
        lines = []

        # Trade-ready stocks (use actual state field names: pcr_sig, buildup, prem_ok)
        poised = []
        for sym, d in self.state.items():
            if (d.get("score", 0) >= 50
                    and d.get("prem_ok")
                    and d.get("pcr_sig") in ("BULLISH", "MILDLY_BULLISH", "BEARISH", "MILDLY_BEARISH")
                    and d.get("buildup") in ("LONG_BUILD", "SHORT_BUILD", "LONG_UNWIND", "SHORT_COVER")):
                poised.append(d)
        poised.sort(key=lambda x: x.get("score", 0), reverse=True)

        lines.append(f"=== LIVE DASHBOARD STATE ({len(self.state)} stocks monitored) ===")
        lines.append(f"Nearest expiry: {self.nearest_expiry}")

        # Market overview — top movers
        movers = sorted(
            [d for d in self.state.values() if d.get("ltp", 0) > 0],
            key=lambda x: abs(x.get("chg_pct", 0)),
            reverse=True,
        )[:10]
        if movers:
            lines.append("\nTop 10 movers:")
            for d in movers:
                lines.append(f"  {d['symbol']}: LTP {d.get('ltp',0):.2f} ({d.get('chg_pct',0):+.2f}%)"
                             f"  Signal={d.get('pcr_sig','?')} Buildup={d.get('buildup','?')}"
                             f"  Score={d.get('score',0)}")

        # Trade-ready stocks
        if poised:
            lines.append(f"\n{len(poised)} TRADE READY stocks:")
            for d in poised[:10]:
                sig = d.get("pcr_sig", "?")
                direction = "CE" if sig in ("BULLISH", "MILDLY_BULLISH") else "PE"
                lines.append(f"  {d['symbol']}: Score={d.get('score',0)} Dir={direction}"
                             f"  LTP={d.get('ltp',0):.2f} ({d.get('chg_pct',0):+.2f}%)"
                             f"  ATM_IV={d.get('atm_iv','?')} PCR={d.get('pcr','?')}"
                             f"  MaxPain={d.get('max_pain','?')}"
                             f"  Lot={d.get('lot','?')}")
        else:
            lines.append("\nNo Trade Ready stocks right now.")

        # Paper trades summary
        open_trades = [t for t in self.paper_trades if t.get("status") == "OPEN"]
        closed_trades = [t for t in self.paper_trades if t.get("status") == "CLOSED"]
        if open_trades or closed_trades:
            lines.append(f"\nPaper trades: {len(open_trades)} open, {len(closed_trades)} closed")
            for t in open_trades:
                tid = t.get("id", "?")
                sym = t.get("symbol", "?")
                side = t.get("side", "?")
                entry = t.get("entry_premium", 0)
                cur = t.get("current_premium", entry)
                pnl = t.get("pnl_total", 0)
                lines.append(f"  [{tid}] {sym} {side}: Entry={entry:.1f} Cur={cur:.1f} PnL={pnl:+.0f}")

        return "\n".join(lines)

    def _build_stock_context(self, symbol: str) -> str:
        """Build detailed context for a specific focused stock."""
        d = self.state.get(symbol)
        if not d:
            return ""
        lines = [f"\n=== FOCUSED STOCK: {symbol} (user is viewing this) ==="]
        lines.append(f"Sector: {d.get('sector','?')} | NIFTY50: {'Yes' if d.get('is_n50') else 'No'}")
        lines.append(f"LTP: {d.get('ltp',0):.2f} | Change: {d.get('chg',0):+.2f}%")
        lines.append(f"Prev Close: {d.get('prev_close',0):.2f}")
        lines.append(f"Volume: {d.get('vol',0):,.0f} | Vol Surge: {d.get('vol_surge','?')}x")
        lines.append(f"Score: {d.get('score',0)} | PCR Signal: {d.get('pcr_sig','?')} | Buildup: {d.get('buildup','?')}")
        lines.append(f"PCR: {d.get('pcr','?')}")
        lines.append(f"ATM IV: {d.get('atm_iv','?')}")
        lines.append(f"ATM CE: {d.get('atm_ce','?')} | ATM PE: {d.get('atm_pe','?')}")
        lines.append(f"Max Pain: {d.get('max_pain','?')} | Max Pain Dist: {d.get('mp_dist','?')}%")
        lines.append(f"Net OI: {d.get('net_oi','?')} | CE OI Chg: {d.get('ce_oi_chg','?')} | PE OI Chg: {d.get('pe_oi_chg','?')}")
        lines.append(f"Lot Size: {d.get('lot','?')} | Expiry: {d.get('expiry','?')}")
        lines.append(f"Premium OK: {d.get('prem_ok','?')}")
        # Check if trade-ready
        is_ready = (d.get("score", 0) >= 50
                    and d.get("prem_ok")
                    and d.get("pcr_sig") in ("BULLISH", "MILDLY_BULLISH", "BEARISH", "MILDLY_BEARISH")
                    and d.get("buildup") in ("LONG_BUILD", "SHORT_BUILD", "LONG_UNWIND", "SHORT_COVER"))
        lines.append(f"Trade Ready: {'YES' if is_ready else 'NO'}")
        return "\n".join(lines)

    async def handle_api_chat(self, request: web.Request) -> web.Response:
        """
        POST /api/chat
        Body: { "message": "...", "history": [{"role":"user"|"assistant","content":"..."}] }
        Returns: { "reply": "..." }
        Uses Anthropic Claude Messages API.
        """
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)

        user_msg = (body.get("message") or "").strip()
        if not user_msg:
            return web.json_response({"error": "Empty message"}, status=400)

        history = body.get("history", [])
        focused_stock = (body.get("focused_stock") or "").upper().strip()

        # Get API key from settings, env, or config.env
        api_key = (
            self._settings.get("anthropic_api_key", "")
            or os.environ.get("ANTHROPIC_API_KEY", "")
        )

        if not api_key:
            return web.json_response({
                "reply": "⚙️ Chat not configured yet.\n\n"
                         "To enable the AI agent, add your Anthropic API key:\n"
                         "1. Go to Admin panel → Settings\n"
                         "2. Set ANTHROPIC_API_KEY\n"
                         "   or add it to config.env\n\n"
                         "Get a key at: https://console.anthropic.com/settings/keys"
            })

        # Build system prompt with live context
        context = self._build_chat_context()
        if focused_stock:
            context += self._build_stock_context(focused_stock)
        system_prompt = (
            "You are Quanta, an F&O stock options trading assistant embedded in a live NSE dashboard. "
            "You speak like a trading-desk colleague — direct, numbers-first, no fluff. "
            "Use Indian F&O terminology (CE/PE, ATM, lot size, expiry, OI, PCR, IV). "
            "You have access to the current live dashboard state below.\n\n"
            "When recommending trades:\n"
            "- Only recommend Trade Ready stocks (score >= 50 with all 5 criteria met)\n"
            "- Always mention: direction (CE/PE), entry premium, SL (15% premium + 1% spot), targets\n"
            "- Cite specific numbers from the data\n"
            "- NIFTY lot size = 65\n\n"
            "Keep responses concise (2-3 short paragraphs max). "
            "Use plain text, no markdown headers. Use ₹ for rupees.\n\n"
            f"{context}"
        )

        # Build messages for Anthropic API
        messages = []
        for h in history[-10:]:  # Keep last 10 turns
            role = h.get("role", "user")
            content = h.get("content", "")
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": user_msg})

        # Call Anthropic Messages API
        try:
            if not self.session:
                return web.json_response({"error": "Server session not ready"}, status=503)

            async with self.session.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 1024,
                    "system": system_prompt,
                    "messages": messages,
                },
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 401:
                    return web.json_response({
                        "reply": "❌ Anthropic API key is invalid or expired. "
                                 "Update it in Admin → Settings."
                    })
                if resp.status == 429:
                    return web.json_response({
                        "reply": "⏳ Rate limited. Try again in a few seconds."
                    })
                if resp.status != 200:
                    text = await resp.text()
                    log.warning("Anthropic API error %d: %s", resp.status, text[:200])
                    return web.json_response({
                        "reply": f"❌ API error ({resp.status}). Try again."
                    })

                data = await resp.json()
                # Extract text from response
                content_blocks = data.get("content", [])
                reply_text = ""
                for block in content_blocks:
                    if block.get("type") == "text":
                        reply_text += block.get("text", "")

                if not reply_text:
                    reply_text = "No response generated. Try rephrasing your question."

                return web.json_response({"reply": reply_text})

        except asyncio.TimeoutError:
            return web.json_response({"reply": "⏳ Request timed out. Try a shorter question."})
        except Exception as exc:
            log.error("Chat API error: %s", exc, exc_info=True)
            return web.json_response({"reply": f"❌ Error: {str(exc)[:100]}"})

    async def handle_admin_settings_update_chat_key(self, request: web.Request) -> web.Response:
        """POST /api/admin/chat-key — Save Anthropic API key separately."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        key = (body.get("key") or "").strip()
        if not key:
            return web.json_response({"error": "Empty key"}, status=400)
        self._settings["anthropic_api_key"] = key
        self.store.save_settings(self._settings)
        log.info("Anthropic API key updated (%d chars)", len(key))
        return web.json_response({"ok": True})

    async def _broadcast_paper_trades(self):
        """Broadcast paper trade updates to all connected clients, filtered by user."""
        if not self.ws_clients:
            return

        all_open_trades_formatted = [self._paper_trade_to_dict(t) for t in self.paper_trades]
        
        dead = []
        for ws in list(self.ws_clients):
            user_id = ws.get("user_id")
            if not user_id:
                continue

            # Open trades for this specific user
            user_open_trades = [t for t in all_open_trades_formatted if t.get("user_id") == user_id]

            # Fetch closed trades for this specific user from DB
            db_closed = self._db.get_paper_trades(user_id=user_id, status="EXITED", limit=100)
            db_closed_alt = self._db.get_paper_trades(user_id=user_id, status="CLOSED", limit=100)
            seen_ids = set()
            user_closed_trades = []
            for r in db_closed + db_closed_alt:
                if r["id"] not in seen_ids:
                    seen_ids.add(r["id"])
                    user_closed_trades.append(self._db_row_to_memory_trade(r))

            user_trades = user_open_trades + user_closed_trades

            # Compute stats
            total_open_pnl = sum(t.get("pnl_total", 0) for t in user_open_trades)
            total_closed_pnl = sum(t.get("final_pnl", 0) for t in user_closed_trades)
            wins = sum(1 for t in user_closed_trades if (t.get("final_pnl", 0) or 0) > 0)
            losses = sum(1 for t in user_closed_trades if (t.get("final_pnl", 0) or 0) <= 0)

            payload = {
                "type": "paper",
                "trades": user_trades,
                "summary": {
                    "open_count": len(user_open_trades),
                    "closed_count": len(user_closed_trades),
                    "total_open_pnl": round(total_open_pnl, 2),
                    "total_closed_pnl": round(total_closed_pnl, 2),
                    "total_pnl": round(total_open_pnl + total_closed_pnl, 2),
                    "wins": wins,
                    "losses": losses,
                    "win_rate": round(wins / (wins + losses) * 100, 1) if (wins + losses) > 0 else 0,
                },
                "ts": time.time(),
            }

            try:
                await ws.send_str(json.dumps(payload, default=str))
            except (ConnectionResetError, ConnectionError, RuntimeError):
                dead.append(ws)
            except Exception:
                dead.append(ws)

        for ws in dead:
            self.ws_clients.discard(ws)

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------

    async def _startup_avg5d_vol(self):
        """Wait for token and session to be ready, then populate avg5d_vol once."""
        await asyncio.sleep(10)  # Let LTP populate first
        try:
            await self.populate_avg5d_vol()
        except Exception as exc:
            log.warning("avg5d_vol startup task failed: %s", exc)

    def _create_safe_task(self, coro_func, *args, **kwargs):
        """Wraps a background task in a resilient loop so it restarts on failure."""
        async def safe_wrapper():
            while self._running:
                try:
                    await coro_func(*args, **kwargs)
                    break
                except Exception as e:
                    log.error("Task %s crashed: %s", coro_func.__name__, e, exc_info=True)
                    await asyncio.sleep(5)
        return asyncio.create_task(safe_wrapper())

    async def on_startup(self, app: web.Application):
        """Called when the aiohttp app starts."""
        # Initialize SQLite DB & load active paper trades
        self._db.init()
        db_trades = self._db.get_all_open_paper_trades()
        self.paper_trades = [self._db_row_to_memory_trade(r) for r in db_trades]
        log.info("Loaded %d active paper trades from SQLite DB", len(self.paper_trades))

        self.session = aiohttp.ClientSession()
        self._running = True

        # ── Upstox WebSocket v3 Streamer (primary tick source) ──
        # Replaces REST poll_ltp() with real-time streaming.
        # poll_ltp is kept as fallback — it auto-sleeps when WS is connected.
        instrument_keys = [s.ikey for s in self.stocks]

        # Add index instruments for the top ticker bar
        INDEX_KEYS = {
            "NSE_INDEX|Nifty 50":          "NIFTY50",
            "NSE_INDEX|Nifty Bank":        "BANKNIFTY",
            "NSE_INDEX|NIFTY MID SELECT":  "MIDCAPNIFTY",
            "NSE_INDEX|India VIX":         "INDIAVIX",
        }
        # Also subscribe to Nifty 50 futures (near-month) for basis calculation.
        # The ikey for the current-month Nifty future is resolved at runtime;
        # we'll use the REST quote endpoint for basis since futures ikeys rotate.
        self._index_keys = INDEX_KEYS
        self._index_state: Dict[str, Dict[str, Any]] = {
            sym: {"ltp": None, "chg_pct": None, "prev_close": None, "prev_close_date": ""}
            for sym in INDEX_KEYS.values()
        }
        # Extend the WS subscription with index keys
        all_keys = instrument_keys + list(INDEX_KEYS.keys())
        # Extend ikey_to_symbol map for index ticks
        for ikey, sym in INDEX_KEYS.items():
            self.ikey_to_symbol[ikey] = sym

        self._upstox_streamer = UpstoxStreamer(
            token=self.token,
            instrument_keys=all_keys,
            ikey_to_symbol=self.ikey_to_symbol,
            on_tick=self._handle_ws_tick,
            on_status=self._handle_ws_status,
            session=self.session,
            mode=os.environ.get("WS_FEED_MODE", "ltpc"),
        )
        self._tasks.append(asyncio.create_task(self._upstox_streamer.run()))
        log.info("Upstox WS v3 streamer launched for %d instruments (incl. %d indices)",
                 len(all_keys), len(INDEX_KEYS))

        # ── Tier 3: optional option-strike streamer (live OI per CE/PE) ──
        # Enabled via WS_OPTION_OI=1. Subscribes to ATM±N strikes per stock in
        # full_d5 mode for tick-level OI changes. Defaults OFF — needs a
        # market-open window to bootstrap strike resolution and baseline OI.
        if os.environ.get("WS_OPTION_OI") == "1":
            self._tasks.append(asyncio.create_task(self._option_oi_bootstrap()))
            log.info("Option-OI WS streamer scheduled (will start once strikes resolve)")

        # ── Fallback REST polling (auto-sleeps when WS is connected) ──
        self._tasks.append(self._create_safe_task(self.poll_ltp))

        # ── Other background tasks (unchanged) ──
        self._tasks.append(self._create_safe_task(self.poll_ohlc))
        self._tasks.append(self._create_safe_task(self.poll_chains))
        self._tasks.append(self._create_safe_task(self._fyers_historical_sync_loop))

        # ── Fast OI poll disabled to prevent 429 Rate Limits ──
        # self._tasks.append(self._create_safe_task(self.poll_oi_fast))

        # One-time: populate avg 5-day volume from historical daily candles
        self._tasks.append(asyncio.create_task(self._startup_avg5d_vol()))

        # ── Historical data recorder ──
        if DATA_RECORDER_AVAILABLE:
            try:
                data_recorder.init_db()
                log.info("data_recorder: SQLite DB ready at data/quantra_history.db")
                # Auto-seed Nifty time-series if empty
                seeded = data_recorder.seed_nifty_timeseries_if_empty()
                if seeded > 0:
                    log.info("data_recorder: Auto-seeded %d mock Nifty ticks", seeded)
                
                # Auto-seed Nifty option chain snapshots if empty
                seeded_chain = data_recorder.seed_nifty_chain_if_empty()
                if seeded_chain > 0:
                    log.info("data_recorder: Auto-seeded %d mock Nifty chain snapshots", seeded_chain)
            except Exception as exc:
                log.warning("data_recorder init failed (will skip recording): %s", exc)

        # ── Auto Paper Trader (scans every 5min during trading hours) ──
        self._auto_trader = AutoPaperTrader(self)
        self._tasks.append(asyncio.create_task(self._auto_trader.run()))
        log.info("Auto paper trader launched (max %d trades/day)", self._auto_trader.MAX_TRADES_PER_DAY)

        # ── Weekend/Non-trading Simulation Mode ──
        # Simulator removed per user request. We will show real last traded data.
        log.info("Weekend simulator is DEACTIVATED by user preference.")
        
        # Pre-fill state with last trading day's quotes (fixes empty dashboard on weekends)
        self._tasks.append(asyncio.create_task(self._bootstrap_market_data()))

        log.info("Background tasks started (WS_STREAM=primary, LTP_REST=fallback, OHLC=30s, Chains=15m, AutoTrade=5m)")

    async def on_shutdown(self, app: web.Application):
        """Called when the aiohttp app shuts down."""
        log.info("Shutting down...")
        self._running = False

        # Stop Upstox WS streamer gracefully
        if self._upstox_streamer:
            await self._upstox_streamer.stop()
            log.info("Upstox WS streamer stopped")

        # Stop auto paper trader
        if self._auto_trader:
            await self._auto_trader.stop()
            log.info("Auto paper trader stopped")

        # Cancel background tasks
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        # Close all WebSocket connections
        for ws in list(self.ws_clients):
            try:
                await ws.close(code=aiohttp.WSCloseCode.GOING_AWAY,
                               message=b"Server shutting down")
            except Exception:
                pass
        self.ws_clients.clear()

        # Close HTTP session
        if self.session:
            await self.session.close()
            self.session = None

        log.info("Shutdown complete.")

    def create_app(self) -> web.Application:
        """Create and configure the aiohttp Application."""
        app = web.Application()


        # Routes


        app.router.add_get("/", self.handle_index)
        app.router.add_get("/ws", self.handle_ws)
        app.router.add_get("/api/state", self.handle_api_state)
        app.router.add_get("/api/debug", self.handle_debug)
        app.router.add_get("/health", self.handle_health)

        # Paper trading routes
        app.router.add_get("/paper", self.handle_paper_page)
        app.router.add_post("/api/paper/enter", self.handle_paper_enter)
        app.router.add_post("/api/paper/exit", self.handle_paper_exit)
        app.router.add_get("/api/paper/trades", self.handle_paper_trades)
        app.router.add_post("/api/paper/sync", self.handle_paper_sync)
        app.router.add_get("/api/paper/auto-status", self.handle_auto_trader_status)
        app.router.add_post("/api/paper/auto-scan", self.handle_auto_trader_scan)

        # Sectors page
        app.router.add_get("/sectors", self.handle_sectors_page)
        app.router.add_get("/advanced-analytics", self.handle_advanced_analytics_page)

        # Main Data / FNO APIs
        app.router.add_get("/oi-thesis", self.handle_oi_thesis_page)
        app.router.add_get("/api/oi-thesis", self.handle_api_oi_thesis)
        app.router.add_get("/oi-scanner", self.handle_oi_scanner_page)
        app.router.add_get("/api/oi-scanner", self.handle_api_oi_scanner)
        app.router.add_get("/api/index-summary", self.handle_api_index_summary)

        # RSI pages
        app.router.add_get("/rsi", self.handle_rsi_page)


        # Admin routes
        app.router.add_get("/admin", self.handle_admin_page)
        app.router.add_get("/api/admin/status", self.handle_admin_status)
        app.router.add_post("/api/admin/token", self.handle_admin_token)
        app.router.add_post("/api/admin/fyers-token", self.handle_admin_fyers_token)
        app.router.add_post("/api/admin/test-broker", self.handle_admin_test_broker)
        app.router.add_get("/api/admin/settings", self.handle_admin_settings_get)
        app.router.add_post("/api/admin/settings", self.handle_admin_settings_update)
        app.router.add_get("/api/admin/trades/export", self.handle_admin_trades_export)
        app.router.add_post("/api/admin/trades/clear", self.handle_admin_trades_clear)
        app.router.add_get("/api/admin/logs", self.handle_admin_logs)
        app.router.add_post("/api/admin/rollover", self.handle_admin_rollover)

        # Candle data route
        app.router.add_get("/api/candles", self.handle_api_candles)
        app.router.add_get("/api/stock/oi_timeseries", self.handle_api_stock_oi_timeseries)
        app.router.add_get("/api/chain", self.handle_api_chain)
        app.router.add_get("/api/advanced-chain", self.handle_api_advanced_chain)

        # Nifty specialized routes
        app.router.add_get("/nifty", self.handle_nifty_page)
        app.router.add_get("/api/nifty/data", self.handle_api_nifty_data)
        app.router.add_get("/api/nifty/timeseries", self.handle_api_nifty_timeseries)
        app.router.add_get("/api/nifty/multi-strike-oi", self.handle_api_nifty_multi_strike_oi)
        app.router.add_get("/api/nifty/chart.png", self.handle_api_nifty_chart)

        # Chat AI route
        app.router.add_post("/api/chat", self.handle_api_chat)
        app.router.add_post("/api/admin/chat-key", self.handle_admin_settings_update_chat_key)

        # Static files (CSS, JS, assets)
        static_dir = Path(__file__).parent / "static"
        if static_dir.exists():
            app.router.add_static("/static/", path=static_dir, name="static")

        # Lifecycle hooks
        app.on_startup.append(self.on_startup)
        app.on_shutdown.append(self.on_shutdown)

        return app


# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------

def setup_signal_handlers(loop: asyncio.AbstractEventLoop):
    """Register graceful shutdown on SIGINT and SIGTERM."""
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(_signal_handler(s)))
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            pass


async def _signal_handler(sig):
    """Handle shutdown signal."""
    log.info("Received signal %s, initiating shutdown...", sig.name)
    # aiohttp's runner handles the actual shutdown
    raise web.GracefulExit()


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="F&O Stock Dashboard - Async WebSocket Server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--token", type=str, default=None,
        help="Upstox access token (overrides env var and config.env)",
    )
    parser.add_argument(
        "--port", type=int, default=8081,
        help="Server port (default: 8081)",
    )
    args = parser.parse_args()

    # Load config
    script_dir = str(Path(__file__).parent)
    config_vars = load_config_env(script_dir)

    # Apply config.env values to os.environ for downstream use
    for k, v in config_vars.items():
        if k not in os.environ:
            os.environ[k] = v

    # Resolve token
    token = resolve_token(args.token, config_vars)
    log.info("Token resolved (%d chars)", len(token))

    # Download and parse instruments
    stocks = download_and_parse_instruments()
    if not stocks:
        log.error("No F&O stocks found after parsing instruments. Exiting.")
        sys.exit(1)

    # Log summary
    sectors = defaultdict(int)
    n50_count = 0
    for s in stocks:
        sectors[s.sector] += 1
        if s.is_n50:
            n50_count += 1

    log.info("Loaded %d F&O stocks (%d Nifty 50)", len(stocks), n50_count)
    for sector, count in sorted(sectors.items(), key=lambda x: -x[1]):
        log.info("  %-12s: %d", sector, count)

    # Initialize data store
    store = DataStore(script_dir)

    # Check if token is available from DataStore settings
    stored_settings = store.get_settings()
    if not token or token == "paste_your_token_here":
        stored_token = stored_settings.get("upstox_token", "")
        if stored_token and len(stored_token) > 20:
            token = stored_token
            log.info("Token loaded from DataStore settings (%d chars)", len(token))

    # Create and run server
    server = DashboardServer(token=token, port=args.port, stocks=stocks, store=store)
    app = server.create_app()

    log.info("Starting server on port %d ...", args.port)
    log.info("  Dashboard:  http://localhost:%d/", args.port)
    log.info("  WebSocket:  ws://localhost:%d/ws", args.port)
    log.info("  API State:  http://localhost:%d/api/state", args.port)
    log.info("  Health:     http://localhost:%d/health", args.port)
    log.info("  Paper:      http://localhost:%d/paper", args.port)
    log.info("  Sectors:    http://localhost:%d/sectors", args.port)
    log.info("  Admin:      http://localhost:%d/admin", args.port)


    web.run_app(app, host="0.0.0.0", port=args.port, print=None)


if __name__ == "__main__":
    main()
