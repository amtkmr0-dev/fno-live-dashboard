# Market Open Pre-Flight — Tuesday 26 May 2026, 09:15 IST

Three changes were shipped overnight. This list confirms each works.
Tick boxes as you go.

Backups (rollback if anything misbehaves):
- Pre-hardening:  `~/Desktop/fno-live-dashboard_backup_2026-05-26_06-14-58/`
- Pre-WS-speed:   `~/Desktop/fno-live-dashboard_backup_2026-05-26_06-43-13/`
- Pre-rate-meter: `~/Desktop/fno-live-dashboard_backup_2026-05-26_07-00-50/`

---

## Pre-9:15 — Restart and sanity-check (do this NOW)

### 1. Stop both processes if they're already running
```
pkill -f auth_proxy.py
pkill -f ws_server.py
sleep 2
```
- [ ] Done

### 2. Start ws_server in one terminal
```
cd ~/Desktop/FNO\ Dashboard/fno-live-dashboard
source venv/bin/activate
python3 ws_server.py
```
**Expected log lines (look for these):**
- `Loaded N vars from .../config.env` — config loaded
- `Loaded 196 F&O stocks` (or similar) — universe parsed
- `Subscribed to N instruments in 'full' mode via Upstox WS v3` — **critical: must say `'full'`**
- Bind line at the bottom; should bind to `127.0.0.1` (loopback). If it says `0.0.0.0`, your `WS_BIND_HOST` is overridden somewhere; fine for solo-on-laptop, but flag it.

- [ ] ws_server started cleanly
- [ ] Streamer subscribed in `'full'` mode

### 3. Start auth_proxy in a second terminal
```
cd ~/Desktop/FNO\ Dashboard/fno-live-dashboard
source venv/bin/activate
python3 auth_proxy.py
```
**Expected log lines:**
- `Database ready: N user(s)`
- `Internal auth (proxy↔ws_server): signed via INTERNAL_AUTH_SECRET` — confirms cross-process auth is wired
- `Security: rate limiting, brute force guard, CSRF, security headers — ALL ACTIVE`
- `Auth proxy started on :8080, backend at 127.0.0.1:8081`

If you see `INTERNAL_AUTH_SECRET not set — generated runtime secret …`, that means the proxy and ws_server have **different** secrets and the proxy will fail to authenticate to ws_server. Stop, check `config.env` has the `INTERNAL_AUTH_SECRET` line, restart both.

- [ ] auth_proxy started cleanly
- [ ] Internal auth secret line present (NOT the runtime-generated warning)

### 4. Hit `/api/admin/status` directly to confirm the rate meter block is present
```
curl -s -b /tmp/quantra_cookies http://localhost:8080/api/admin/status | python3 -m json.tool | head -50
```
(You'll need a session cookie — easier to just open the admin page in the browser.)

Open: **http://localhost:8080/admin** — log in if needed.

- [ ] Admin page loads
- [ ] "API Rate Meter" card visible between "Server Status" and "Settings"
- [ ] Badge says **IDLE** (no calls yet — or might say HEALTHY if startup chain refresh has begun)
- [ ] Headline counters show numbers (probably small: 0-5 in the first minute)

### 5. Open the dashboard
Open: **http://localhost:8080/**

- [ ] Top bar shows yellow `waiting` pill (no ticks yet) **OR** red `Ns stale` pill (we're outside market hours so no live data is flowing). Both are correct pre-market states.
- [ ] Connection dot is green
- [ ] Stocks render with prev-close values

---

## At 9:15 — Watch the open

### 6. The pill flips green within ~10 seconds of NSE going live
- [ ] Pill turns green, label shows "live", soft blink animation
- If it stays yellow/red after 30 seconds: the WS streamer isn't getting ticks. Check ws_server log for `Upstox WS connect failed`. Fall back: edit `config.env`, set `WS_FEED_MODE=ltpc`, restart both.

### 7. The rate meter starts populating
Refresh the admin page; the table at the bottom of the rate-meter card should show rows.

**Expected within the first 5 minutes (steady state):**

| source | endpoint        | /60s   | /5min  | notes                                      |
| ------ | --------------- | ------ | ------ | ------------------------------------------ |
| upstox | chain           | 0-15   | 30-100 | climbs as poll_chains cycles               |
| upstox | quotes          | 0      | 0      | OHLC polling is gated; should stay zero    |
| upstox | ltp             | 0      | 0      | LTP polling is gated; should stay zero     |
| upstox | daily_candle    | bursts at startup, then 0 | one-shot for avg5d_vol |
| fyers  | fyers_history   | 0-40   | 100-200| every 5 min sync burst                     |

- [ ] `upstox/quotes` is 0 (proves the OHLC poll gate is working)
- [ ] `upstox/ltp` is 0 (proves the LTP poll gate is working)
- [ ] `upstox/chain` rises but doesn't spike past 25/sec
- [ ] **No `429s SEEN` red badge**

### 8. Dashboard staleness pill stays mostly green
- [ ] Pill is green most of the time
- [ ] Yellow `Ns` flashes are OK on quiet symbols (between-tick gaps)
- [ ] Pill stays green or yellow; never red for >5 seconds during active market

### 9. Sample one stock by eye against your broker terminal
Pick one liquid stock you have on Fyers / Zerodha. Compare LTP for 30 seconds.
- [ ] Quantra LTP tracks within ~1 second of broker terminal
- [ ] OHLC values match (open, high, low)
- [ ] Volume column ticks up

---

## Failure escape hatches

### If WS doesn't deliver ticks at all (rate meter shows nothing, pill stays yellow)
```
# Edit config.env
WS_FEED_MODE=ltpc
```
Restart ws_server. You're back to yesterday's behavior. Loses OHLC+volume freshness via WS but the REST poll falls back automatically because it's gated on `streamer.connected` and the streamer will be in `ltpc` mode anyway.

### If you see `429s SEEN` in red on the rate meter
Look at which endpoint family. Likely culprits and fixes:
- `chain` 429s → bump pacing: `CHAIN_PACING=0.6` and `CHAIN_CONCURRENCY=1` in config.env, restart.
- `daily_candle` 429s → only happens at startup; ignore unless persistent.
- `fyers_history` 429s → reduce sync frequency: there's no env knob for this yet, so just live with it for today and we'll add one.

### If the dashboard staleness pill is red but rate meter shows healthy chain calls
The WS streamer is the problem, not the REST path. Restart ws_server only:
```
pkill -f ws_server.py
python3 ws_server.py
```
auth_proxy can stay up; the proxy will reconnect.

### If everything looks worse than yesterday
Restore the most recent backup:
```
rsync -a --delete \
  ~/Desktop/fno-live-dashboard_backup_2026-05-26_07-00-50/ \
  ~/Desktop/FNO\ Dashboard/fno-live-dashboard/
```
Restart both processes. You're back to yesterday's state.

---

## What to capture during the 30-minute observation window

So we can decide on the next changes (adaptive chains, OI fast-tier, theme, branding) with real data:

- [ ] Screenshot of the API Rate Meter card after 30 min — shows what the actual budget usage looks like.
- [ ] Note any time the staleness pill went red and for how long.
- [ ] If you can spare 10 seconds: open `/api/admin/status` JSON and copy the `rest_calls.cumulative` block — that's the cleanest record for me to reason against later.
- [ ] One-liner verdict: "feels faster", "feels the same", "something is off".

---

## Once everything is green

After the 30 min observation:
1. Stop watching. The system is fine.
2. Mid-day, when you're free, ping me: I'll start the theme/branding work and we'll discuss the OI fast-tier design with the rate-meter data in hand.
3. Adaptive chain refresh comes after themes (it's a backend-only change, doesn't block any UI work).

Good luck with the open.
