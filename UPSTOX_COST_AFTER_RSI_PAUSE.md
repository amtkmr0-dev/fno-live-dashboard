# Upstox cost: where we stand, what changes tomorrow

## Background load (always running, regardless of any open page)

| Loop                                | Endpoint              | Calls / min | Notes                                    |
|-------------------------------------|-----------------------|-------------|------------------------------------------|
| Chain refresh (`poll_chains`)       | `/option/chain`       | ~13         | 196 symbols ÷ 15 min                     |
| Quotes batch (`poll_quotes`)        | `/market-quote/quotes`| ~2          | batched every 30 s                       |
| Daily candles seed                  | `/historical-candle/...`| ~0 steady | one-shot at boot                         |
| Upstox WebSocket streamer           | none (WS, not REST)   | 0           | streams ticks, no REST                   |

**Background floor: ~15 calls/min during market hours.**

## Per-page contribution

|                              | Before today      | After RSI pause + Nifty pause                                |
|------------------------------|-------------------|--------------------------------------------------------------|
| RSI page (auto-scan + manual)| ~390 calls per scan, twice/day = ~780 calls/day | **0**       |
| Nifty page (5 s polling)     | ~12 calls/min while open                        | **0**       |
| Dashboard analysis panel     | ~1 call/min while pinned, 60 s client cache     | unchanged   |
| Admin "Test Token" click     | +1 per click                                     | unchanged   |

## What this means tomorrow

**Before today's pauses, on a typical trading day with the Nifty page pinned and one RSI scan in the morning:**
- Background: 15/min × 375 min (9:15 → 15:30) = **5,625 calls**
- Nifty page open ~6h: 12/min × 360 min = **4,320 calls**
- 1 RSI scan: **~390 calls**
- Dashboard panel (intermittent, cached): **~50 calls**
- **Total ≈ 10,400 Upstox calls/day**

**Tomorrow with RSI paused + Nifty paused:**
- Background: same **5,625 calls**
- Dashboard panel (intermittent): **~50 calls**
- **Total ≈ 5,700 Upstox calls/day**

**Drop: ~4,700 calls/day. Roughly 45% reduction.**

The Upstox v2 limit is ~25 req/sec per app (~1500/min, ~562k/day). So we were at ~2 % of headroom and are now at ~1 %. **Neither number is anywhere close to the rate limit.** The pauses don't help us avoid throttling — they help us be a *good citizen* and reduce wasted calls on data we're not actually using.

The bigger win these pauses unlock is **stability under stress**. If something on Upstox's side gets flaky (it has happened), every saved call is one less retry-loop you might get pulled into. Quieter system = more predictable behaviour during the moments that matter.

## Cost of activating advanced-analytics page

Looking at `advanced-analytics.html`:
- The only fetch is `/api/advanced-chain?symbol=XYZ` on click.
- Server-side that handler reads from `self.state` in memory.
- **No Upstox calls per page open. No polling. Zero ongoing cost.**

You can pin advanced-analytics open all day for free (in Upstox-call terms). The page itself uses your already-streamed chain data.

## Summary in one line

After today's pauses we use **~5,700 Upstox calls/day**, down from ~10,400. Activating advanced-analytics adds zero. We are nowhere near a rate limit and never were — but the system is now quieter and more predictable for tomorrow's open.
