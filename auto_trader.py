#!/usr/bin/env python3
"""
QUANTRA Auto-Trade Engine
=========================
Stages: NIFTY direction → Sector momentum → Stock scoring → Paper trade creation

This module is imported by auth_proxy.py and triggered via:
  - POST /api/admin/auto-trade/run
  - A periodic timer inside auth_proxy.py

It is NOT a standalone server. Use the AutoTrader class directly.

Usage:
    from auto_trader import AutoTrader
    trader = AutoTrader(db)  # db is a QuantraDB (DB) instance from db.py
    result = await trader.run_scan(
        user_id=1,
        candidates=[
            {
                "symbol": "RELIANCE",
                "direction": "CE",
                "strike": 2900,
                "entry_premium": 45.0,
                "lot_size": 250,
                "expiry": "2025-05-29",
            },
            ...
        ],
        nifty_direction="BULLISH",
        nifty_score=72,
    )
"""

import json
import logging
import math
import os
import time
from datetime import datetime, timezone, timedelta

log = logging.getLogger("quantra.auto_trader")

# ---------------------------------------------------------------------------
# Timezone
# ---------------------------------------------------------------------------

IST = timezone(timedelta(hours=5, minutes=30))

# ---------------------------------------------------------------------------
# NSE trading window
# ---------------------------------------------------------------------------

# NSE opens at 09:15 and closes at 15:30; we stop auto-trading at 15:15
# to avoid end-of-day volatility and ensure order-fill time.
_TRADING_START = (9, 15)   # HH, MM in IST
_TRADING_END   = (15, 15)  # HH, MM in IST

# ---------------------------------------------------------------------------
# Scoring windows (IST)
# ---------------------------------------------------------------------------

# Afternoon sweet-spot: 12:00 – 13:30 → premium is stable & direction confirmed
_AFTERNOON_START = (12, 0)
_AFTERNOON_END   = (13, 30)

# Post-afternoon: 13:30 onwards → still tradeable but thinner edge
_POST_AFTERNOON  = (13, 30)

# ---------------------------------------------------------------------------
# Trade constraints
# ---------------------------------------------------------------------------

MAX_AUTO_TRADES_PER_DAY   = 2     # hard cap across all auto-trades per user per day
MIN_PREMIUM_RS             = 15.0  # absolute floor — never trade below this
PREFERRED_MIN_PREMIUM_RS   = 30.0  # premium >= 30 earns a bonus point (liquid options)
PREFERRED_MAX_CAPITAL_RS   = 20_000.0  # capital <= 20 K earns a bonus point

# SL / target parameters
SL_PREMIUM_PCT   = 0.15   # SL at -15 % of entry premium
SL_SPOT_PCT      = 0.01   # Spot SL at -1 % of underlying spot (stored for reference)
T1_RR_MULTIPLE   = 2.0    # T1 = 2 : 1 risk-reward
T2_RR_MULTIPLE   = 3.0    # T2 = 3 : 1 risk-reward

# ---------------------------------------------------------------------------
# Blacklisted stocks — never trade these regardless of setup quality
# ---------------------------------------------------------------------------

BLACKLISTED_STOCKS = {
    "AARTIIND", "ABCAPITAL", "ABFRL", "ACC", "ALKEM", "AMBUJACEM",
    "APOLLOHOSP", "ASTRAL", "AUBANK", "AUROPHARMA", "BANDHANBNK",
    "BATAINDIA", "BEL", "BHARATFORG", "BIOCON", "CANFINHOME",
    "CHAMBLFERT", "COFORGE", "CONCOR", "CROMPTON", "CUB",
    "CUMMINSIND", "DALBHARAT", "DEEPAKNTR", "DELTACORP", "ESCORTS",
    "EXIDEIND", "FEDERALBNK", "GMRINFRA", "GODREJCP", "GODREJPROP",
    "GRANULES", "GUJGASLTD", "HINDPETRO", "IDFCFIRSTB", "IEX",
    "INDHOTEL", "INDUSTOWER", "IRCTC", "JUBLFOOD", "LAURUSLABS",
    "LICHSGFIN", "LUPIN", "MANAPPURAM", "METROPOLIS", "MFSL",
    "MGL", "MOTHERSON", "MUTHOOTFIN", "NAM-INDIA", "NATIONALUM",
    "NAVINFLUOR", "NAUKRI", "NMDC", "OBEROIRLTY", "PAGEIND",
    "PERSISTENT", "PETRONET", "PFC", "PIIND", "PNB",
    "POLYCAB", "RBLBANK", "RECLTD", "SAIL", "SBICARD",
    "SYNGENE", "UBL",
}

# ---------------------------------------------------------------------------
# Whitelisted stocks — preferred universe; non-whitelisted candidates are
# accepted but receive no whitelist bonus (scoring is neutral, not penalised)
# ---------------------------------------------------------------------------

WHITELISTED_STOCKS = {
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK",
    "HINDUNILVR", "ITC", "SBIN", "BHARTIARTL", "KOTAKBANK",
    "LT", "AXISBANK", "ASIANPAINT", "MARUTI", "SUNPHARMA",
    "TATAMOTORS", "BAJFINANCE", "BAJAJFINSV", "WIPRO", "HCLTECH",
    "NESTLEIND", "TITAN", "ULTRACEMCO", "NTPC", "POWERGRID",
    "TECHM", "ONGC", "JSWSTEEL", "TATASTEEL", "ADANIENT",
    "ADANIPORTS", "COALINDIA", "GRASIM", "DIVISLAB", "DRREDDY",
    "CIPLA", "EICHERMOT", "HEROMOTOCO", "BAJAJ-AUTO", "M&M",
    "BPCL", "INDUSINDBK", "SBILIFE", "HDFCLIFE", "ICICIPRULI",
    "TATACONSUM", "DABUR", "PIDILITIND",
}

# ---------------------------------------------------------------------------
# Lot sizes for common NSE F&O symbols (fallback table)
# Real lot sizes are resolved dynamically; these are used when the caller
# does not supply lot_size and no API data is available.
# ---------------------------------------------------------------------------

DEFAULT_LOT_SIZES = {
    "NIFTY":       65,
    "BANKNIFTY":   15,
    "FINNIFTY":    40,
    "RELIANCE":   250,
    "TCS":        175,
    "HDFCBANK":   550,
    "INFY":       300,
    "ICICIBANK":  700,
    "SBIN":       750,
    "KOTAKBANK":  400,
    "AXISBANK":  1200,
    "LT":         150,
    "WIPRO":      300,
    "BAJFINANCE": 125,
    "TATAMOTORS": 550,
    "MARUTI":      75,
    "HINDUNILVR": 300,
    "BHARTIARTL": 950,
    "ITC":       1600,
    "NTPC":      4000,
    "POWERGRID": 3000,
    "ONGC":      1925,
    "BPCL":      1800,
    "COALINDIA": 4200,
    "ADANIENT":   400,
    "ADANIPORTS": 550,
    "TITAN":      375,
    "NESTLEIND":   50,
    "ULTRACEMCO": 100,
    "JSWSTEEL":   675,
    "TATASTEEL": 5500,
    "GRASIM":     375,
    "HCLTECH":    350,
    "TECHM":      600,
    "SUNPHARMA":  350,
    "CIPLA":      650,
    "DRREDDY":    125,
    "DIVISLAB":   150,
    "BAJAJFINSV": 125,
    "M&M":        350,
    "EICHERMOT":  100,
    "HEROMOTOCO": 150,
    "INDUSINDBK": 600,
    "SBILIFE":    750,
    "HDFCLIFE":   800,
    "ICICIPRULI": 750,
    "TATACONSUM": 800,
    "DABUR":     1250,
    "ASIANPAINT": 200,
    "PIDILITIND": 250,
}


# ===========================================================================
# AutoTrader
# ===========================================================================

class AutoTrader:
    """
    Auto-trade engine for the QUANTRA Terminal platform.

    Orchestrates a five-stage pipeline:
      1. NIFTY direction assessment (BULLISH / BEARISH / NEUTRAL)
      2. Sector momentum filter (pass-through in v1; reserved for extension)
      3. Composite V3 candidate scoring
      4. Paper trade creation in the SQLite database
      5. Signal logging via db.log_auto_signal()

    Parameters
    ----------
    db : DB
        A fully-initialised QuantraDB (``DB``) instance from db.py.
        The caller is responsible for calling ``db.init()`` before passing it.
    """

    def __init__(self, db):
        """
        Initialise the AutoTrader.

        Parameters
        ----------
        db : DB
            QuantraDB instance from db.py — used for all database operations.
        """
        self.db = db
        log.debug("AutoTrader initialised")

    # ------------------------------------------------------------------
    # Stage helpers — public so they can be unit-tested individually
    # ------------------------------------------------------------------

    def is_trading_window(self) -> bool:
        """
        Check whether the current wall-clock time (IST) falls within the
        NSE trading window used for auto-trading: 09:15 – 15:15, Mon–Fri.

        Weekends (Saturday = 5, Sunday = 6 in Python's weekday()) are
        excluded. Indian market holidays are NOT checked here; that would
        require a holiday calendar which is outside scope for v1.

        Returns
        -------
        bool
            True if auto-trading is permissible right now.
        """
        now_ist = datetime.now(IST)
        if now_ist.weekday() >= 5:  # Saturday or Sunday
            log.debug("is_trading_window: weekend — market closed")
            return False

        hhmm = (now_ist.hour, now_ist.minute)
        in_window = _TRADING_START <= hhmm < _TRADING_END
        log.debug(
            "is_trading_window: %02d:%02d IST → %s",
            now_ist.hour, now_ist.minute,
            "OPEN" if in_window else "CLOSED",
        )
        return in_window

    def assess_nifty_direction(
        self,
        nifty_direction: str = None,
        nifty_score: float = None,
    ) -> dict:
        """
        Stage 1 — Determine market direction.

        In v1 the caller may supply ``nifty_direction`` and ``nifty_score``
        directly (e.g. derived from a dashboard WebSocket feed or an earlier
        API call).  When not supplied the engine defaults to NEUTRAL / 50.

        Scoring semantics (reserved for live implementation):
          - price > VWAP and price > 9-EMA  → BULLISH  (score 60–100)
          - price < VWAP and price < 9-EMA  → BEARISH  (score 0–40)
          - otherwise                        → NEUTRAL  (score 41–59)

        Parameters
        ----------
        nifty_direction : str, optional
            Override string: ``"BULLISH"``, ``"BEARISH"``, or ``"NEUTRAL"``.
            Case-insensitive; invalid values fall back to ``"NEUTRAL"``.
        nifty_score : float, optional
            A 0–100 confidence score. When ``nifty_direction`` is provided
            without a score, a sensible default is inferred.

        Returns
        -------
        dict
            ``{"direction": str, "score": float, "source": str}``
        """
        VALID_DIRECTIONS = {"BULLISH", "BEARISH", "NEUTRAL"}

        if nifty_direction:
            direction = nifty_direction.upper().strip()
            if direction not in VALID_DIRECTIONS:
                log.warning(
                    "assess_nifty_direction: unknown direction %r — defaulting to NEUTRAL",
                    nifty_direction,
                )
                direction = "NEUTRAL"
        else:
            direction = "NEUTRAL"

        # Infer a default score when not supplied
        if nifty_score is None:
            default_scores = {"BULLISH": 70, "BEARISH": 30, "NEUTRAL": 50}
            score = float(default_scores[direction])
            source = "default"
        else:
            score = float(max(0, min(100, nifty_score)))
            source = "caller_supplied"

        log.info(
            "Stage 1 — NIFTY direction: %s (score=%.1f, source=%s)",
            direction, score, source,
        )
        return {"direction": direction, "score": score, "source": source}

    def filter_sector_momentum(self, candidates: list) -> list:
        """
        Stage 2 — Sector momentum filter.

        In v1 this is a pass-through: every candidate that reaches this stage
        is forwarded to scoring.  Blacklist and minimum-premium checks are
        deliberately deferred to Stage 3 so that the reason for exclusion is
        clearly attributed.

        Future versions will:
          - Group candidates by GICS sector
          - Compute sector-level RSI / price-rate-of-change
          - Penalise or exclude candidates from weak/diverging sectors

        Parameters
        ----------
        candidates : list[dict]
            Each dict must contain at least ``symbol`` (str).

        Returns
        -------
        list[dict]
            The same list (pass-through in v1), potentially reordered or
            enriched with a ``sector_momentum`` key in future versions.
        """
        if not candidates:
            log.debug("Stage 2 — no candidates to filter")
            return []

        log.info(
            "Stage 2 — Sector momentum filter (pass-through v1): %d candidates",
            len(candidates),
        )
        # Enrich each candidate with a placeholder momentum value so that
        # downstream code can rely on the key's existence.
        enriched = []
        for c in candidates:
            enriched.append({**c, "sector_momentum": None})
        return enriched

    def score_candidate(
        self,
        symbol: str,
        entry_premium: float,
        sl_premium: float,
        lots: int,
        lot_size: int,
    ) -> dict:
        """
        Stage 3 — Composite V3 scoring for a single option candidate.

        Scoring rubric
        --------------
        Time window (IST):
          +3  if 12:00 ≤ now < 13:30 (afternoon sweet-spot)
          +1  if now ≥ 13:30          (post-afternoon, still tradeable)

        Risk % (= (entry - SL) / entry × 100):
          +2  if risk% ≤ 0.35 %
          +1  if risk% ≤ 0.50 %   (only one tier awarded)

        Capital deployed (= entry × lots × lot_size):
          +1  if capital ≤ Rs 20,000

        Premium liquidity:
          +1  if entry_premium ≥ Rs 30  (liquid; tight spread expected)
          NEVER award a point for low premium (Rs 15–29 range is neutral)

        Parameters
        ----------
        symbol : str
            Ticker symbol, e.g. ``"RELIANCE"``.
        entry_premium : float
            ATM / selected strike option LTP (Rs per share).
        sl_premium : float
            Stop-loss premium (Rs per share).
        lots : int
            Number of lots to trade.
        lot_size : int
            Contract lot size for this symbol.

        Returns
        -------
        dict
            ``{"symbol": str, "score": int, "breakdown": dict,
               "capital": float, "risk_pct": float}``
        """
        breakdown = {}
        score = 0

        # --- Time-window bonus ---
        now_ist = datetime.now(IST)
        hhmm = (now_ist.hour, now_ist.minute)
        if _AFTERNOON_START <= hhmm < _AFTERNOON_END:
            breakdown["time_window"] = 3
            score += 3
        elif hhmm >= _POST_AFTERNOON:
            breakdown["time_window"] = 1
            score += 1
        else:
            breakdown["time_window"] = 0

        # --- Risk % bonus ---
        try:
            risk_pct = abs(entry_premium - sl_premium) / entry_premium * 100
        except ZeroDivisionError:
            risk_pct = 999.0  # entry_premium == 0 → degenerate case, no bonus

        if risk_pct <= 0.35:
            breakdown["risk_pct_bonus"] = 2
            score += 2
        elif risk_pct <= 0.50:
            breakdown["risk_pct_bonus"] = 1
            score += 1
        else:
            breakdown["risk_pct_bonus"] = 0
        breakdown["risk_pct"] = round(risk_pct, 4)

        # --- Capital bonus ---
        capital = entry_premium * lots * lot_size
        if capital <= PREFERRED_MAX_CAPITAL_RS:
            breakdown["capital_bonus"] = 1
            score += 1
        else:
            breakdown["capital_bonus"] = 0
        breakdown["capital"] = round(capital, 2)

        # --- Premium liquidity bonus ---
        # Positive only when premium is at or above the "liquid" threshold.
        # Never award a point for low-premium trades.
        if entry_premium >= PREFERRED_MIN_PREMIUM_RS:
            breakdown["premium_bonus"] = 1
            score += 1
        else:
            breakdown["premium_bonus"] = 0

        log.debug(
            "score_candidate %s: score=%d breakdown=%s",
            symbol, score, breakdown,
        )
        return {
            "symbol": symbol,
            "score": score,
            "breakdown": breakdown,
            "capital": round(capital, 2),
            "risk_pct": round(risk_pct, 4),
        }

    def calculate_levels(
        self,
        entry_premium: float,
        direction: str,
        spot_price: float = None,
    ) -> dict:
        """
        Calculate SL, T1, and T2 price levels for a trade.

        Stop-loss (hybrid):
          - Premium SL : entry × (1 − SL_PREMIUM_PCT)  i.e. entry − 15 %
          - Spot SL    : stored as a reference value when spot_price is
                         provided (spot × (1 − SL_SPOT_PCT))

        Targets (based on risk from entry to premium SL):
          - T1 : entry + risk × T1_RR_MULTIPLE  (2 : 1 RR)
          - T2 : entry + risk × T2_RR_MULTIPLE  (3 : 1 RR)

        Both CE and PE calls use the same arithmetic since premiums always
        rise when the trade is in our favour regardless of direction.

        Parameters
        ----------
        entry_premium : float
            Option LTP at entry (Rs per share).
        direction : str
            ``"CE"`` or ``"PE"`` — informational only; levels are symmetric.
        spot_price : float, optional
            Underlying spot price; used to populate ``sl_spot``.

        Returns
        -------
        dict
            ``{"sl_premium": float, "sl_spot": float|None,
               "t1_premium": float, "t2_premium": float,
               "risk_per_share": float}``
        """
        if entry_premium <= 0:
            log.warning("calculate_levels: entry_premium=%s is invalid", entry_premium)
            return {
                "sl_premium": None,
                "sl_spot": None,
                "t1_premium": None,
                "t2_premium": None,
                "risk_per_share": None,
            }

        sl_premium = round(entry_premium * (1 - SL_PREMIUM_PCT), 2)
        risk = entry_premium - sl_premium  # positive value

        t1_premium = round(entry_premium + risk * T1_RR_MULTIPLE, 2)
        t2_premium = round(entry_premium + risk * T2_RR_MULTIPLE, 2)

        sl_spot = None
        if spot_price and spot_price > 0:
            sl_spot = round(spot_price * (1 - SL_SPOT_PCT), 2)

        log.debug(
            "calculate_levels %s: entry=%.2f sl=%.2f t1=%.2f t2=%.2f (risk=%.2f)",
            direction, entry_premium, sl_premium, t1_premium, t2_premium, risk,
        )
        return {
            "sl_premium":   sl_premium,
            "sl_spot":      sl_spot,
            "t1_premium":   t1_premium,
            "t2_premium":   t2_premium,
            "risk_per_share": round(risk, 2),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_user_settings(self, user_id: int) -> dict:
        """
        Retrieve user settings from the database with safe defaults.

        Returns a dict guaranteed to contain the keys used by the engine.
        If the database returns None (settings row absent), defaults are used.

        Parameters
        ----------
        user_id : int
            The platform user ID.

        Returns
        -------
        dict
        """
        settings = self.db.get_user_settings(user_id) or {}
        return {
            "auto_trade_enabled":      bool(settings.get("auto_trade_enabled", 0)),
            "auto_trade_max_positions": int(settings.get("auto_trade_max_positions", 2)),
            "auto_trade_max_capital":  float(settings.get("auto_trade_max_capital", 50_000.0)),
        }

    def _count_auto_trades_today(self, user_id: int) -> int:
        """
        Return the number of auto trades already created today for user_id.

        Delegates to db.count_user_trades_today with trade_type='auto'.

        Parameters
        ----------
        user_id : int

        Returns
        -------
        int
        """
        try:
            return self.db.count_user_trades_today(user_id, trade_type="auto")
        except Exception as exc:
            log.error("_count_auto_trades_today failed: %s", exc, exc_info=True)
            # Fail safe: treat as if limit is reached to avoid uncontrolled trading
            return MAX_AUTO_TRADES_PER_DAY

    def _validate_candidate(
        self,
        candidate: dict,
        max_capital: float,
    ) -> tuple:
        """
        Validate a single candidate dict and return (is_valid, reason).

        Checks performed:
          - Symbol is present and not in BLACKLISTED_STOCKS
          - direction is CE or PE
          - entry_premium is present and ≥ MIN_PREMIUM_RS
          - lot_size is resolvable (candidate key or DEFAULT_LOT_SIZES)
          - Capital deployed does not exceed user's max_capital setting

        Parameters
        ----------
        candidate : dict
            Candidate dict from the caller.
        max_capital : float
            User's ``auto_trade_max_capital`` setting.

        Returns
        -------
        tuple[bool, str]
            ``(True, "")`` if valid; ``(False, reason_string)`` otherwise.
        """
        symbol = str(candidate.get("symbol", "")).upper().strip()
        if not symbol:
            return False, "Missing symbol"

        if symbol in BLACKLISTED_STOCKS:
            return False, f"{symbol} is blacklisted"

        direction = str(candidate.get("direction", "")).upper().strip()
        if direction not in ("CE", "PE"):
            return False, f"Invalid direction '{direction}' — must be CE or PE"

        entry_premium = candidate.get("entry_premium")
        if entry_premium is None:
            return False, "Missing entry_premium"
        try:
            entry_premium = float(entry_premium)
        except (TypeError, ValueError):
            return False, "entry_premium is not a number"
        if entry_premium < MIN_PREMIUM_RS:
            return False, (
                f"entry_premium Rs {entry_premium:.2f} < minimum Rs {MIN_PREMIUM_RS:.2f}"
            )

        lots = int(candidate.get("lots", 1))
        lot_size = candidate.get("lot_size")
        if lot_size is None:
            lot_size = DEFAULT_LOT_SIZES.get(symbol)
        if not lot_size:
            return False, f"Cannot determine lot_size for {symbol}"
        lot_size = int(lot_size)

        capital = entry_premium * lots * lot_size
        if capital > max_capital:
            return False, (
                f"Capital Rs {capital:,.0f} exceeds user limit Rs {max_capital:,.0f}"
            )

        return True, ""

    def _resolve_lot_size(self, candidate: dict) -> int:
        """
        Resolve the lot size for a candidate.

        Preference order:
          1. ``lot_size`` key in candidate dict
          2. DEFAULT_LOT_SIZES table
          3. Returns 1 as a last-resort fallback (should never happen in prod)

        Parameters
        ----------
        candidate : dict

        Returns
        -------
        int
        """
        ls = candidate.get("lot_size")
        if ls:
            return int(ls)
        symbol = str(candidate.get("symbol", "")).upper().strip()
        return DEFAULT_LOT_SIZES.get(symbol, 1)

    # ------------------------------------------------------------------
    # Main pipeline
    # ------------------------------------------------------------------

    def run_scan(
        self,
        user_id: int,
        candidates: list = None,
        nifty_direction: str = None,
        nifty_score: float = None,
        force: bool = False,
    ) -> dict:
        """
        Execute the full auto-trade pipeline for a single user.

        Pipeline stages
        ---------------
        0. Pre-flight checks: trading hours, user settings, daily limit
        1. NIFTY direction assessment
        2. Sector momentum filter (pass-through v1)
        3. Composite V3 scoring → rank → take top 2
        4. Paper trade creation (DB insert, one per selected candidate)
        5. Signal logging (auto_signals table)

        Parameters
        ----------
        user_id : int
            Target user for whom trades will be created.
        candidates : list[dict], optional
            Each dict should contain:
              - symbol       (str, required)
              - direction    (str, "CE" or "PE", required)
              - strike       (float, required)
              - entry_premium (float, required)
              - lot_size     (int, optional — resolved from DEFAULT_LOT_SIZES)
              - lots         (int, optional — defaults to 1)
              - expiry       (str, optional — ISO date "YYYY-MM-DD")
              - spot_price   (float, optional — used for spot-SL calculation)
            If None or empty, the scan returns immediately with
            skip_reason="No candidates supplied".
        nifty_direction : str, optional
            Override for Stage 1: "BULLISH", "BEARISH", or "NEUTRAL".
        nifty_score : float, optional
            Override for Stage 1: 0–100 confidence score.

        Returns
        -------
        dict
            ``{
                "success":        bool,
                "nifty_direction": str,
                "nifty_score":    float,
                "candidates_found": int,
                "trades_entered": list[dict],
                "skip_reason":    str | None,
                "signal_id":      int | None,
                "scan_time_ms":   int,
            }``
        """
        t0 = time.monotonic()

        def _result(
            success: bool,
            direction: str,
            score_val: float,
            candidates_found: int,
            trades: list,
            skip_reason: str,
            signal_id,
        ) -> dict:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            return {
                "success":          success,
                "nifty_direction":  direction,
                "nifty_score":      score_val,
                "candidates_found": candidates_found,
                "trades_entered":   trades,
                "skip_reason":      skip_reason,
                "signal_id":        signal_id,
                "scan_time_ms":     elapsed_ms,
            }

        log.info("=== AutoTrader.run_scan START (user_id=%d) ===", user_id)

        # ----------------------------------------------------------------
        # Stage 0a — Trading window check
        # ----------------------------------------------------------------
        if not force and not self.is_trading_window():
            log.info("run_scan: outside trading hours — aborting")
            return _result(
                success=False,
                direction="NEUTRAL",
                score_val=50.0,
                candidates_found=0,
                trades=[],
                skip_reason="Outside trading hours",
                signal_id=None,
            )

        # ----------------------------------------------------------------
        # Stage 0b — User settings check
        # ----------------------------------------------------------------
        try:
            settings = self._get_user_settings(user_id)
        except Exception as exc:
            log.error("run_scan: failed to load user settings: %s", exc, exc_info=True)
            return _result(
                success=False,
                direction="NEUTRAL",
                score_val=50.0,
                candidates_found=0,
                trades=[],
                skip_reason=f"Failed to load user settings: {exc}",
                signal_id=None,
            )

        if not settings["auto_trade_enabled"]:
            log.info("run_scan: auto-trade disabled for user %d", user_id)
            return _result(
                success=False,
                direction="NEUTRAL",
                score_val=50.0,
                candidates_found=0,
                trades=[],
                skip_reason="Auto-trade disabled",
                signal_id=None,
            )

        max_capital = settings["auto_trade_max_capital"]

        # ----------------------------------------------------------------
        # Stage 0c — Daily limit check
        # ----------------------------------------------------------------
        trades_today = self._count_auto_trades_today(user_id)
        if trades_today >= MAX_AUTO_TRADES_PER_DAY:
            log.info(
                "run_scan: daily limit reached for user %d (%d trades today)",
                user_id, trades_today,
            )
            return _result(
                success=False,
                direction="NEUTRAL",
                score_val=50.0,
                candidates_found=0,
                trades=[],
                skip_reason=f"Daily limit reached ({trades_today}/{MAX_AUTO_TRADES_PER_DAY})",
                signal_id=None,
            )

        remaining_slots = MAX_AUTO_TRADES_PER_DAY - trades_today
        log.info(
            "run_scan: user %d has %d/%d auto-trade slots remaining today",
            user_id, remaining_slots, MAX_AUTO_TRADES_PER_DAY,
        )

        # ----------------------------------------------------------------
        # Stage 0d — Candidates check
        # ----------------------------------------------------------------
        if not candidates:
            log.info("run_scan: no candidates supplied for user %d", user_id)
            return _result(
                success=False,
                direction="NEUTRAL",
                score_val=50.0,
                candidates_found=0,
                trades=[],
                skip_reason="No candidates supplied",
                signal_id=None,
            )

        # ----------------------------------------------------------------
        # Stage 1 — NIFTY direction
        # ----------------------------------------------------------------
        nifty_info = self.assess_nifty_direction(nifty_direction, nifty_score)
        market_direction = nifty_info["direction"]
        market_score     = nifty_info["score"]

        # ----------------------------------------------------------------
        # Stage 2 — Sector momentum filter (pass-through v1)
        # ----------------------------------------------------------------
        filtered_candidates = self.filter_sector_momentum(candidates)
        log.info("Stage 2 — %d candidates after sector filter", len(filtered_candidates))

        # ----------------------------------------------------------------
        # Stage 3 — Validate, score, rank, select top N
        # ----------------------------------------------------------------
        scored = []
        skipped_reasons = {}

        for raw in filtered_candidates:
            sym = str(raw.get("symbol", "")).upper().strip()
            try:
                is_valid, reason = self._validate_candidate(raw, max_capital)
                if not is_valid:
                    log.info("Stage 3 — skipping %s: %s", sym, reason)
                    skipped_reasons[sym] = reason
                    continue

                entry_premium = float(raw["entry_premium"])
                lots          = int(raw.get("lots", 1))
                lot_size      = self._resolve_lot_size(raw)
                sl_levels     = self.calculate_levels(
                    entry_premium,
                    str(raw.get("direction", "CE")).upper(),
                    spot_price=raw.get("spot_price"),
                )
                sl_premium    = sl_levels["sl_premium"] or (entry_premium * (1 - SL_PREMIUM_PCT))

                score_result = self.score_candidate(
                    symbol        = sym,
                    entry_premium = entry_premium,
                    sl_premium    = sl_premium,
                    lots          = lots,
                    lot_size      = lot_size,
                )

                scored.append({
                    **raw,
                    "symbol":       sym,
                    "lots":         lots,
                    "lot_size":     lot_size,
                    "entry_premium": entry_premium,
                    "sl_premium":   sl_levels["sl_premium"],
                    "sl_spot":      sl_levels["sl_spot"],
                    "t1_premium":   sl_levels["t1_premium"],
                    "t2_premium":   sl_levels["t2_premium"],
                    "risk_per_share": sl_levels["risk_per_share"],
                    "composite_score": score_result["score"],
                    "score_breakdown": score_result["breakdown"],
                    "capital":      score_result["capital"],
                    "risk_pct":     score_result["risk_pct"],
                })

            except Exception as exc:
                log.error(
                    "Stage 3 — error processing candidate %s: %s",
                    sym, exc, exc_info=True,
                )
                skipped_reasons[sym] = f"Processing error: {exc}"

        if not scored:
            log.info("run_scan: no valid candidates after scoring for user %d", user_id)

            # Log the scan even when no trades were placed
            signal_id = self._log_scan_signal(
                user_id         = user_id,
                market_direction = market_direction,
                market_score    = market_score,
                candidates      = filtered_candidates,
                trades          = [],
                action_taken    = "no_candidates",
                skipped_reasons = skipped_reasons,
            )
            return _result(
                success=False,
                direction=market_direction,
                score_val=market_score,
                candidates_found=0,
                trades=[],
                skip_reason="No valid candidates after scoring and validation",
                signal_id=signal_id,
            )

        # Sort descending by composite score; secondary sort by premium (higher = more liquid)
        scored.sort(
            key=lambda c: (c["composite_score"], c["entry_premium"]),
            reverse=True,
        )

        # Honour both the configured positions limit and today's remaining slots
        max_new = min(
            settings["auto_trade_max_positions"],
            remaining_slots,
            MAX_AUTO_TRADES_PER_DAY,  # global cap
        )
        selected = scored[:max_new]

        log.info(
            "Stage 3 — %d candidates scored; selecting top %d (limit=%d)",
            len(scored), len(selected), max_new,
        )

        # ----------------------------------------------------------------
        # Stage 4 — Create paper trades
        # ----------------------------------------------------------------
        trades_entered = []

        for candidate in selected:
            sym = candidate["symbol"]
            try:
                # Double-check daily limit (re-query in case of race condition)
                current_count = self._count_auto_trades_today(user_id)
                if current_count >= MAX_AUTO_TRADES_PER_DAY:
                    log.warning(
                        "Stage 4 — daily limit hit mid-loop for user %d; stopping",
                        user_id,
                    )
                    break

                direction     = str(candidate.get("direction", "CE")).upper()
                entry_premium = candidate["entry_premium"]
                strike        = candidate.get("strike")
                expiry        = candidate.get("expiry")
                lots          = candidate["lots"]
                lot_size      = candidate["lot_size"]
                sl_premium    = candidate["sl_premium"]
                sl_spot       = candidate.get("sl_spot")
                t1_premium    = candidate["t1_premium"]
                t2_premium    = candidate["t2_premium"]
                capital       = candidate["capital"]

                # Build a human-readable entry reason
                entry_reason = (
                    f"AutoTrader v1 | NIFTY={market_direction}({market_score:.0f}) | "
                    f"Score={candidate['composite_score']} | "
                    f"Capital=Rs{capital:,.0f} | "
                    f"Risk={candidate['risk_pct']:.3f}%"
                )

                trade_id = self.db.create_paper_trade(
                    user_id       = user_id,
                    symbol        = sym,
                    direction     = direction,
                    trade_type    = "auto",
                    status        = "PENDING",
                    strike        = strike,
                    expiry        = expiry,
                    entry_premium = entry_premium,
                    lots          = lots,
                    lot_size      = lot_size,
                    sl_premium    = sl_premium,
                    sl_spot       = sl_spot,
                    t1_premium    = t1_premium,
                    t2_premium    = t2_premium,
                    entry_reason  = entry_reason,
                )

                trade_summary = {
                    "trade_id":      trade_id,
                    "symbol":        sym,
                    "direction":     direction,
                    "strike":        strike,
                    "expiry":        expiry,
                    "entry_premium": entry_premium,
                    "sl_premium":    sl_premium,
                    "sl_spot":       sl_spot,
                    "t1_premium":    t1_premium,
                    "t2_premium":    t2_premium,
                    "lots":          lots,
                    "lot_size":      lot_size,
                    "capital":       capital,
                    "composite_score": candidate["composite_score"],
                }
                trades_entered.append(trade_summary)

                log.info(
                    "Stage 4 — trade created: id=%d %s %s@%.2f "
                    "SL=%.2f T1=%.2f T2=%.2f",
                    trade_id, sym, direction, entry_premium,
                    sl_premium, t1_premium, t2_premium,
                )

            except Exception as exc:
                log.error(
                    "Stage 4 — failed to create trade for %s: %s",
                    sym, exc, exc_info=True,
                )
                # Continue to next candidate; do not abort the whole scan
                skipped_reasons[sym] = f"Trade creation failed: {exc}"

        # ----------------------------------------------------------------
        # Stage 5 — Log the signal
        # ----------------------------------------------------------------
        action_taken = "traded" if trades_entered else "no_trade"
        signal_id = self._log_scan_signal(
            user_id          = user_id,
            market_direction = market_direction,
            market_score     = market_score,
            candidates       = filtered_candidates,
            trades           = trades_entered,
            action_taken     = action_taken,
            skipped_reasons  = skipped_reasons,
        )

        # Backfill signal_id into the paper trades we just created
        if signal_id:
            for t in trades_entered:
                try:
                    self.db.conn.execute(
                        "UPDATE paper_trades SET auto_signal_id = ? WHERE id = ?",
                        (signal_id, t["trade_id"]),
                    )
                except Exception as exc:
                    log.warning(
                        "Could not backfill signal_id=%d into trade %d: %s",
                        signal_id, t["trade_id"], exc,
                    )
            try:
                self.db.conn.commit()
            except Exception as exc:
                log.warning("Commit for signal_id backfill failed: %s", exc)

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        success = len(trades_entered) > 0

        log.info(
            "=== AutoTrader.run_scan END (user_id=%d) — %d trade(s) in %d ms ===",
            user_id, len(trades_entered), elapsed_ms,
        )

        return {
            "success":          success,
            "nifty_direction":  market_direction,
            "nifty_score":      market_score,
            "candidates_found": len(scored),
            "trades_entered":   trades_entered,
            "skip_reason":      None if success else "No trades entered",
            "signal_id":        signal_id,
            "scan_time_ms":     elapsed_ms,
        }

    # ------------------------------------------------------------------
    # Internal — signal logging
    # ------------------------------------------------------------------

    def _log_scan_signal(
        self,
        user_id: int,
        market_direction: str,
        market_score: float,
        candidates: list,
        trades: list,
        action_taken: str,
        skipped_reasons: dict,
    ):  # -> Optional[int]
        """
        Persist a summary of the scan run to the auto_signals table.

        A single row is inserted capturing the aggregate result.  If
        individual trades were created their IDs are included in the
        analysis_json blob.

        Parameters
        ----------
        user_id : int
        market_direction : str
        market_score : float
        candidates : list[dict]
        trades : list[dict]
        action_taken : str
            One of: ``"traded"``, ``"no_candidates"``, ``"no_trade"``.
        skipped_reasons : dict
            symbol → reason string for candidates that were rejected.

        Returns
        -------
        int | None
            The newly inserted auto_signals row ID, or None on failure.
        """
        try:
            analysis_data = {
                "candidates_evaluated": len(candidates),
                "candidates_skipped":   skipped_reasons,
                "trades_created":       [t["trade_id"] for t in trades],
                "symbols_traded":       [t["symbol"]   for t in trades],
                "market_direction":     market_direction,
                "market_score":         market_score,
                "scan_timestamp_ist":   datetime.now(IST).isoformat(),
            }

            summary_parts = [
                f"NIFTY={market_direction}({market_score:.0f})",
                f"candidates={len(candidates)}",
                f"traded={len(trades)}",
            ]
            if trades:
                summary_parts.append(
                    "symbols=" + ",".join(t["symbol"] for t in trades)
                )
            if skipped_reasons:
                summary_parts.append(f"skipped={len(skipped_reasons)}")
            analysis_summary = " | ".join(summary_parts)

            # Determine confidence as a normalised version of the top trade score
            confidence = None
            if trades:
                top_score = max(t.get("composite_score", 0) for t in trades)
                # Max possible score is 3 (time) + 2 (risk) + 1 (capital) + 1 (premium) = 7
                confidence = round(min(top_score / 7.0 * 100, 100), 1)

            signal_id = self.db.log_auto_signal(
                user_id          = user_id,
                nifty_direction  = market_direction,
                nifty_score      = market_score,
                analysis_summary = analysis_summary,
                analysis_data    = analysis_data,
                confidence       = confidence,
                action_taken     = action_taken,
                trade_id         = trades[0]["trade_id"] if trades else None,
            )
            log.info(
                "Stage 5 — signal logged: id=%s action=%s confidence=%s",
                signal_id, action_taken, confidence,
            )
            return signal_id

        except Exception as exc:
            log.error(
                "_log_scan_signal failed (non-fatal): %s", exc, exc_info=True
            )
            return None


# ===========================================================================
# LiveDataBridge — Fetches real Upstox data with rate-limit protection
# ===========================================================================

# ---------------------------------------------------------------------------
# Rate-limit / throttle constants
# ---------------------------------------------------------------------------
# Upstox v2 documented limits: 25 requests/second, varies by endpoint.
# We stay well under by throttling to ~4 req/sec (250 ms gap between calls).

_THROTTLE_MS           = 250    # minimum gap between consecutive API calls (ms)
_RETRY_MAX             = 3      # max retries on 429 / 5xx
_RETRY_BASE_WAIT_S     = 2.0   # first retry waits 2 s, then 4 s, then 8 s
_CACHE_TTL_NIFTY_S     = 120   # cache NIFTY direction result for 2 minutes
_CACHE_TTL_CHAIN_S     = 180   # cache per-stock chain data for 3 minutes
_CIRCUIT_BREAKER_429   = 5     # after 5 consecutive 429s, stop scanning for this cycle
_CIRCUIT_BREAKER_COOL  = 60    # cool-down seconds after circuit breaker trips


class LiveDataBridge:
    """
    Async bridge between the auto-trade engine and Upstox API v2.

    Built-in protections against Upstox rate limits:
      - Per-call throttle: minimum 250 ms between consecutive HTTP requests
      - Retry with exponential backoff on HTTP 429 / 5xx (up to 3 retries)
      - Response caching: NIFTY direction cached 2 min, stock chains cached 3 min
      - Circuit breaker: 5 consecutive 429s → stop scanning, cool down 60 s
      - Respects Retry-After header from 429 responses

    Reuses functions from chat_analysis.py for instrument resolution.
    Designed to be called from auth_proxy.py's periodic timer.

    Usage (inside an async context):
        bridge = LiveDataBridge()
        nifty_dir, nifty_score = await bridge.fetch_nifty_direction()
        candidates = await bridge.scan_whitelisted_stocks(nifty_dir)
    """

    def __init__(self):
        self._token = None
        self._token_checked_at = 0
        # Throttle state
        self._last_request_at = 0.0          # monotonic timestamp of last API call
        # Response cache: key → (timestamp, data)
        self._cache = {}
        # Circuit breaker
        self._consecutive_429s = 0
        self._circuit_open_until = 0.0       # monotonic time when circuit breaker resets

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    def _get_token(self):
        """Read Upstox token (reuses chat_analysis.get_upstox_token)."""
        now = time.time()
        if self._token and (now - self._token_checked_at) < 300:
            return self._token
        try:
            from chat_analysis import get_upstox_token
            self._token = get_upstox_token()
        except ImportError:
            log.warning("LiveDataBridge: chat_analysis.py not available")
            self._token = os.environ.get("UPSTOX_ACCESS_TOKEN", "")
        self._token_checked_at = now
        return self._token

    def _headers(self, token):
        return {
            "Accept": "application/json",
            "Api-Version": "2.0",
            "Authorization": f"Bearer {token}",
        }

    # ------------------------------------------------------------------
    # Rate-limit helpers
    # ------------------------------------------------------------------

    async def _throttle(self):
        """Enforce minimum gap between API calls."""
        import asyncio
        now = time.monotonic()
        elapsed_ms = (now - self._last_request_at) * 1000
        if elapsed_ms < _THROTTLE_MS:
            wait_s = (_THROTTLE_MS - elapsed_ms) / 1000.0
            await asyncio.sleep(wait_s)
        self._last_request_at = time.monotonic()

    def _circuit_is_open(self) -> bool:
        """Return True if circuit breaker is tripped (too many 429s)."""
        if self._consecutive_429s >= _CIRCUIT_BREAKER_429:
            if time.monotonic() < self._circuit_open_until:
                return True
            # Cool-down expired — reset
            self._consecutive_429s = 0
        return False

    def _record_429(self):
        """Record a 429 response; trip circuit breaker if threshold reached."""
        self._consecutive_429s += 1
        if self._consecutive_429s >= _CIRCUIT_BREAKER_429:
            self._circuit_open_until = time.monotonic() + _CIRCUIT_BREAKER_COOL
            log.warning(
                "LiveDataBridge: circuit breaker TRIPPED — %d consecutive 429s, "
                "cooling down %d s",
                self._consecutive_429s, _CIRCUIT_BREAKER_COOL,
            )

    def _record_success(self):
        """Reset consecutive 429 counter on a successful response."""
        self._consecutive_429s = 0

    def _cache_get(self, key, ttl_s):
        """Return cached value if fresh, else None."""
        entry = self._cache.get(key)
        if entry and (time.monotonic() - entry[0]) < ttl_s:
            log.debug("LiveDataBridge: cache HIT for %s", key)
            return entry[1]
        return None

    def _cache_set(self, key, value):
        """Store value in cache with current timestamp."""
        self._cache[key] = (time.monotonic(), value)

    async def _api_get(self, session, url, headers, params, timeout_s=15):
        """
        Rate-limited GET with retry on 429/5xx and circuit breaker.

        Returns (status_code, json_data) or (error_code, None) on failure.
        Special return codes:
          -1  = circuit breaker open
          -2  = all retries exhausted
          401 = token expired (no retry)
        """
        import aiohttp
        import asyncio

        if self._circuit_is_open():
            log.debug("LiveDataBridge: circuit breaker OPEN — skipping request to %s", url)
            return -1, None

        for attempt in range(_RETRY_MAX):
            await self._throttle()

            try:
                async with session.get(
                    url, headers=headers, params=params,
                    timeout=aiohttp.ClientTimeout(total=timeout_s),
                ) as resp:
                    status = resp.status

                    if status == 200:
                        self._record_success()
                        data = await resp.json()
                        return 200, data

                    if status == 401:
                        log.error("LiveDataBridge: Upstox token expired (401)")
                        return 401, None

                    if status == 429:
                        self._record_429()
                        # Respect Retry-After header if present
                        retry_after = resp.headers.get("Retry-After")
                        if retry_after:
                            try:
                                wait_s = float(retry_after)
                            except ValueError:
                                wait_s = _RETRY_BASE_WAIT_S * (2 ** attempt)
                        else:
                            wait_s = _RETRY_BASE_WAIT_S * (2 ** attempt)
                        log.warning(
                            "LiveDataBridge: 429 rate limited (attempt %d/%d), "
                            "waiting %.1f s before retry",
                            attempt + 1, _RETRY_MAX, wait_s,
                        )
                        await asyncio.sleep(wait_s)
                        continue

                    if status >= 500:
                        wait_s = _RETRY_BASE_WAIT_S * (2 ** attempt)
                        log.warning(
                            "LiveDataBridge: server error %d (attempt %d/%d), "
                            "waiting %.1f s",
                            status, attempt + 1, _RETRY_MAX, wait_s,
                        )
                        await asyncio.sleep(wait_s)
                        continue

                    # Other 4xx — don't retry
                    log.debug("LiveDataBridge: HTTP %d for %s — not retrying", status, url)
                    return status, None

            except asyncio.TimeoutError:
                wait_s = _RETRY_BASE_WAIT_S * (2 ** attempt)
                log.warning(
                    "LiveDataBridge: timeout (attempt %d/%d), waiting %.1f s",
                    attempt + 1, _RETRY_MAX, wait_s,
                )
                await asyncio.sleep(wait_s)
                continue
            except Exception as exc:
                log.error("LiveDataBridge: request error: %s", exc)
                return -2, None

        log.warning("LiveDataBridge: all %d retries exhausted for %s", _RETRY_MAX, url)
        return -2, None

    # ------------------------------------------------------------------
    # NIFTY direction
    # ------------------------------------------------------------------

    async def fetch_nifty_direction(self):
        """
        Determine NIFTY direction from Upstox live option chain data.
        Cached for 2 minutes to avoid redundant API calls.

        Returns (direction: str, score: float)
        """
        import aiohttp

        # Check cache first
        cached = self._cache_get("nifty_direction", _CACHE_TTL_NIFTY_S)
        if cached:
            return cached

        token = self._get_token()
        if not token:
            log.warning("LiveDataBridge: no Upstox token — defaulting NEUTRAL")
            return "NEUTRAL", 50.0

        headers = self._headers(token)
        nifty_key = "NSE_INDEX|Nifty 50"

        try:
            async with aiohttp.ClientSession() as session:
                # 1) Get nearest expiry
                contract_url = "https://api.upstox.com/v2/option/contract"
                status, resp_json = await self._api_get(
                    session, contract_url, headers,
                    params={"instrument_key": nifty_key},
                )
                if status != 200 or not resp_json:
                    return "NEUTRAL", 50.0

                contracts = resp_json.get("data", [])
                if not contracts:
                    return "NEUTRAL", 50.0

                expiries = sorted(set(c.get("expiry") or c for c in contracts if c))
                if not expiries:
                    return "NEUTRAL", 50.0
                expiry = expiries[0]

                # 2) Fetch chain data
                chain_url = "https://api.upstox.com/v2/option/chain"
                status, resp_json = await self._api_get(
                    session, chain_url, headers,
                    params={"instrument_key": nifty_key, "expiry_date": expiry},
                    timeout_s=20,
                )
                if status != 200 or not resp_json:
                    return "NEUTRAL", 50.0

                data = resp_json.get("data", [])
                if not data:
                    return "NEUTRAL", 50.0

                # Parse chain — compute PCR, OI buildup direction
                spot = data[0].get("underlying_spot_price", 0)
                total_ce_oi = 0
                total_pe_oi = 0
                ce_oi_chg_sum = 0
                pe_oi_chg_sum = 0

                for r in data:
                    ce = r.get("call_options", {}).get("market_data", {})
                    pe = r.get("put_options", {}).get("market_data", {})
                    ce_oi = ce.get("oi", 0) or 0
                    pe_oi = pe.get("oi", 0) or 0
                    ce_prev_oi = ce.get("prev_oi", 0) or 0
                    pe_prev_oi = pe.get("prev_oi", 0) or 0
                    total_ce_oi += ce_oi
                    total_pe_oi += pe_oi
                    ce_oi_chg_sum += (ce_oi - ce_prev_oi)
                    pe_oi_chg_sum += (pe_oi - pe_prev_oi)

                pcr = total_pe_oi / total_ce_oi if total_ce_oi > 0 else 1.0

                # Direction scoring
                score = 50.0
                if pcr > 1.3:
                    score += 20
                elif pcr > 1.1:
                    score += 10
                elif pcr < 0.7:
                    score -= 20
                elif pcr < 0.9:
                    score -= 10

                # OI change signal
                if pe_oi_chg_sum > ce_oi_chg_sum * 1.5:
                    score += 10
                elif ce_oi_chg_sum > pe_oi_chg_sum * 1.5:
                    score -= 10

                score = max(0, min(100, score))

                if score >= 60:
                    direction = "BULLISH"
                elif score <= 40:
                    direction = "BEARISH"
                else:
                    direction = "NEUTRAL"

                log.info(
                    "LiveDataBridge NIFTY: spot=%.2f pcr=%.3f "
                    "ce_oi_chg=%d pe_oi_chg=%d → %s (%.0f)",
                    spot, pcr, ce_oi_chg_sum, pe_oi_chg_sum, direction, score,
                )

                # Cache the result
                result = (direction, score)
                self._cache_set("nifty_direction", result)
                return result

        except Exception as exc:
            log.error(
                "LiveDataBridge.fetch_nifty_direction failed: %s",
                exc, exc_info=True,
            )
            return "NEUTRAL", 50.0

    # ------------------------------------------------------------------
    # Whitelisted stock scan
    # ------------------------------------------------------------------

    async def scan_whitelisted_stocks(self, nifty_direction, max_stocks=10):
        """
        Scan whitelisted stocks via Upstox option chains with rate-limit
        protection. Each stock chain fetch is throttled and cached.

        Returns a list of candidate dicts ready for AutoTrader.run_scan().
        """
        import aiohttp

        token = self._get_token()
        if not token:
            log.warning("LiveDataBridge: no token — cannot scan stocks")
            return []

        headers = self._headers(token)
        candidates = []

        opt_type = "PE" if nifty_direction == "BEARISH" else "CE"

        # Resolve instruments (cached for the day inside chat_analysis)
        try:
            from chat_analysis import _load_instruments
        except ImportError:
            log.warning("LiveDataBridge: chat_analysis not importable")
            return []

        try:
            async with aiohttp.ClientSession() as session:
                instruments = await _load_instruments(session)

                scan_symbols = [
                    sym for sym in WHITELISTED_STOCKS
                    if sym in instruments
                    and instruments[sym].get("lot_size", 0) > 0
                ][:max_stocks]

                log.info(
                    "LiveDataBridge: scanning %d stocks for %s setups "
                    "(throttle=%d ms, cache_ttl=%d s)",
                    len(scan_symbols), opt_type,
                    _THROTTLE_MS, _CACHE_TTL_CHAIN_S,
                )

                scanned = 0
                skipped_429 = 0

                for sym in scan_symbols:
                    # Check circuit breaker before each stock
                    if self._circuit_is_open():
                        log.warning(
                            "LiveDataBridge: circuit breaker open — "
                            "stopping after %d/%d stocks",
                            scanned, len(scan_symbols),
                        )
                        break

                    try:
                        cand = await self._fetch_stock_candidate(
                            session, headers, sym, opt_type, instruments,
                        )
                        if cand:
                            candidates.append(cand)
                        scanned += 1
                    except _RateLimitedError:
                        skipped_429 += 1
                        log.debug("LiveDataBridge: %s skipped (rate limited)", sym)
                        continue
                    except Exception as exc:
                        log.debug("LiveDataBridge: error scanning %s: %s", sym, exc)
                        scanned += 1
                        continue

        except Exception as exc:
            log.error(
                "LiveDataBridge.scan_whitelisted_stocks failed: %s",
                exc, exc_info=True,
            )

        log.info(
            "LiveDataBridge: %d candidates from %d scanned "
            "(%d rate-limited skips, circuit_breaker=%s)",
            len(candidates),
            scanned if 'scanned' in dir() else 0,
            skipped_429 if 'skipped_429' in dir() else 0,
            "OPEN" if self._circuit_is_open() else "closed",
        )
        return candidates

    async def _fetch_stock_candidate(
        self, session, headers, symbol, opt_type, instruments,
    ):
        """
        Fetch option chain for a single stock (with cache + rate-limit).
        Returns a candidate dict or None.
        Raises _RateLimitedError if circuit breaker or 429 blocks us.
        """
        # Check cache first
        cache_key = f"chain:{symbol}:{opt_type}"
        cached = self._cache_get(cache_key, _CACHE_TTL_CHAIN_S)
        if cached is not None:
            return cached  # may be None (= "no valid candidate", also cached)

        info = instruments.get(symbol, {})
        lot_size = info.get("lot_size", 0)
        nearest_expiry = info.get("nearest_expiry", "")

        if not lot_size or not nearest_expiry:
            self._cache_set(cache_key, None)
            return None

        eq_key = info.get("equity_key", "")
        if not eq_key:
            self._cache_set(cache_key, None)
            return None

        fo_key = eq_key.replace("NSE_EQ|", "NSE_FO|")

        chain_url = "https://api.upstox.com/v2/option/chain"
        status, resp_json = await self._api_get(
            session, chain_url, headers,
            params={"instrument_key": fo_key, "expiry_date": nearest_expiry},
        )

        if status == -1:
            raise _RateLimitedError("circuit breaker open")
        if status != 200 or not resp_json:
            self._cache_set(cache_key, None)
            return None

        data = resp_json.get("data", [])
        if not data:
            self._cache_set(cache_key, None)
            return None

        spot = data[0].get("underlying_spot_price", 0)
        if spot <= 0:
            self._cache_set(cache_key, None)
            return None

        # Find ATM strike
        atm_strike = None
        atm_premium = 0
        min_dist = float("inf")

        for r in data:
            strike = r.get("strike_price", 0)
            dist = abs(strike - spot)
            if dist < min_dist:
                min_dist = dist
                atm_strike = strike
                if opt_type == "CE":
                    md = r.get("call_options", {}).get("market_data", {})
                else:
                    md = r.get("put_options", {}).get("market_data", {})
                atm_premium = md.get("ltp", 0) or 0

        if not atm_strike or atm_premium < MIN_PREMIUM_RS:
            self._cache_set(cache_key, None)
            return None

        capital = atm_premium * lot_size
        if capital > 50_000:
            self._cache_set(cache_key, None)
            return None

        result = {
            "symbol": symbol,
            "direction": opt_type,
            "strike": atm_strike,
            "entry_premium": round(atm_premium, 2),
            "lots": 1,
            "lot_size": lot_size,
            "expiry": nearest_expiry,
            "spot_price": round(spot, 2),
        }
        self._cache_set(cache_key, result)
        return result


class _RateLimitedError(Exception):
    """Internal signal that a request was blocked by rate-limit protection."""
    pass


# ===========================================================================
# Quick smoke-test (run as a script for local development only)
# ===========================================================================

if __name__ == "__main__":
    import sys
    import os

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    # Allow running from any directory by resolving db.py relative to this file
    _here = os.path.dirname(os.path.abspath(__file__))
    if _here not in sys.path:
        sys.path.insert(0, _here)

    from db import DB

    _db_path = os.path.join(_here, "_auto_trader_test.db")
    print(f"Using test DB: {_db_path}")
    db = DB(_db_path)
    db.init()

    # Create a test user with auto-trade enabled
    try:
        uid = db.create_user("autotest", "autotest@example.com", "testpass123")
    except ValueError:
        uid = db.get_user_by_username("autotest")["id"]

    db.update_user_settings(uid, auto_trade_enabled=1, auto_trade_max_capital=200_000)

    trader = AutoTrader(db)

    # Test is_trading_window
    print(f"\nis_trading_window() = {trader.is_trading_window()}")

    # Test assess_nifty_direction
    d = trader.assess_nifty_direction("BULLISH", 75)
    print(f"\nassess_nifty_direction: {d}")

    # Test score_candidate
    s = trader.score_candidate("RELIANCE", 48.5, 41.2, 1, 250)
    print(f"\nscore_candidate RELIANCE: {s}")

    # Test calculate_levels
    lvl = trader.calculate_levels(48.5, "CE", spot_price=2900.0)
    print(f"\ncalculate_levels: {lvl}")

    # Test run_scan with sample candidates
    sample_candidates = [
        {
            "symbol":        "RELIANCE",
            "direction":     "CE",
            "strike":        2900,
            "entry_premium": 48.5,
            "lots":          1,
            "lot_size":      250,
            "expiry":        "2025-05-29",
            "spot_price":    2885.0,
        },
        {
            "symbol":        "HDFCBANK",
            "direction":     "CE",
            "strike":        1750,
            "entry_premium": 32.0,
            "lots":          1,
            "lot_size":      550,
            "expiry":        "2025-05-29",
            "spot_price":    1742.0,
        },
        {
            "symbol":        "AARTIIND",   # blacklisted — should be skipped
            "direction":     "PE",
            "strike":        500,
            "entry_premium": 25.0,
            "lots":          1,
            "lot_size":      500,
        },
        {
            "symbol":        "SBIN",
            "direction":     "CE",
            "strike":        800,
            "entry_premium": 12.0,   # below MIN_PREMIUM_RS — should be skipped
            "lots":          1,
            "lot_size":      750,
        },
    ]

    result = trader.run_scan(
        user_id          = uid,
        candidates       = sample_candidates,
        nifty_direction  = "BULLISH",
        nifty_score      = 72,
    )

    print("\n=== run_scan result ===")
    print(json.dumps(result, indent=2, default=str))

    # Clean up test DB
    db.close()
    os.remove(_db_path)
    print("\nTest DB cleaned up. Done.")
