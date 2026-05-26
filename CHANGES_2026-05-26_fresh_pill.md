# Dashboard freshness pill — 2026-05-26

## What changed

`dashboard_live.html`:
- Top-bar pill (`#freshPill`) added to the existing `q-nav-status` block,
  next to the connection dot.
- CSS in the inline `<style>` block: `.fresh-pill` with four states
  (`fresh`, `warm`, `stale`, `offline`).
- JS function `updateFreshPill()` runs every 1s. Reads `lastTick`
  (already updated on every WS `tick` message) and `wsConn.readyState`,
  then sets the pill class + label.
- `setConnStatus()` calls `updateFreshPill()` on disconnect for
  immediate visual feedback.

## State machine

| Condition                          | Pill class | Label             | Color  |
|------------------------------------|------------|-------------------|--------|
| Socket up, no ticks yet            | warm       | "waiting"         | yellow |
| Socket down, no ticks yet          | offline    | "offline"         | gray   |
| Last tick < 2s                     | fresh      | "live" (blinking) | green  |
| 2s ≤ last tick < 10s               | warm       | "5.5s" etc.       | yellow |
| 10s ≤ last tick < 60s              | stale      | "30s stale"       | red    |
| 60s ≤ last tick                    | stale      | "5m stale"        | red    |
| Socket dropped (any age)           | offline    | "offline"         | gray   |

## Verified now

JS smoke test (`_smoke_fresh_pill.js`, ran + deleted) covered 13
transitions including the 2s and 10s boundaries, the 60s→minutes label
flip, and the disconnect override. All green.

DOM sanity check: only one `freshPill` in the file, CSS class
`.fresh-pill` defined, function `updateFreshPill` defined, 1s interval
registered.

## What you'll see today at 9:15 AM

The pill should:
1. Show "waiting" (yellow) the moment you load the dashboard, until the
   first WebSocket tick arrives.
2. Flip to "live" (green) within seconds and stay green during normal
   trading.
3. Flip to a "Ns" yellow countdown if ticks pause for 2-10 seconds (a
   normal between-tick gap on lightly traded symbols won't push it past
   yellow because the LTP stream covers the whole universe).
4. Go red ("30s stale") if the WS feed actually stalls. That's your
   signal to check `/api/admin/status` for the streamer state and the
   API rate meter for whether something is hammering Upstox.
5. Go gray ("offline") if the socket itself drops. The existing
   reconnect logic kicks in; the pill recovers automatically.

## Cost

CSS: ~25 lines. JS: ~35 lines, one 1Hz timer touching ~3 DOM nodes.
Negligible CPU and memory.
