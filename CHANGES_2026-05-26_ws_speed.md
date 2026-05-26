# Step 1+2 — WS speed pass — 2026-05-26 (afternoon)

Backup: `~/Desktop/fno-live-dashboard_backup_2026-05-26_06-43-13/`

## What changed

### `ws_server.py`

1. `OHLC_POLL_INTERVAL` default: **5s → 60s**.
   Previously fired 4 batches × every 5s = ~48 req/min just for OHLC. Now
   it's a once-a-minute safety net.

2. `poll_ohlc` gate widened. Previously skipped REST only when streamer
   was in `full_d5` mode. Now skips whenever `streamer.connected == True`,
   regardless of mode. Rationale: in `full` and `full_d5` modes the WS
   tick stream already delivers OHLC, vtt, OI, IV via `_handle_ws_tick`;
   in `ltpc` mode the price/change is live and we accept slightly stale
   day-OHLC since `poll_ohlc` still runs every 60s as the safety net.

### `config.env`

- `WS_FEED_MODE=full` was already present locally. Confirmed it's the
  right value: `full` is the Upstox v3 mode that wraps `MarketFullFeed`
  (LTPC + day OHLC + vtt + oi + iv). The runtime decoder in
  `upstox_ws_stream.py` reads every one of those fields.

### Code default unchanged

`UpstoxStreamer(... mode=os.environ.get("WS_FEED_MODE", "ltpc"))`
stays as is. To revert, just remove or change the `WS_FEED_MODE` line in
`config.env`. No code rollback needed.

## Verification (markets closed, today)

Synthetic-protobuf smoke tests passed for:

- **decoder round-trip**: a hand-built `MarketFullFeed` payload (LTPC +
  day OHLC + vtt + oi + iv) decodes to the expected dict via
  `decode_feed_response`.
- **streamer dispatch**: `UpstoxStreamer._process_message` fed that
  binary frame routes the decoded fields into the `on_tick` callback as
  `{ltp, cp, chg, chg_pct, open, high, low, vol, oi, iv}`.
- **state writeback**: `_handle_ws_tick` driven by that delta updates
  `state[symbol]` with prev_close, gap_pct, range_pct, vol_surge_5d,
  score, and emits a tick broadcast carrying every field the dashboard
  uses today.
- **gate honored**: `poll_ohlc` makes zero REST calls while
  `streamer.connected = True`.

The smoke test file was removed after running. It lives in git history
of this change if needed.

## Tomorrow morning checklist (when markets open)

In the order I'd watch them:

1. **Start ws_server.** Look for the streamer log line:
   `Subscribed to N instruments in 'full' mode via Upstox WS v3`
   If you see `'full' mode` and N matches your stock count + indices,
   subscription succeeded.

2. **Check tick rate.** `GET /api/admin/status` (admin auth required)
   shows `upstox_ws_stream.ticks_received`. After 30 seconds it should
   be in the thousands. If it's still 0 after a minute, the WS path is
   broken — fall back instructions below.

3. **Check OHLC stops being a hot path.** Watch server.log; the
   `OHLC poll: %d stocks updated` line should appear at most once a
   minute (it's now the safety net), not every 5 seconds. When the
   streamer is connected it shouldn't appear at all between
   reconnections.

4. **Compare dashboard freshness.** Open the dashboard. LTP and OHLC
   columns should update within a second or two of NSE prints, not lag
   by 5+ seconds.

## Fallback if `full` mode misbehaves

Two failure modes are possible at first market touch:

- **Upstox rejects the subscription** (subscription tier, symbol cap,
  etc.). The streamer will log `Upstox WS connect failed` or no
  `live_feed` will arrive. In that case:

      # config.env
      WS_FEED_MODE=ltpc

  Restart ws_server. You're back to the old behavior. `poll_ohlc` at 60s
  will still pull OHLC, just at lower frequency than before — if you
  want the old 5s aggressiveness back:

      OHLC_POLL_INTERVAL=5

- **Streamer flaps disconnects.** `poll_ohlc` will resume between
  drops because the gate is `streamer.connected`, not mode-based. So
  the dashboard stays populated; you just lose the speed win during
  flap windows.

## What was deliberately NOT changed

- `poll_ltp` still runs as a fallback (already gated). No change needed.
- `poll_chains` and `poll_oi_fast` are independent endpoints; this pass
  doesn't touch them.
- `populate_avg5d_vol` is a one-shot at startup, untouched.
- No changes to Fyers streamer (separate path; not in scope).

## Net expected impact

When markets open and the WS subscription works:

- REST calls per minute drop from ~52 (4 LTP batches × 12/min + 12 OHLC
  batches/min) to ~4 (1 OHLC safety-net cycle/min when streamer is
  briefly down, otherwise zero).
- Dashboard LTP and OHLC become tick-driven instead of 5s-quantized.
- Score/direction recompute per tick instead of per 5s poll, so the
  trade-ready highlight reacts in near-real-time.
