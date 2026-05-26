# Three fixes — 2026-05-26 12:51 IST

Backup: `~/Desktop/fno-live-dashboard_backup_2026-05-26_12-49-39/` (1.0 GB)

## What changed

### Fix 1: Chain refresh routes 100% through Upstox
`_fetch_chain_for_stock` no longer splits A-M to Fyers and N-Z to Upstox.
The 50/50 split was causing ~73 chain failures per cycle because Fyers
was rate-limiting half the calls. Now everything goes Upstox.

### Fix 2: Fyers historical sync stands down when WS is healthy
`_fyers_historical_sync_loop` skips its 199-stock cycle whenever the
Upstox WS streamer is connected in `full` / `full_d5` / `full_d30` mode.
Pacing also bumped from 0.3s → 1.0s per call when it does run.

This eliminates 95+ Fyers 429s per cycle (the load we observed before
this fix), and keeps Fyers as a true backup instead of an active
load source.

### Fix 3: Volume baselines wait for token, don't race
`_startup_avg5d_vol` previously slept a fixed 10 seconds and then called
`populate_avg5d_vol`. If the Upstox token wasn't valid in those 10s
(common on cold start), the function silently logged
"Skipping volume baselines fetch — no valid token" and the entire
session ran with `avg5d_vol = 0`, killing the surge column.

Now it waits on `_get_token_event()` (up to 5 minutes), then runs the
baseline fetch with a working token. After this, volume surge populates
within 90 seconds of WS connection.

## Verification at 12:51-13:08 IST

- ✅ Subscribed in `'full'` mode immediately at boot
- ✅ Volume baselines populated: 195/199 stocks
- ✅ Surge ratios live and climbing through the day (0.26-0.78x sample)
- ✅ OI populated for 198/199 stocks
- ✅ Pre-existing TATAMOTORS empty-state issue unchanged (not a regression)

## What to watch for tomorrow

1. **Chain cycle elapsed time.** Without Fyers in the mix it should be
   ~80-90 seconds. Today's first cycle was muddied by a network blip
   at 13:02. Tomorrow at fresh open will be the clean baseline.
2. **Fyers 429 count.** Should be near zero on the rate meter once
   WS is connected (the historical sync gate kicks in).
3. **TATAMOTORS** — not fixed by this pass. After-market diagnosis:
   check whether its instrument_key is in `ikey_to_symbol` map and
   whether the WS streamer is actually subscribing to it.

## Network blip at 13:02 IST

Observed during validation: brief DNS failure resolving
`api.upstox.com`. System handled it correctly:
- All in-flight chain calls failed gracefully
- Streamer entered exponential backoff (3s → 4s → 7s → 10s → 15s → 23s)
- WS reconnected at 13:04:17 in `'full'` mode
- Chain refresh restarted automatically at 13:04:20
- 198/199 OI counts unchanged through the blip (state held last-good)

Code worked as designed. Flagged here for completeness, no action needed.
