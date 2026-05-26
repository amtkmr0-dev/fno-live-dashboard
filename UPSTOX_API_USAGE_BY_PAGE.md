# Upstox API usage by page

A complete inventory of which user-facing page consumes which Upstox endpoint, at what cadence, and whether opening the page actually adds load (vs. piggybacking on a background task that runs whether the page is open or not).

**Bottom line:** Only **two** of the user pages add Upstox calls when opened. Everything else either reads from in-memory state populated by the WebSocket streamer or from SQLite snapshots.

---

## What's running in the background regardless of any open page

These cost the same whether 0 or 100 tabs are open. They're the floor.

| Background loop                              | Endpoint hit                | Frequency                  | Approx calls/min |
|----------------------------------------------|-----------------------------|----------------------------|------------------|
| Chain refresh (`poll_chains`)                | `/option/chain` (Upstox v2) | every 15 min × 196 symbols | ~13 / min        |
| LTP/OHLC quotes (`poll_quotes`)              | `/market-quote/quotes`      | batched every 30 s         | ~2 / min         |
| 5-day avg vol seed (`_startup_avg5d_vol`)    | `/historical-candle/.../day/...` | once at boot, then idle | ~0 / min steady  |
| Index ticker (`pollIndexSummary`)            | `/api/index-summary`        | reads `_index_state` only  | **0 (no Upstox)**|
| WebSocket streamer (Upstox WS)               | not REST                    | persistent connection      | **0 REST**       |

Total background: **~15 Upstox REST calls/min** baseline during market hours. Well within Upstox's 25 req/sec headroom (~1500/min). Comfortable.

---

## Per-page Upstox cost

For each user page below: poll cadence, endpoints touched, Upstox cost per minute attributable to that page being open.

### `dashboard_live.html` — Main dashboard

| Endpoint                  | Trigger                  | Backend                | Upstox/min |
|---------------------------|--------------------------|------------------------|------------|
| `/api/state`              | one-shot on connect      | `self.state` memory    | **0**      |
| `/api/index-summary`      | every 3 s                | `_index_state` memory  | **0**      |
| `/ws` (WebSocket)         | persistent               | server-side broadcast  | **0**      |
| `/api/candles` (analysis panel) | on click + 60 s cache | direct Upstox call    | **~1/min while panel open**, 0 when closed |
| `/api/stock/oi_timeseries`     | on analysis panel open | SQLite                 | **0**      |

**Cost while just looking at the dashboard table: 0/min.** Only the analysis side-panel adds load — and it has a 60 s client-side cache, so even with the panel pinned open you get at most 1 candles call/minute per symbol.

### `nifty.html` — NIFTY 50 page

| Endpoint                     | Trigger              | Backend             | Upstox/min |
|------------------------------|----------------------|---------------------|------------|
| `/api/nifty/data`            | every 2 s            | memory + SQLite     | **0**      |
| `/api/nifty/timeseries`      | every 10 s           | SQLite              | **0**      |
| `/api/candles?symbol=NIFTY`  | every 5 s            | direct Upstox call  | **12 / min** |
| `/api/nifty/multi-strike-oi` | one-shot per refresh | SQLite              | **0**      |
| `/api/nifty/chart.png`       | one-shot fallback    | Upstox (rare)       | **~0**     |

**Cost: ~12 Upstox calls/min while open** — all to `intraday_candle`. **The biggest per-page consumer in the app.**

The 5 s polling interval is overkill — 1-minute candles don't change at sub-30 s granularity. A 30 s server-side TTL cache + raising the page poll to 15 s would drop this to ~1-2/min without any user-visible change.

### `rsi.html` — RSI scanner

| Endpoint                  | Trigger                              | Upstox cost                                                                 |
|---------------------------|--------------------------------------|-----------------------------------------------------------------------------|
| `/api/candles` × 2        | manual scan or 500 ms after WS connect | ~196 symbols × 2 intervals (5m + 15m) = **~390 calls in a single burst**   |

**Cost: ~390 calls per scan, in batches of 5 parallel.**
- One-shot, not periodic.
- Auto-starts ~500 ms after the WebSocket sends the first stock list.
- Click "Scan RSI" again = another 390-call burst.

This is by far the page that *spikes* the rate meter. With the 1500/min headroom, a single scan eats ~26 % of a minute's budget; two back-to-back scans (~780 calls in ~30 s) would briefly graze the limit.

### `oi-thesis.html` — OI Thesis tracker

| Endpoint                  | Trigger              | Backend         | Upstox/min |
|---------------------------|----------------------|-----------------|------------|
| `/api/oi-thesis?days=30`  | one-shot on load     | SQLite          | **0**      |

**Cost: 0/min.** Reads only from the OI Thesis SQLite tables populated by the post-close cron.

### `oi-scanner.html` — OI Scanner

| Endpoint                | Trigger          | Backend          | Upstox/min |
|-------------------------|------------------|------------------|------------|
| `/api/oi-scanner`       | one-shot on load | memory + SQLite  | **0**      |

**Cost: 0/min.**

### `oi-timeseries.html` — Per-symbol OI history

| Endpoint                  | Trigger              | Backend  | Upstox/min |
|---------------------------|----------------------|----------|------------|
| `/api/state`              | one-shot for sym list | memory   | **0**      |
| `/api/stock/oi_timeseries` | per symbol selection | SQLite   | **0**      |

**Cost: 0/min.**

### `advanced-analytics.html` — Advanced Desk

| Endpoint               | Trigger                   | Backend         | Upstox/min |
|------------------------|---------------------------|-----------------|------------|
| `/api/advanced-chain`  | on click of "Pull Chain"  | memory          | **0**      |
| `/api/tv-script`       | on export                 | memory          | **0**      |

**Cost: 0/min.**

### `paper.html` — Paper trades

| Endpoint                    | Trigger          | Backend  | Upstox/min |
|-----------------------------|------------------|----------|------------|
| `/api/paper/trades`         | one-shot on load | SQLite   | **0**      |
| `/api/paper/auto-status`    | every 30 s       | memory   | **0**      |
| `/api/paper/auto-scan`      | manual button    | memory   | **0**      |

**Cost: 0/min.**

### `admin.html` — Admin console

| Endpoint                    | Trigger      | Backend            | Upstox/min                                     |
|-----------------------------|--------------|--------------------|------------------------------------------------|
| `/api/admin/status`         | every 15 s   | memory + meter     | **0**                                          |
| `/api/admin/test-broker`    | manual click | direct Upstox call | **1 per click** to `/market-quote/quotes`     |
| `/api/admin/logs`           | manual click | tail file          | **0**                                          |

**Cost: 0/min idle.** Each manual "Test Token" click adds 1.

### `sectors.html`, `settings.html`, `billing.html`, `profile.html`, `paper_trades.html`, `index.html`

| Endpoint                  | Trigger              | Backend         | Upstox/min |
|---------------------------|----------------------|-----------------|------------|
| Various                   | one-shot or 15 s+    | memory or DB    | **0**      |

**Cost: 0/min.**

---

## Summary table

| Page                      | Upstox/min | Notes                                          |
|---------------------------|-----------|-------------------------------------------------|
| dashboard_live.html       | ~1        | Only when analysis panel pinned open. Cached.   |
| **nifty.html**            | **~12**   | **Biggest steady consumer.**                    |
| **rsi.html**              | **0 idle / ~390 per scan** | Spike on demand.                  |
| oi-thesis.html            | 0         |                                                 |
| oi-scanner.html           | 0         |                                                 |
| oi-timeseries.html        | 0         |                                                 |
| advanced-analytics.html   | 0         |                                                 |
| paper.html                | 0         |                                                 |
| admin.html                | 0 idle / +1 per "Test Token" |                              |
| settings/billing/profile  | 0         |                                                 |

---

## Optimization priority (in order of leverage)

### 1. Add 30 s TTL cache on `/api/candles` ⭐ highest leverage
- Affects every page that fetches candles: `nifty.html`, `dashboard_live.html` analysis panel, `rsi.html` scan.
- 1-minute candles don't update at sub-30 s anyway.
- Drops nifty page from 12/min → 2/min.
- Drops a fresh RSI scan from 390 calls to whatever's not in cache (typically ~390 the first time, ~0 within 30 s of a previous scan).
- ~5 minutes of work, ~5 lines in `handle_api_candles`. Single revert.

### 2. Raise nifty.html chart poll from 5 s → 15 s
- After step 1 the cache is doing the heavy lifting, but raising the client interval also means fewer JS work cycles + fewer table redraws.
- Drops nifty page from ~2/min → ~1/min after cache.
- 1-line change.

### 3. Coalesce RSI scan with the dashboard analysis panel cache
- Both touch `/api/candles` for the same symbols at the same intervals.
- Sharing the cache means an RSI scan inherits 60 s of free hits from any symbol the user opened in the dashboard panel.
- Implementation: same TTL store as step 1, no extra code needed if step 1 is in place.

### 4. Snapshot /api/candles to SQLite intraday
- We already have a `data_recorder` for chain snapshots. Adding an intraday-candle snapshot every minute would make `/api/candles` SQLite-first and Upstox-only for the latest tail.
- Drops Upstox cost effectively to "fetch only the most recent 1-2 candles per request" regardless of how many pages are open.
- Bigger change — separate session.

---

## Where each metric comes from

The numbers above are from reading:
- Inline JS in every `*.html` for `setInterval` calls and `fetch('/api/...')` patterns.
- `ws_server.py` handlers for what each `/api/*` endpoint actually does (in-memory read, SQLite read, or Upstox call).
- The rate meter (`_rate_meter.record("upstox", label, ...)`) lines for the four Upstox-touching paths: `chain`, `daily_candle`, `intraday_candle`, `quotes`.

For live confirmation during market hours, `/api/admin/status` exposes the actual per-endpoint counts in real time on the admin page.

---

## Next move
Implement step 1 (`/api/candles` 30 s TTL cache). It benefits every page that fetches candles simultaneously, has a single revert path, and the headroom freed is enormous compared to the code change.
