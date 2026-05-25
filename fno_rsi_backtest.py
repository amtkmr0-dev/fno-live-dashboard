#!/usr/bin/env python3
"""
F&O RSI Divergence Backtester
=============================
Backtests RSI divergence strategy across all 209 F&O stocks using 1-minute
Parquet data, resampled to 1m/3m/5m/15m/30m/1h timeframes. Sweeps a grid of SL/TGT
percentages, computes per-stock/side/timeframe statistics, and writes a
comprehensive JSON report.

Usage:
    python fno_rsi_backtest.py
    python fno_rsi_backtest.py --symbols RELIANCE,TCS --verbose
    python fno_rsi_backtest.py --limit 10 --workers 2 --trades-dir ./trades

Dependencies: pandas, pyarrow (standard on most GCP data-science images)
"""

import argparse
import glob
import json
import logging
import math
import multiprocessing
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta

import pandas as pd

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SL_PCT_GRID = [0.5, 0.75, 1.0, 1.5, 2.0, 3.0]
TGT_PCT_GRID = [1.0, 1.5, 2.0, 3.0, 4.0, 5.0]
TIMEFRAMES = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60}
DEFAULT_COST_PER_TRADE = 100  # Rs flat cost per trade (override with --cost-per-trade)

ENTRY_WINDOW = 10      # bars after signal to trigger entry
MIN_BARS_REQUIRED = 30 # skip stocks with fewer bars after resampling

# RSI / pivot parameters (match the verified engine exactly)
PIVOT_LEFT = 5
PIVOT_RIGHT = 5
RSI_LEN = 14
MIN_BARS = 5
MAX_BARS = 60

# Market hours in IST (hour, minute)
MARKET_OPEN_H, MARKET_OPEN_M = 9, 15
MARKET_CLOSE_H, MARKET_CLOSE_M = 15, 30

DEFAULT_DATA_DIR = "/home/amitkumar/nifty_bt/data/fno_stock_spot/5y_1m"
DEFAULT_OUTPUT = "./results/fno_rsi_results.json"

# ---------------------------------------------------------------------------
# Lot sizes for all 209 F&O stocks
# ---------------------------------------------------------------------------

LOT_SIZES = {
    "360ONE": 500, "ABB": 125, "ABCAPITAL": 3100, "ADANIENSOL": 675,
    "ADANIENT": 309, "ADANIGREEN": 600, "ADANIPORTS": 475, "ADANIPOWER": 3550,
    "ALKEM": 125, "AMBER": 100, "AMBUJACEM": 1050, "ANGELONE": 2500,
    "APLAPOLLO": 350, "APOLLOHOSP": 125, "ASHOKLEY": 5000, "ASIANPAINT": 250,
    "ASTRAL": 425, "AUBANK": 1000, "AUROPHARMA": 550, "AXISBANK": 625,
    "BAJAJ-AUTO": 75, "BAJAJFINSV": 250, "BAJAJHLDNG": 50, "BAJFINANCE": 750,
    "BANDHANBNK": 3600, "BANKBARODA": 2925, "BANKINDIA": 5200, "BDL": 350,
    "BEL": 1425, "BHARATFORG": 500, "BHARTIARTL": 475, "BHEL": 2625,
    "BIOCON": 2500, "BLUESTARCO": 325, "BOSCHLTD": 25, "BPCL": 1975,
    "BRITANNIA": 125, "BSE": 375, "CAMS": 750, "CANBK": 6750,
    "CDSL": 475, "CGPOWER": 850, "CHOLAFIN": 625, "CIPLA": 375,
    "COALINDIA": 1350, "COCHINSHIP": 400, "COFORGE": 375, "COLPAL": 225,
    "CONCOR": 1250, "CROMPTON": 1800, "CUMMINSIND": 200, "DABUR": 1250,
    "DALBHARAT": 325, "DELHIVERY": 2075, "DIVISLAB": 100, "DIXON": 50,
    "DLF": 825, "DMART": 150, "DRREDDY": 625, "EICHERMOT": 100,
    "ETERNAL": 2425, "EXIDEIND": 1800, "FEDERALBNK": 2500, "FORCEMOT": 25,
    "FORTIS": 775, "GAIL": 3150, "GLENMARK": 375, "GMRAIRPORT": 6975,
    "GODFRYPHLP": 275, "GODREJCP": 500, "GODREJPROP": 275, "GRASIM": 250,
    "HAL": 150, "HAVELLS": 500, "HCLTECH": 350, "HDFCAMC": 300,
    "HDFCBANK": 550, "HDFCLIFE": 1100, "HEROMOTOCO": 150, "HINDALCO": 700,
    "HINDPETRO": 2025, "HINDUNILVR": 300, "HINDZINC": 1225, "HYUNDAI": 275,
    "ICICIBANK": 700, "ICICIGI": 325, "ICICIPRULI": 925, "IDEA": 71475,
    "IDFCFIRSTB": 9275, "IEX": 3750, "INDHOTEL": 1000, "INDIANB": 1000,
    "INDIGO": 150, "INDUSINDBK": 700, "INDUSTOWER": 1700, "INFY": 400,
    "INOXWIND": 3575, "IOC": 4875, "IREDA": 3450, "IRFC": 4250,
    "ITC": 1600, "JINDALSTEL": 625, "JIOFIN": 2350, "JSWENERGY": 1000,
    "JSWSTEEL": 675, "JUBLFOOD": 1250, "KALYANKJIL": 1175, "KAYNES": 100,
    "KEI": 175, "KFINTECH": 500, "KOTAKBANK": 2000, "KPITTECH": 425,
    "LAURUSLABS": 850, "LICHSGFIN": 1000, "LICI": 700, "LODHA": 450,
    "LT": 175, "LTF": 2250, "LTM": 150, "LUPIN": 425,
    "M&M": 200, "MANAPPURAM": 3000, "MANKIND": 225, "MARICO": 1200,
    "MARUTI": 50, "MAXHEALTH": 525, "MAZDOCK": 200, "MCX": 625,
    "MFSL": 400, "MOTHERSON": 6150, "MOTILALOFS": 775, "MPHASIS": 275,
    "MUTHOOTFIN": 275, "NAM-INDIA": 625, "NATIONALUM": 1875, "NAUKRI": 375,
    "NBCC": 6500, "NESTLEIND": 500, "NHPC": 6400, "NMDC": 6750,
    "NTPC": 1500, "NUVAMA": 500, "NYKAA": 3125, "OBEROIRLTY": 350,
    "OFSS": 75, "OIL": 1400, "ONGC": 2250, "PAGEIND": 15,
    "PATANJALI": 900, "PAYTM": 725, "PERSISTENT": 100, "PETRONET": 1900,
    "PFC": 1300, "PGEL": 950, "PHOENIXLTD": 350, "PIDILITIND": 500,
    "PIIND": 175, "PNB": 8000, "PNBHOUSING": 650, "POLICYBZR": 350,
    "POLYCAB": 125, "POWERGRID": 1900, "POWERINDIA": 25, "PREMIERENE": 575,
    "PRESTIGE": 450, "RBLBANK": 3175, "RECLTD": 1400, "RELIANCE": 500,
    "RVNL": 1525, "SAIL": 4700, "SAMMAANCAP": 4300, "SBICARD": 800,
    "SBILIFE": 375, "SBIN": 750, "SHREECEM": 25, "SHRIRAMFIN": 825,
    "SIEMENS": 175, "SOLARINDS": 50, "SONACOMS": 1225, "SRF": 200,
    "SUNPHARMA": 350, "SUPREMEIND": 175, "SUZLON": 9025, "SWIGGY": 1300,
    "TATACONSUM": 550, "TATAELXSI": 100, "TATAPOWER": 1450, "TATASTEEL": 2750,
    "TCS": 175, "TECHM": 600, "TIINDIA": 200, "TITAN": 175,
    "TMPV": 800, "TORNTPHARM": 125, "TRENT": 100, "TVSMOTOR": 175,
    "ULTRACEMCO": 50, "UNIONBANK": 4425, "UNITDSPR": 400, "UNOMINDA": 550,
    "UPL": 1355, "VBL": 1125, "VEDL": 1150, "VMM": 4850,
    "VOLTAS": 375, "WAAREEENER": 175, "WIPRO": 3000, "YESBANK": 31100,
    "ZYDUSLIFE": 900,
}

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logger = logging.getLogger("fno_rsi")


def setup_logging(verbose: bool):
    level = logging.DEBUG if verbose else logging.INFO
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.setLevel(level)
    logger.addHandler(handler)


# ===================================================================
# RSI DIVERGENCE DETECTION ENGINE (EXACT COPY — DO NOT MODIFY)
# ===================================================================

def wilder_rsi(closes, period=14):
    n = len(closes)
    rsi = [float("nan")] * n
    if n < period + 1:
        return rsi
    gains, losses = [], []
    for i in range(1, n):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rsi[period] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1 + avg_gain / avg_loss)
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rsi[i + 1] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1 + avg_gain / avg_loss)
    return rsi


def find_rsi_pivots(rsi_values, left=5, right=5):
    n = len(rsi_values)
    pl = [False] * n
    ph = [False] * n
    for i in range(left, n - right):
        val = rsi_values[i]
        if math.isnan(val):
            continue
        neighbors = [
            j for j in range(i - left, i + right + 1)
            if j != i and 0 <= j < n and not math.isnan(rsi_values[j])
        ]
        if len(neighbors) < left + right:
            continue
        pl[i] = all(rsi_values[j] > val for j in neighbors)
        ph[i] = all(rsi_values[j] < val for j in neighbors)
    return pl, ph


def detect_divergences(highs, lows, rsi, pivot_lows, pivot_highs):
    N = len(rsi)
    pl_events, ph_events = [], []
    for i in range(PIVOT_RIGHT, N):
        pb = i - PIVOT_RIGHT
        if pivot_lows[pb]:
            pl_events.append((i, pb, lows[pb], rsi[pb], highs[pb]))
        if pivot_highs[pb]:
            ph_events.append((i, pb, highs[pb], rsi[pb], lows[pb]))

    bear_signals, bull_signals = [], []
    for idx in range(1, len(ph_events)):
        det_bar, pb, high_val, rsi_val, low_val = ph_events[idx]
        prev_det = ph_events[idx - 1][0]
        prev_high = ph_events[idx - 1][2]
        prev_rsi = ph_events[idx - 1][3]
        barssince = det_bar - (prev_det + 1)
        if barssince < MIN_BARS or barssince > MAX_BARS:
            continue
        if high_val > prev_high and rsi_val < prev_rsi:
            bear_signals.append({
                "det_bar": det_bar,
                "pivot_bar": pb,
                "trigger": low_val,
                "price": high_val,
                "rsi": rsi_val,
                "barssince": barssince,
            })

    for idx in range(1, len(pl_events)):
        det_bar, pb, low_val, rsi_val, high_val = pl_events[idx]
        prev_det = pl_events[idx - 1][0]
        prev_low = pl_events[idx - 1][2]
        prev_rsi = pl_events[idx - 1][3]
        barssince = det_bar - (prev_det + 1)
        if barssince < MIN_BARS or barssince > MAX_BARS:
            continue
        if low_val < prev_low and rsi_val > prev_rsi:
            bull_signals.append({
                "det_bar": det_bar,
                "pivot_bar": pb,
                "trigger": high_val,
                "price": low_val,
                "rsi": rsi_val,
                "barssince": barssince,
            })

    return bear_signals, bull_signals


# ===================================================================
# END OF RSI DIVERGENCE DETECTION ENGINE
# ===================================================================


# ---------------------------------------------------------------------------
# Data loading and resampling
# ---------------------------------------------------------------------------

# Column name aliases — we try each group in order and use the first match
_TS_ALIASES = ["timestamp", "ts", "date", "datetime", "time", "Date", "Timestamp", "DateTime"]
_OPEN_ALIASES = ["open", "o", "Open", "OPEN"]
_HIGH_ALIASES = ["high", "h", "High", "HIGH"]
_LOW_ALIASES = ["low", "l", "Low", "LOW"]
_CLOSE_ALIASES = ["close", "c", "Close", "CLOSE"]
_VOLUME_ALIASES = ["volume", "vol", "v", "Volume", "VOLUME", "Vol"]


def _find_col(df_columns, aliases):
    """Return the first column name from *aliases* that exists in df_columns."""
    col_set = set(df_columns)
    for alias in aliases:
        if alias in col_set:
            return alias
    return None


def extract_symbol(filepath: str) -> str:
    """
    Extract the stock symbol from a parquet filename.
    Strips common suffixes like _1m, _1m_5y, _spot, _5y, etc.
    """
    basename = os.path.basename(filepath).replace(".parquet", "")
    # Progressively strip known suffixes (order matters: longer first)
    for suffix in [
        "_1m_5y", "_5y_1m", "_1m_5yr", "_5yr_1m", "_1min_5y",
        "_1m_2y", "_2y_1m", "_1m_2yr", "_2yr_1m", "_1min_2y",
        "_1m_1y", "_1y_1m", "_1m_1yr", "_1yr_1m", "_1min_1y",
        "_1m_3y", "_3y_1m", "_gap_3y_1m",
        "_1min", "_1m", "_5y", "_5yr", "_2y", "_2yr", "_1y", "_1yr", "_spot",
    ]:
        if basename.lower().endswith(suffix):
            basename = basename[: len(basename) - len(suffix)]
            break
    return basename.upper()


def load_parquet(filepath: str) -> pd.DataFrame:
    """
    Load a parquet file, auto-detect column names, normalise to
    [timestamp, open, high, low, close, volume], filter to market hours,
    and sort by timestamp.
    """
    df = pd.read_parquet(filepath)

    if df.empty:
        raise ValueError("Empty parquet file")

    cols = list(df.columns)

    # --- Detect and rename columns ---
    ts_col = _find_col(cols, _TS_ALIASES)
    open_col = _find_col(cols, _OPEN_ALIASES)
    high_col = _find_col(cols, _HIGH_ALIASES)
    low_col = _find_col(cols, _LOW_ALIASES)
    close_col = _find_col(cols, _CLOSE_ALIASES)
    vol_col = _find_col(cols, _VOLUME_ALIASES)

    # If timestamp column not found, check if the index is a DatetimeIndex
    if ts_col is None:
        if isinstance(df.index, pd.DatetimeIndex):
            df = df.reset_index()
            ts_col = df.columns[0]
        else:
            raise ValueError(
                f"Cannot find timestamp column. Available: {cols}"
            )

    for required, name in [
        (open_col, "open"), (high_col, "high"),
        (low_col, "low"), (close_col, "close"),
    ]:
        if required is None:
            raise ValueError(f"Cannot find '{name}' column. Available: {cols}")

    rename_map = {
        ts_col: "timestamp",
        open_col: "open",
        high_col: "high",
        low_col: "low",
        close_col: "close",
    }
    if vol_col is not None:
        rename_map[vol_col] = "volume"

    df = df.rename(columns=rename_map)

    # Keep only needed columns
    keep = ["timestamp", "open", "high", "low", "close"]
    if "volume" in df.columns:
        keep.append("volume")
    df = df[keep].copy()

    # Ensure timestamp is proper datetime
    df["timestamp"] = pd.to_datetime(df["timestamp"])

    # Sort chronologically
    df.sort_values("timestamp", inplace=True)
    df.reset_index(drop=True, inplace=True)

    # --- Filter to market hours (9:15 - 15:29 IST, inclusive) ---
    # We keep candles whose timestamp falls within 09:15:00 to 15:29:59.
    # The 15:30 candle (if it exists) is the close auction and is excluded
    # because it is not a normal trading candle.
    #
    # Assumption: timestamps are already in IST (Indian market data).
    # If they are UTC, the caller should convert before passing in,
    # but we handle the common case of IST data here.
    hour = df["timestamp"].dt.hour
    minute = df["timestamp"].dt.minute
    time_val = hour * 60 + minute  # minutes since midnight
    market_open = MARKET_OPEN_H * 60 + MARKET_OPEN_M   # 555
    market_close = MARKET_CLOSE_H * 60 + MARKET_CLOSE_M  # 930
    # Keep 09:15 (555) through 15:29 (929)
    mask = (time_val >= market_open) & (time_val < market_close)
    df = df.loc[mask].copy()
    df.reset_index(drop=True, inplace=True)

    return df


def resample_ohlcv(df_1m: pd.DataFrame, tf_minutes: int) -> pd.DataFrame:
    """
    Resample 1-minute OHLCV data to the target timeframe.
    Groups candles by flooring the timestamp to the nearest tf_minutes interval.
    """
    df = df_1m.copy()
    df.set_index("timestamp", inplace=True)

    rule = f"{tf_minutes}min"

    agg = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
    }
    if "volume" in df.columns:
        agg["volume"] = "sum"

    # origin='start_day' ensures 9:15 is a bucket boundary for 15m
    # offset ensures alignment: 9:15, 9:30, 9:45, ...
    resampled = (
        df.resample(rule, offset=f"{MARKET_OPEN_M}min")
        .agg(agg)
        .dropna(subset=["open"])
    )
    resampled.reset_index(inplace=True)

    # Re-filter to market hours (resampling might create edge buckets)
    hour = resampled["timestamp"].dt.hour
    minute = resampled["timestamp"].dt.minute
    time_val = hour * 60 + minute
    market_open = MARKET_OPEN_H * 60 + MARKET_OPEN_M
    market_close = MARKET_CLOSE_H * 60 + MARKET_CLOSE_M
    mask = (time_val >= market_open) & (time_val < market_close)
    resampled = resampled.loc[mask].copy()
    resampled.reset_index(drop=True, inplace=True)

    return resampled


# ---------------------------------------------------------------------------
# Trade simulation
# ---------------------------------------------------------------------------

def simulate_trades(
    signals: list,
    side: str,
    highs: list,
    lows: list,
    closes: list,
    timestamps: list,
    sl_pct: float,
    tgt_pct: float,
    no_overlap: bool = False,
) -> list:
    """
    Simulate trades for a list of signals (bear or bull) with given SL/TGT.

    For each signal:
      1. Starting from det_bar, scan up to ENTRY_WINDOW bars for trigger hit.
      2. If triggered, set SL and TGT based on entry price.
      3. Scan forward bar-by-bar for SL or TGT hit.
      4. Record the trade result.

    If no_overlap=True, signals that fire while a previous trade is still
    open are skipped (realistic single-position constraint).

    Returns a list of trade dicts.
    """
    n_bars = len(closes)
    trades = []
    last_exit_bar = -1  # tracks when the last trade exited (for no_overlap)

    for sig in signals:
        det_bar = sig["det_bar"]
        trigger = sig["trigger"]

        # --- No-overlap filter: skip if previous trade still open ---
        if no_overlap and det_bar <= last_exit_bar:
            continue

        # --- Phase 1: Look for entry within ENTRY_WINDOW ---
        entry_bar = None
        entry_price = None

        for b in range(det_bar, min(det_bar + ENTRY_WINDOW, n_bars)):
            if side == "bear":
                # Entry triggers when price drops below trigger (pivot low)
                if lows[b] <= trigger:
                    entry_bar = b
                    # Use the trigger price as entry (limit-style)
                    entry_price = trigger
                    break
            else:  # bull
                # Entry triggers when price rises above trigger (pivot high)
                if highs[b] >= trigger:
                    entry_bar = b
                    entry_price = trigger
                    break

        if entry_bar is None:
            continue  # No entry within window — skip

        # --- Phase 2: Compute SL and TGT levels ---
        if side == "bear":
            sl_level = entry_price + (sl_pct / 100.0 * entry_price)
            tgt_level = entry_price - (tgt_pct / 100.0 * entry_price)
        else:  # bull
            sl_level = entry_price - (sl_pct / 100.0 * entry_price)
            tgt_level = entry_price + (tgt_pct / 100.0 * entry_price)

        # --- Phase 3: Scan forward for exit ---
        exit_bar = None
        exit_price = None
        result = None

        for b in range(entry_bar + 1, n_bars):
            if side == "bear":
                # Check SL first (high >= SL)
                if highs[b] >= sl_level:
                    exit_bar = b
                    exit_price = sl_level
                    result = "LOSS"
                    break
                # Check TGT (low <= TGT)
                if lows[b] <= tgt_level:
                    exit_bar = b
                    exit_price = tgt_level
                    result = "WIN"
                    break
            else:  # bull
                # Check SL first (low <= SL)
                if lows[b] <= sl_level:
                    exit_bar = b
                    exit_price = sl_level
                    result = "LOSS"
                    break
                # Check TGT (high >= TGT)
                if highs[b] >= tgt_level:
                    exit_bar = b
                    exit_price = tgt_level
                    result = "WIN"
                    break

        # If neither hit, trade expires at last bar
        if exit_bar is None:
            exit_bar = n_bars - 1
            exit_price = closes[exit_bar]
            result = "EXPIRED"

        # --- Compute P&L ---
        if side == "bear":
            pnl_pts = entry_price - exit_price
        else:
            pnl_pts = exit_price - entry_price

        pnl_pct = (pnl_pts / entry_price) * 100.0 if entry_price != 0 else 0.0

        trade = {
            "entry_bar": entry_bar,
            "exit_bar": exit_bar,
            "entry_price": round(entry_price, 2),
            "exit_price": round(exit_price, 2),
            "sl_level": round(sl_level, 2),
            "tgt_level": round(tgt_level, 2),
            "pnl_pts": round(pnl_pts, 2),
            "pnl_pct": round(pnl_pct, 4),
            "result": result,
            "det_bar": det_bar,
            "pivot_bar": sig["pivot_bar"],
            "rsi_at_signal": round(sig["rsi"], 2),
        }

        # Add timestamps if available
        if timestamps is not None and len(timestamps) > exit_bar:
            trade["entry_ts"] = str(timestamps[entry_bar])
            trade["exit_ts"] = str(timestamps[exit_bar])

        trades.append(trade)

        # Update last exit bar for no-overlap tracking
        last_exit_bar = exit_bar

    return trades


# ---------------------------------------------------------------------------
# Stats computation
# ---------------------------------------------------------------------------

def compute_stats(trades: list, lot_size: int, cost_per_trade: float = 100) -> dict:
    """
    Compute strategy statistics from a list of trade dicts.
    Returns a dict with n, wr, pf, net_pct, net_inr, max_dd_pct, mcl,
    and monthly_pnl breakdown.
    """
    n = len(trades)
    if n == 0:
        return {
            "n": 0, "wr": 0.0, "pf": 0.0, "net_pct": 0.0,
            "net_inr": 0.0, "max_dd_pct": 0.0, "mcl": 0,
            "monthly_pnl": {},
        }

    wins = sum(1 for t in trades if t["result"] == "WIN")
    losses = sum(1 for t in trades if t["result"] == "LOSS")
    expired = n - wins - losses

    wr = (wins / n) * 100.0 if n > 0 else 0.0

    # Profit factor: gross profit / gross loss
    gross_profit = sum(t["pnl_pct"] for t in trades if t["pnl_pct"] > 0)
    gross_loss = abs(sum(t["pnl_pct"] for t in trades if t["pnl_pct"] < 0))
    pf = (gross_profit / gross_loss) if gross_loss > 0 else (
        999.99 if gross_profit > 0 else 0.0
    )

    net_pct = sum(t["pnl_pct"] for t in trades)

    # Net INR: for each trade, pnl_pts * lot_size - cost
    net_inr = sum(
        t["pnl_pts"] * lot_size - cost_per_trade for t in trades
    )

    # Max drawdown (cumulative pct returns)
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in trades:
        cum += t["pnl_pct"]
        if cum > peak:
            peak = cum
        dd = cum - peak
        if dd < max_dd:
            max_dd = dd

    # Max consecutive losses
    mcl = 0
    current_streak = 0
    for t in trades:
        if t["result"] == "LOSS":
            current_streak += 1
            if current_streak > mcl:
                mcl = current_streak
        else:
            current_streak = 0

    # Monthly P&L breakdown (keyed by "YYYY-MM")
    monthly_pnl = defaultdict(lambda: {"n": 0, "pnl_pct": 0.0, "pnl_inr": 0.0})
    for t in trades:
        ts_str = t.get("entry_ts", "")
        if ts_str:
            month_key = ts_str[:7]  # "YYYY-MM"
        else:
            month_key = "unknown"
        monthly_pnl[month_key]["n"] += 1
        monthly_pnl[month_key]["pnl_pct"] += t["pnl_pct"]
        monthly_pnl[month_key]["pnl_inr"] += t["pnl_pts"] * lot_size - cost_per_trade

    # Round monthly values
    monthly_out = {}
    for k, v in sorted(monthly_pnl.items()):
        monthly_out[k] = {
            "n": v["n"],
            "pnl_pct": round(v["pnl_pct"], 4),
            "pnl_inr": round(v["pnl_inr"], 2),
        }

    return {
        "n": n,
        "wins": wins,
        "losses": losses,
        "expired": expired,
        "wr": round(wr, 2),
        "pf": round(pf, 4),
        "net_pct": round(net_pct, 4),
        "net_inr": round(net_inr, 2),
        "max_dd_pct": round(max_dd, 4),
        "mcl": mcl,
        "monthly_pnl": monthly_out,
    }


def pick_best_config(grid_results: list) -> dict:
    """
    Pick the best config: rank by profit_factor among configs with n >= 5,
    break ties by net_inr.
    Returns the best config dict or None if nothing qualifies.
    """
    eligible = [g for g in grid_results if g["n"] >= 5]
    if not eligible:
        # Fallback: pick the one with highest n
        if grid_results:
            eligible = grid_results
        else:
            return None

    eligible.sort(key=lambda x: (-x["pf"], -x["net_inr"]))
    return eligible[0]


# ---------------------------------------------------------------------------
# Per-stock processing (runs in worker process)
# ---------------------------------------------------------------------------

def process_stock(args):
    """
    Process a single stock across all timeframes and SL/TGT combos.
    This is the top-level function dispatched to multiprocessing workers.
    """
    filepath, symbol, trades_dir, no_overlap, cost_per_trade, start_date, end_date = args
    result = {"symbol": symbol}

    try:
        df_1m = load_parquet(filepath)
    except Exception as e:
        logger.warning(f"[{symbol}] Failed to load parquet: {e}")
        result["error"] = str(e)
        return result

    # --- Apply date filters for walk-forward splits ---
    if start_date:
        ts = pd.Timestamp(start_date)
        if df_1m["timestamp"].dt.tz is not None:
            ts = ts.tz_localize(df_1m["timestamp"].dt.tz)
        df_1m = df_1m[df_1m["timestamp"] >= ts].copy()
        df_1m.reset_index(drop=True, inplace=True)
    if end_date:
        ts = pd.Timestamp(end_date)
        if df_1m["timestamp"].dt.tz is not None:
            ts = ts.tz_localize(df_1m["timestamp"].dt.tz)
        df_1m = df_1m[df_1m["timestamp"] < ts].copy()
        df_1m.reset_index(drop=True, inplace=True)

    if len(df_1m) < 100:
        logger.warning(f"[{symbol}] Only {len(df_1m)} 1m bars after market-hour filter — skipping")
        result["error"] = f"Too few 1m bars: {len(df_1m)}"
        return result

    lot_size = LOT_SIZES.get(symbol, 1)
    logger.debug(f"[{symbol}] Loaded {len(df_1m)} 1m bars, lot_size={lot_size}")

    for tf_label, tf_minutes in TIMEFRAMES.items():
        # --- Resample (skip for 1m — use raw data) ---
        try:
            if tf_minutes == 1:
                df_tf = df_1m.copy()
                df_tf.reset_index(drop=True, inplace=True)
            else:
                df_tf = resample_ohlcv(df_1m, tf_minutes)
        except Exception as e:
            logger.warning(f"[{symbol}] Resample to {tf_label} failed: {e}")
            result[f"error_{tf_label}"] = str(e)
            continue

        n_bars = len(df_tf)
        result[f"bars_{tf_label}"] = n_bars

        if n_bars < MIN_BARS_REQUIRED:
            logger.debug(f"[{symbol}] Only {n_bars} bars at {tf_label} — skipping")
            continue

        # Convert to lists for the engine (faster than repeated DataFrame access)
        opens = df_tf["open"].tolist()
        highs = df_tf["high"].tolist()
        lows = df_tf["low"].tolist()
        closes = df_tf["close"].tolist()
        timestamps = df_tf["timestamp"].tolist()

        # --- RSI and pivot detection (once per stock/tf) ---
        rsi = wilder_rsi(closes, RSI_LEN)
        pivot_lows, pivot_highs = find_rsi_pivots(rsi, PIVOT_LEFT, PIVOT_RIGHT)

        # --- Detect divergences ---
        bear_signals, bull_signals = detect_divergences(
            highs, lows, rsi, pivot_lows, pivot_highs
        )

        logger.debug(
            f"[{symbol}][{tf_label}] Signals: {len(bear_signals)} bear, "
            f"{len(bull_signals)} bull"
        )

        # --- Sweep SL/TGT grid for each side ---
        for side, signals, key_prefix in [
            ("bear", bear_signals, f"bear_{tf_label}"),
            ("bull", bull_signals, f"bull_{tf_label}"),
        ]:
            side_result = {
                "signals_detected": len(signals),
                "grid": [],
                "best_config": None,
            }

            if len(signals) == 0:
                result[key_prefix] = side_result
                continue

            all_grid_trades = {}  # (sl, tgt) -> trades list

            for sl_pct in SL_PCT_GRID:
                for tgt_pct in TGT_PCT_GRID:
                    trades = simulate_trades(
                        signals, side, highs, lows, closes, timestamps,
                        sl_pct, tgt_pct, no_overlap=no_overlap,
                    )
                    stats = compute_stats(trades, lot_size, cost_per_trade=cost_per_trade)

                    grid_entry = {
                        "sl_pct": sl_pct,
                        "tgt_pct": tgt_pct,
                        "n": stats["n"],
                        "wr": stats["wr"],
                        "pf": stats["pf"],
                        "net_pct": stats["net_pct"],
                        "net_inr": stats["net_inr"],
                        "max_dd_pct": stats["max_dd_pct"],
                        "mcl": stats["mcl"],
                    }
                    side_result["grid"].append(grid_entry)
                    all_grid_trades[(sl_pct, tgt_pct)] = (trades, stats)

            # Pick best config
            best = pick_best_config(side_result["grid"])
            if best is not None:
                # Attach monthly P&L from the best config's trades
                best_key = (best["sl_pct"], best["tgt_pct"])
                _, best_stats = all_grid_trades[best_key]
                best_with_monthly = dict(best)
                best_with_monthly["monthly_pnl"] = best_stats["monthly_pnl"]
                side_result["best_config"] = best_with_monthly

                # Optionally dump trades CSV for best config
                if trades_dir:
                    best_trades, _ = all_grid_trades[best_key]
                    _dump_trades_csv(
                        trades_dir, symbol, side, tf_label,
                        best["sl_pct"], best["tgt_pct"], best_trades,
                    )

            result[key_prefix] = side_result

    # --- Determine best_overall across all sides/tfs ---
    best_overall = None
    best_pf = -1
    best_net = -float("inf")
    for side in ["bear", "bull"]:
        for tf_label in TIMEFRAMES.keys():
            key = f"{side}_{tf_label}"
            if key not in result or not isinstance(result[key], dict):
                continue
            bc = result[key].get("best_config")
            if bc is None or bc.get("n", 0) < 5:
                continue
            pf = bc.get("pf", 0)
            net = bc.get("net_inr", 0)
            if pf > best_pf or (pf == best_pf and net > best_net):
                best_pf = pf
                best_net = net
                best_overall = {
                    "side": side,
                    "tf": tf_label,
                    "config": {k: v for k, v in bc.items() if k != "monthly_pnl"},
                }

    result["best_overall"] = best_overall
    return result


def _dump_trades_csv(trades_dir, symbol, side, tf_label, sl, tgt, trades):
    """Write a CSV of trades for one stock/side/tf/config to trades_dir."""
    os.makedirs(trades_dir, exist_ok=True)
    fname = f"{symbol}_{side}_{tf_label}_sl{sl}_tgt{tgt}.csv"
    path = os.path.join(trades_dir, fname)
    try:
        df = pd.DataFrame(trades)
        df.to_csv(path, index=False)
    except Exception as e:
        logger.warning(f"Failed to write trades CSV {path}: {e}")


# ---------------------------------------------------------------------------
# File discovery and symbol extraction
# ---------------------------------------------------------------------------

def discover_files(data_dir: str, symbols_filter=None, limit=None):
    """
    Discover all .parquet files in data_dir. Return list of (filepath, symbol).
    Optionally filter to specific symbols or limit count.
    """
    pattern = os.path.join(data_dir, "*.parquet")
    files = sorted(glob.glob(pattern))

    if not files:
        # Try recursive search one level deep
        pattern2 = os.path.join(data_dir, "**", "*.parquet")
        files = sorted(glob.glob(pattern2, recursive=True))

    if not files:
        logger.error(f"No .parquet files found in {data_dir}")
        return []

    result = []
    for fp in files:
        sym = extract_symbol(fp)
        if symbols_filter and sym not in symbols_filter:
            continue
        result.append((fp, sym))

    if limit and limit > 0:
        result = result[:limit]

    logger.info(f"Discovered {len(result)} parquet files in {data_dir}")
    return result


# ---------------------------------------------------------------------------
# Rankings and summary
# ---------------------------------------------------------------------------

def build_rankings(stocks_data: dict) -> dict:
    """Build top-50 and profitable/unprofitable stock rankings."""
    # Collect all best_overall entries that exist
    entries = []
    for sym, data in stocks_data.items():
        bo = data.get("best_overall")
        if bo and bo.get("config"):
            entries.append({
                "symbol": sym,
                "side": bo["side"],
                "tf": bo["tf"],
                **bo["config"],
            })

    # Top 50 by profit factor (n >= 5)
    qualified = [e for e in entries if e.get("n", 0) >= 5]
    top_by_pf = sorted(qualified, key=lambda x: (-x.get("pf", 0), -x.get("net_inr", 0)))[:50]

    # Top 50 by net INR
    top_by_inr = sorted(qualified, key=lambda x: -x.get("net_inr", 0))[:50]

    # Profitable / unprofitable
    profitable = [e["symbol"] for e in entries if e.get("net_inr", 0) > 0]
    unprofitable = [e["symbol"] for e in entries if e.get("net_inr", 0) <= 0]

    return {
        "top_50_by_pf": top_by_pf,
        "top_50_by_net_inr": top_by_inr,
        "profitable_stocks": sorted(profitable),
        "unprofitable_stocks": sorted(unprofitable),
    }


def build_summary(stocks_data: dict, rankings: dict) -> dict:
    """Build the top-level summary section."""
    n_profitable = len(rankings["profitable_stocks"])
    n_no_signals = 0
    total_signals = 0
    best_stock = None
    best_net = -float("inf")
    worst_stock = None
    worst_net = float("inf")

    for sym, data in stocks_data.items():
        if "error" in data:
            continue

        sym_signals = 0
        for side in ["bear", "bull"]:
            for tf_label in TIMEFRAMES.keys():
                key = f"{side}_{tf_label}"
                if key in data and isinstance(data[key], dict):
                    sym_signals += data[key].get("signals_detected", 0)

        total_signals += sym_signals
        if sym_signals == 0:
            n_no_signals += 1

        bo = data.get("best_overall")
        if bo and bo.get("config"):
            net = bo["config"].get("net_inr", 0)
            if net > best_net:
                best_net = net
                best_stock = sym
            if net < worst_net:
                worst_net = net
                worst_stock = sym

    n_stocks = len([s for s in stocks_data if "error" not in stocks_data[s]])
    avg_signals = total_signals / n_stocks if n_stocks > 0 else 0

    return {
        "stocks_with_profitable_configs": n_profitable,
        "stocks_with_no_signals": n_no_signals,
        "avg_signals_per_stock": round(avg_signals, 1),
        "best_stock": best_stock,
        "worst_stock": worst_stock,
        "total_stocks_processed": n_stocks,
        "total_stocks_errored": len(stocks_data) - n_stocks,
    }


# ---------------------------------------------------------------------------
# Progress tracker (simple stderr prints, no tqdm)
# ---------------------------------------------------------------------------

class ProgressTracker:
    """Thread-safe progress counter printing to stderr."""

    def __init__(self, total: int):
        self.total = total
        self.count = 0
        self.start_time = time.time()
        self.lock = multiprocessing.Lock()

    def update(self, symbol: str):
        with self.lock:
            self.count += 1
            elapsed = time.time() - self.start_time
            rate = self.count / elapsed if elapsed > 0 else 0
            eta = (self.total - self.count) / rate if rate > 0 else 0
            print(
                f"\r  [{self.count}/{self.total}] {symbol:20s} "
                f"({rate:.1f} stocks/sec, ETA {eta:.0f}s)   ",
                end="", file=sys.stderr, flush=True,
            )


# We use a module-level counter for multiprocessing (shared via initializer)
_progress_counter = None
_progress_total = None
_progress_start = None


def _init_progress(total):
    """Initializer for multiprocessing pool — sets up shared progress state."""
    global _progress_counter, _progress_total, _progress_start
    _progress_counter = multiprocessing.Value("i", 0)
    _progress_total = total
    _progress_start = time.time()


def _update_progress(symbol):
    """Called after each stock completes in the pool."""
    global _progress_counter, _progress_total, _progress_start
    if _progress_counter is None:
        return
    with _progress_counter.get_lock():
        _progress_counter.value += 1
        count = _progress_counter.value
    elapsed = time.time() - _progress_start
    rate = count / elapsed if elapsed > 0 else 0
    eta = (_progress_total - count) / rate if rate > 0 else 0
    print(
        f"\r  [{count}/{_progress_total}] {symbol:20s} "
        f"({rate:.1f} stocks/sec, ETA {eta:.0f}s)   ",
        end="", file=sys.stderr, flush=True,
    )


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="F&O RSI Divergence Backtester — backtest 209 F&O stocks"
    )
    parser.add_argument(
        "--data-dir", default=DEFAULT_DATA_DIR,
        help=f"Directory with 1m parquet files (default: {DEFAULT_DATA_DIR})"
    )
    parser.add_argument(
        "--output", default=DEFAULT_OUTPUT,
        help=f"Output JSON file path (default: {DEFAULT_OUTPUT})"
    )
    parser.add_argument(
        "--workers", type=int, default=None,
        help="Number of multiprocessing workers (default: cpu_count)"
    )
    parser.add_argument(
        "--symbols", default=None,
        help="Comma-separated symbols to process (default: all)"
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Process only first N files (for testing)"
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Enable debug logging"
    )
    parser.add_argument(
        "--trades-dir", default=None,
        help="Directory to dump per-stock trade CSVs for best configs"
    )
    parser.add_argument(
        "--no-overlap", action="store_true",
        help="Skip signals that fire while a previous trade is still open"
    )
    parser.add_argument(
        "--cost-per-trade", type=float, default=DEFAULT_COST_PER_TRADE,
        help=f"Cost per trade in Rs (default: {DEFAULT_COST_PER_TRADE})"
    )
    parser.add_argument(
        "--start-date", default=None,
        help="Filter data starting from this date (YYYY-MM-DD), inclusive"
    )
    parser.add_argument(
        "--end-date", default=None,
        help="Filter data up to this date (YYYY-MM-DD), exclusive"
    )
    args = parser.parse_args()

    setup_logging(args.verbose)

    # Parse optional symbol filter
    symbols_filter = None
    if args.symbols:
        symbols_filter = set(s.strip().upper() for s in args.symbols.split(","))

    # Discover files
    file_list = discover_files(args.data_dir, symbols_filter, args.limit)
    if not file_list:
        logger.error("No files to process. Exiting.")
        sys.exit(1)

    total = len(file_list)
    n_workers = args.workers or multiprocessing.cpu_count()
    n_workers = min(n_workers, total)  # Don't create more workers than tasks
    total_configs = total * len(TIMEFRAMES) * 2 * len(SL_PCT_GRID) * len(TGT_PCT_GRID)

    print(f"\n{'='*70}", file=sys.stderr)
    print(f"  F&O RSI Divergence Backtester", file=sys.stderr)
    print(f"{'='*70}", file=sys.stderr)
    print(f"  Data dir    : {args.data_dir}", file=sys.stderr)
    print(f"  Stocks      : {total}", file=sys.stderr)
    print(f"  Timeframes  : {list(TIMEFRAMES.keys())}", file=sys.stderr)
    print(f"  SL grid     : {SL_PCT_GRID}", file=sys.stderr)
    print(f"  TGT grid    : {TGT_PCT_GRID}", file=sys.stderr)
    print(f"  Configs/stock: {len(TIMEFRAMES) * 2 * len(SL_PCT_GRID) * len(TGT_PCT_GRID)}", file=sys.stderr)
    print(f"  Total configs: {total_configs}", file=sys.stderr)
    print(f"  Workers     : {n_workers}", file=sys.stderr)
    print(f"  Output      : {args.output}", file=sys.stderr)
    if args.trades_dir:
        print(f"  Trades dir  : {args.trades_dir}", file=sys.stderr)
    print(f"  No-overlap  : {args.no_overlap}", file=sys.stderr)
    print(f"  Cost/trade  : Rs {args.cost_per_trade}", file=sys.stderr)
    if args.start_date:
        print(f"  Start date  : {args.start_date}", file=sys.stderr)
    if args.end_date:
        print(f"  End date    : {args.end_date}", file=sys.stderr)
    print(f"{'='*70}\n", file=sys.stderr)

    # Prepare worker arguments
    worker_args = [
        (fp, sym, args.trades_dir, args.no_overlap, args.cost_per_trade,
         args.start_date, args.end_date)
        for fp, sym in file_list
    ]

    t0 = time.time()

    # --- Run with multiprocessing ---
    # We use a shared counter for progress. Since multiprocessing.Value
    # cannot easily be passed via Pool, we use imap_unordered and print
    # progress in the main process after each result.
    stocks_data = {}
    completed = 0

    if n_workers <= 1:
        # Single-process mode (easier to debug)
        for wa in worker_args:
            res = process_stock(wa)
            sym = res.get("symbol", wa[1])
            stocks_data[sym] = res
            completed += 1
            elapsed = time.time() - t0
            rate = completed / elapsed if elapsed > 0 else 0
            eta = (total - completed) / rate if rate > 0 else 0
            print(
                f"\r  [{completed}/{total}] {sym:20s} "
                f"({rate:.1f} stocks/sec, ETA {eta:.0f}s)   ",
                end="", file=sys.stderr, flush=True,
            )
    else:
        with multiprocessing.Pool(processes=n_workers) as pool:
            for res in pool.imap_unordered(process_stock, worker_args):
                sym = res.get("symbol", "???")
                stocks_data[sym] = res
                completed += 1
                elapsed = time.time() - t0
                rate = completed / elapsed if elapsed > 0 else 0
                eta = (total - completed) / rate if rate > 0 else 0
                print(
                    f"\r  [{completed}/{total}] {sym:20s} "
                    f"({rate:.1f} stocks/sec, ETA {eta:.0f}s)   ",
                    end="", file=sys.stderr, flush=True,
                )

    elapsed_total = time.time() - t0
    print(f"\n\n  Completed in {elapsed_total:.1f} seconds.\n", file=sys.stderr)

    # --- Determine data period from directory name ---
    data_dir_name = os.path.basename(os.path.normpath(args.data_dir))
    data_period = f"{data_dir_name} (1m candles)"

    # --- Build rankings and summary ---
    rankings = build_rankings(stocks_data)
    summary = build_summary(stocks_data, rankings)

    # --- Assemble final output ---
    output = {
        "meta": {
            "data_dir": args.data_dir,
            "data_period": data_period,
            "stocks_processed": total,
            "total_configs_tested": total_configs,
            "runtime_seconds": round(elapsed_total, 2),
            "cost_per_trade": args.cost_per_trade,
            "no_overlap": args.no_overlap,
            "start_date": args.start_date,
            "end_date": args.end_date,
            "sl_grid": SL_PCT_GRID,
            "tgt_grid": TGT_PCT_GRID,
            "timeframes": list(TIMEFRAMES.keys()),
            "entry_window": ENTRY_WINDOW,
            "rsi_period": RSI_LEN,
            "pivot_left": PIVOT_LEFT,
            "pivot_right": PIVOT_RIGHT,
        },
        "stocks": stocks_data,
        "rankings": rankings,
        "summary": summary,
    }

    # --- Write output JSON ---
    output_path = args.output
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2, default=str)

    print(f"  Results written to: {output_path}", file=sys.stderr)
    print(f"  File size: {os.path.getsize(output_path) / 1024 / 1024:.1f} MB\n", file=sys.stderr)

    # --- Print quick summary to stderr ---
    print(f"  {'='*50}", file=sys.stderr)
    print(f"  SUMMARY", file=sys.stderr)
    print(f"  {'='*50}", file=sys.stderr)
    print(f"  Stocks processed           : {summary['total_stocks_processed']}", file=sys.stderr)
    print(f"  Stocks with errors          : {summary['total_stocks_errored']}", file=sys.stderr)
    print(f"  Stocks with no signals      : {summary['stocks_with_no_signals']}", file=sys.stderr)
    print(f"  Stocks with profitable cfg  : {summary['stocks_with_profitable_configs']}", file=sys.stderr)
    print(f"  Avg signals per stock       : {summary['avg_signals_per_stock']}", file=sys.stderr)
    print(f"  Best stock                  : {summary['best_stock']}", file=sys.stderr)
    print(f"  Worst stock                 : {summary['worst_stock']}", file=sys.stderr)
    print(f"  {'='*50}\n", file=sys.stderr)

    # Top 5 by profit factor
    top5 = rankings["top_50_by_pf"][:5]
    if top5:
        print(f"  TOP 5 BY PROFIT FACTOR:", file=sys.stderr)
        for i, e in enumerate(top5, 1):
            print(
                f"    {i}. {e['symbol']:15s} {e['side']:4s} {e['tf']:3s} "
                f"SL={e['sl_pct']}% TGT={e['tgt_pct']}%  "
                f"PF={e['pf']:.2f}  WR={e['wr']:.1f}%  "
                f"Net=Rs{e['net_inr']:,.0f}  (n={e['n']})",
                file=sys.stderr,
            )
        print(file=sys.stderr)

    # Top 5 by net INR
    top5_inr = rankings["top_50_by_net_inr"][:5]
    if top5_inr:
        print(f"  TOP 5 BY NET INR:", file=sys.stderr)
        for i, e in enumerate(top5_inr, 1):
            print(
                f"    {i}. {e['symbol']:15s} {e['side']:4s} {e['tf']:3s} "
                f"SL={e['sl_pct']}% TGT={e['tgt_pct']}%  "
                f"PF={e['pf']:.2f}  WR={e['wr']:.1f}%  "
                f"Net=Rs{e['net_inr']:,.0f}  (n={e['n']})",
                file=sys.stderr,
            )
        print(file=sys.stderr)


if __name__ == "__main__":
    main()
