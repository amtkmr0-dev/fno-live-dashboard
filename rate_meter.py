#!/usr/bin/env python3
"""
rate_meter.py — Lightweight in-memory REST call counter.

Used by ws_server.py to track how many Upstox / Fyers HTTP calls we make
per minute, broken down by endpoint and status family. The goal is
visibility, not enforcement: pair this with /api/admin/status to know at
a glance whether we're approaching a published rate limit.

Design:
  - A bounded deque of (timestamp, label, status_family) records.
  - Old entries are evicted lazily on read so we never block the request
    path.
  - O(1) record(), O(window_seconds * record_rate) summarize().
  - No locking: aiohttp's single-threaded event loop runs all handlers,
    so concurrent record() / summary() can't race.

Public API:
  meter = RateMeter()
  meter.record("upstox", "ltp", 200)
  meter.record_failure("upstox", "chain", 429)
  meter.summary()  # → dict for /api/admin/status
"""

from __future__ import annotations

import time
from collections import deque, defaultdict


# Hard cap on retained records so memory stays bounded even if someone
# calls record() in a hot loop without ever reading summary().
_MAX_RECORDS = 200_000


class RateMeter:
    def __init__(self, retention_seconds: int = 3600):
        self.retention_seconds = retention_seconds
        # Each record: (ts: float, source: str, label: str, status_family: int)
        # status_family is the HTTP status rounded down to hundreds (200, 400,
        # 429, 500). 0 means "no response / exception".
        self._records: deque = deque(maxlen=_MAX_RECORDS)
        # Cumulative since process start — survives ring eviction.
        self._totals: defaultdict = defaultdict(int)         # (source, label) → count
        self._totals_status: defaultdict = defaultdict(int)  # (source, status_family) → count
        self._last_call: dict = {}                           # (source, label) → ts
        self._first_record_ts: float | None = None

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record(self, source: str, label: str, status: int = 200) -> None:
        """Record a single REST call.

        source: 'upstox' | 'fyers' | other free-form bucket
        label : endpoint family ('ltp', 'quotes', 'chain', 'expiry',
                'daily_candle', 'intraday_candle', 'fyers_history',
                'fyers_chain', 'fyers_quotes', ...)
        status: HTTP status code, or 0 if request never completed
        """
        now = time.time()
        family = (status // 100) * 100 if status else 0
        self._records.append((now, source, label, family))
        self._totals[(source, label)] += 1
        self._totals_status[(source, family)] += 1
        self._last_call[(source, label)] = now
        if self._first_record_ts is None:
            self._first_record_ts = now

    def record_failure(self, source: str, label: str, status: int = 0) -> None:
        """Convenience alias for non-200 outcomes (timeouts, exceptions)."""
        self.record(source, label, status=status)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def _evict_stale(self, now: float | None = None) -> None:
        if not self._records:
            return
        cutoff = (now or time.time()) - self.retention_seconds
        while self._records and self._records[0][0] < cutoff:
            self._records.popleft()

    def _count_within(self, window_seconds: int) -> tuple[dict, dict, int]:
        """Return ((src,label) → count, (src,family) → count, total) for the
        last ``window_seconds``."""
        now = time.time()
        cutoff = now - window_seconds
        by_label: defaultdict = defaultdict(int)
        by_status: defaultdict = defaultdict(int)
        total = 0
        # Records are appended in monotonic time order so we can stop early
        # walking right-to-left.
        for ts, source, label, family in reversed(self._records):
            if ts < cutoff:
                break
            by_label[(source, label)] += 1
            by_status[(source, family)] += 1
            total += 1
        return dict(by_label), dict(by_status), total

    def summary(self) -> dict:
        """JSON-friendly summary for /api/admin/status."""
        now = time.time()
        self._evict_stale(now)

        last_60_label, last_60_status, last_60_total = self._count_within(60)
        last_300_label, _, last_300_total = self._count_within(300)
        last_3600_label, _, last_3600_total = self._count_within(3600)

        # Group breakdowns by source for readability
        def _by_source(d: dict) -> dict:
            out: dict = {}
            for (src, label), v in d.items():
                out.setdefault(src, {})[label] = v
            return out

        # Last-call timestamps (relative ages)
        last_call_age: dict = {}
        for (src, label), ts in self._last_call.items():
            last_call_age.setdefault(src, {})[label] = round(now - ts, 1)

        # Cumulative totals
        cumulative: dict = {}
        for (src, label), v in self._totals.items():
            cumulative.setdefault(src, {})[label] = v

        cumulative_status: dict = {}
        for (src, family), v in self._totals_status.items():
            cumulative_status.setdefault(src, {})[str(family)] = v

        return {
            "window_seconds": self.retention_seconds,
            "last_60s": {
                "total": last_60_total,
                "by_endpoint": _by_source(last_60_label),
                "by_status": _by_source(last_60_status),
            },
            "last_300s": {
                "total": last_300_total,
                "by_endpoint": _by_source(last_300_label),
            },
            "last_3600s": {
                "total": last_3600_total,
                "by_endpoint": _by_source(last_3600_label),
            },
            "cumulative": {
                "by_endpoint": cumulative,
                "by_status": cumulative_status,
            },
            "last_call_age_seconds": last_call_age,
            "first_record_ts": self._first_record_ts,
        }


# ---------------------------------------------------------------------------
# URL → label classifier
# ---------------------------------------------------------------------------

def classify_upstox_url(url: str) -> str:
    """Map a Upstox REST URL to a short, stable label."""
    if not url:
        return "unknown"
    if "/v3/market-quote/ltp" in url:
        return "ltp"
    if "/v2/market-quote/quotes" in url:
        return "quotes"
    if "/v2/option/chain" in url:
        return "chain"
    if "/v2/option/contract" in url:
        return "expiry"
    if "/v2/historical-candle/intraday" in url or "/v3/historical-candle/intraday" in url:
        return "intraday_candle"
    if "/v2/historical-candle/" in url:
        return "daily_candle"
    if "/v2/market/pcr" in url:
        return "pcr"
    if "/v2/market/max-pain" in url:
        return "max_pain"
    if "/v2/market/change-oi" in url:
        return "change_oi"
    if "/v3/feed/market-data-feed/authorize" in url:
        return "ws_authorize"
    return "other"


def classify_fyers_url(url: str) -> str:
    if not url:
        return "unknown"
    if "/data/options-chain-v3" in url:
        return "fyers_chain"
    if "/data/history" in url:
        return "fyers_history"
    if "/data/quotes" in url:
        return "fyers_quotes"
    return "fyers_other"
