# REST rate meter + admin panel — 2026-05-26

Backup: `~/Desktop/fno-live-dashboard_backup_2026-05-26_07-00-50/`

## What changed

### New module: `rate_meter.py`
A lightweight in-memory `RateMeter` that records every Upstox / Fyers
HTTP call with timestamp, source, endpoint label, and HTTP status family.
Provides three rolling windows (60s / 5min / 1hr), cumulative totals,
last-call age per endpoint, and a memory cap (200k records) so it can't
grow unbounded.

URL classifier maps live URLs to short labels:
`ltp`, `quotes`, `chain`, `expiry`, `intraday_candle`, `daily_candle`,
`pcr`, `max_pain`, `change_oi`, `ws_authorize`, plus
`fyers_chain`, `fyers_history`, `fyers_quotes`.

### `ws_server.py`
- `DashboardServer` now owns a `self._rate_meter`.
- `_api_get` records every Upstox call (200 / 401 / 429 / other / timeout
  / exception). `populate_avg5d_vol` (which bypasses `_api_get`) records
  its `daily_candle` calls. Same for the dashboard candles fallback in
  `handle_api_candles`.
- `_fetch_chain_from_fyers` records `fyers_chain`. `_fetch_historical_candles_fyers`
  records `fyers_history`. `handle_admin_test_broker` records the test
  ping for both brokers.
- `handle_admin_status` returns a new `rest_calls` block containing the
  meter summary.

### `admin.html`
- New "API Rate Meter" card under Server Status with:
  - 4 headline counters: calls in last 60s / 5min / 1hr, plus 429s in
    last 60s.
  - Health badge (`HEALTHY` / `IDLE` / `429s SEEN`).
  - Per-(source, endpoint) table sorted by hourly volume, with last-call
    age beside each row.
- Refreshes on every `/api/admin/status` poll (every 15s, same loop as
  the rest of the admin page).
- One new CSS block (`.rm-table` and source-color helpers).

## Verified now

Six unit-style assertions in `_smoke_rate_meter.py` (run + deleted):
- URL classification matches every live URL pattern in the codebase.
- Counts per source/endpoint/status family are accurate after a burst
  of synthetic calls.
- Old records are correctly excluded from the 60s window but present
  in the 5min / 1hr windows.
- Failure paths (status 0 for exceptions, 5xx for upstream errors)
  appear in the right buckets.
- `last_call_age_seconds` reports a fresh age for the most recent call.
- Memory cap holds at 200k records under a 250k-call hammer test.

`py_compile` clean for all modified files. `admin.html` HTML structure
unchanged outside the new card (pre-existing img-self-close warnings are
not from this change).

## What you'll see at market open

`http://localhost:8080/admin` — scroll to "API Rate Meter". Within
seconds of the streamer starting and the first chain refresh you'll see
the table populate, ordered by hourly volume. Expectations from the
audit:

- `upstox / chain` ≈ 13/min averaged once `poll_chains` is in steady
  state.
- `fyers / fyers_history` ≈ 40/min averaged during the 5-min sync
  bursts.
- `upstox / quotes` should be 0 once the WS streamer is connected
  (the recent OHLC-poll gating change). If you see this climbing, the
  streamer is disconnected — cross-check `Upstox Streamer` status on
  the same admin page.
- `429s SEEN` badge red is the only thing that needs attention.

## Cumulative status by family

The `cumulative.by_status` map separates 200 / 400 / 500 / 0 (timeout
or exception). The admin UI surfaces 4xx in the 60s window because
that's where 429s show up. Not displayed in the table but available in
the raw `/api/admin/status` JSON for ad-hoc inspection.

## What this is NOT (yet)

- No enforcement. The meter only observes; it doesn't slow you down.
  Token-bucket rate limiting is the natural next step but I'm holding
  off until we have a session of real numbers from this meter to base
  the bucket sizes on.
- No persistence. Counters reset on restart. Fine for now; can move to
  a SQLite-backed counter later if we want week-over-week comparison.
- No per-symbol breakdown. We track endpoint families, not which stock
  caused the call. Easy to add if it ever matters.
