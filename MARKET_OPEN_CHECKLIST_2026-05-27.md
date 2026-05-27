# Market open readiness — 2026-05-27 (Wednesday)

Sanity check run at **07:33 IST**. Market opens at **09:15 IST** — about **1h 42m away**.

## ⚠️ Two blockers, must fix before 09:15

### 1. Upstox access token expired
- Token expired **03:30 IST today** (Upstox tokens always expire 03:30 IST every day).
- Server is showing repeated `Upstox WS auth failed: 401 Unauthorized` every 60 s.
- WebSocket streamer is paused; it will auto-reconnect once a fresh token is loaded.
- **What to do:**
  1. Go to <https://api.upstox.com/v2/login> (or your saved bookmark)
  2. Log in to your Upstox account, generate a fresh access token
  3. Paste the new token into `config.env` (`UPSTOX_ACCESS_TOKEN=eyJ...`)
  4. Restart `ws_server.py`

### 2. Server pinned to yesterday's monthly expiry (2026-05-26)
- The **monthly contract expired yesterday**. The new front-month is **2026-06-30** (208–211 stocks).
- The server caches `nearest_expiry` at boot. Yesterday's startup picked May 26 because it was still the nearest. Today's Upstox NSE.csv.gz only contains June 30 / July 28 / August 25 — no May 26.
- A simple token refresh **alone is not enough** — the server will keep asking Upstox for chain data on the May 26 expiry, which now returns empty.
- **Fix is just a restart:** `pkill -f ws_server.py && venv/bin/python3 ws_server.py &` will:
  - Re-download instruments → see June 30 as the front-month
  - Set `nearest_expiry = 2026-06-30`
  - Start fetching the new monthly chain from the first cycle
- **One known parser quirk:** the FUTSTK loader sorts expiries ascending and picks index 0 (front month). Today's file is clean (May 26 already removed). On future expiry days where NSE keeps the just-expired contract for one extra day, this could pick stale. Not an issue today; queued as a future hardening.

## What's ready already

| Check                                  | Status        |
|----------------------------------------|---------------|
| ws_server process running              | ✅ up since 00:33 |
| Auth proxy (port 8080) reachable       | ✅ 200 to /api/index-summary |
| Database schema v7                     | ✅ initialized |
| Active paper trades restored from DB   | ✅ 2 trades resumed |
| Auto paper trader scheduled            | ✅ scan every 300 s, max 2 trades/day |
| Top Picks V2 logic                     | ✅ live |
| Hot list filter button                 | ✅ live |
| Navbar revamp + icon pills             | ✅ live on every page |
| Nifty 50 page paused (splash + nav hide) | ✅ live |
| RSI scanner paused (no auto-scan)      | ✅ live |
| Bloomberg Pro theme + 11 others        | ✅ available in theme picker |
| Chart libs (dashboard + nifty)         | ✅ pinned to lightweight-charts@4.2.3 |
| `chain_snapshot` history (5d baseline) | ✅ 7 trading days in DB (May 19 → May 26) |

## After you restart, the warm-up timeline

This is what you should expect to see, minute by minute:

| Time after restart | What appears                                                                |
|--------------------|------------------------------------------------------------------------------|
| `T+0`              | Server boots, instruments downloaded, **199 stocks loaded**                 |
| `T+0 to +5 s`      | DataStore + DB initialized, paper trades loaded, auto-trader launched       |
| `T+5 s`            | Upstox WS streamer launches; LTP starts arriving via WebSocket              |
| `T+15 s`           | OHLC poller starts (skips while WS is connected — full feed covers it)      |
| `T+~30 s`          | Quotes batch first round → **LTP populated for all 199 stocks**             |
| `T+~30 s`          | `populate_avg5d_vol` starts (**this is the slow part**)                     |
| `T+~30s` to `T+~5min` | 199 sequential daily-candle calls × 0.5 s pacing = **~100 seconds** to fill `avg5d_vol`, `avg10d_vol`, `avg20d_vol`. Once filled, **Volume Surge column populates**. |
| `T+~5 min`         | First chain poll cycle starts (15-min interval)                              |
| `T+~5–8 min`       | All 199 chains fetched (Upstox-only, ~3 min cycle). **OI columns + IV + PCR + Max Pain populate.** |
| `T+~5 min`         | `oi-thesis` SQLite read on dashboard load shows yesterday's flagged set     |
| `T+~5–10 min`      | Top Picks V2 cards appear (needs `vol_surge` + `moneyness` both set, both populated by now) |
| `T+~5–10 min`      | Hot list count starts updating in the footer                                |
| **`T+~10 min`**    | **Full dashboard is "warm" — every column has live data.**                  |

If you restart at 08:00, the dashboard is fully warm by ~08:10 — **75 minutes before market open**. Plenty of slack.

## Recommended timing

- **Now (07:33)**: refresh Upstox token in `config.env`
- **Now (07:34)**: restart ws_server with `pkill -f ws_server.py && venv/bin/python3 ws_server.py &` (or ask me to run it)
- **By 07:50**: dashboard fully warm, Top Picks bar starts populating
- **08:00-09:14**: pre-open drift; chains and IV will be sparse since options markets don't open until 09:15
- **09:15:00**: ticks start. WS streamer pushes LTP changes immediately. First post-open chain refresh hits within the first 15-min cycle, so OI columns will catch up by 09:18-09:20
- **09:30**: by this time everything (vol surge, OI moneyness, PCR, max pain, Top Picks) is reflecting today's session

## Things to watch in the first 15 minutes after open

1. **Vol Surge column populating** — should be non-zero for most names by 09:25. Anything still showing `–` past 09:30 means `populate_avg5d_vol` failed for that symbol (rare, usually a Fyers fallback case).
2. **OI columns turning on** — first chain refresh post-open completes by ~09:18. If it's still at zero by 09:30, check the rate meter (`/api/admin/status`) for 429 spikes or `chain` failures.
3. **Top Picks bar — empty until both moneyness and surge converge.** Realistically populates by 09:20–09:30. Don't act on a card that flickers on and off in the first 5 minutes.
4. **Freshness pill** — should go green (sub-2 s) within 10 seconds of the first WS tick.

## What would block tomorrow's open completely?

Only one thing: if you try to use yesterday's stale token without restarting, the chain refresh + LTP poll will both fail silently. Symptoms: "Connecting" in the navbar status, no LTP movement, Vol Surge stays at –. **Fix: restart with a fresh token.**

## Next move

Tell me:
- The new Upstox token, OR
- "Go ahead and restart with the existing config" (if you've already pasted it into `config.env` yourself)

Either way I'll do the restart and watch the boot logs for clean startup.
