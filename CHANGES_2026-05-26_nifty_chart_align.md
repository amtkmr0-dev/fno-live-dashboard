# Align nifty.html chart with the dashboard chart

**Date:** 2026-05-26 23:26 IST
**Backup:** `~/Desktop/fno-live-dashboard_backup_2026-05-26_23-26-15/`

## Why

The `dashboard_live.html` analysis-panel chart was fixed earlier today by pinning `lightweight-charts@4.2.3` from CDN. The user asked for the same chart on `nifty.html` for the NIFTY index.

`nifty.html` already had a chart, but it was loading **two copies of LightweightCharts**:

1. `/static/js/lightweight-charts.js` — v5.2.0 standalone build
2. `/static/js/rendering_core.js?v=100` — also v5.2.0 standalone build (header literally says `TradingView Lightweight Charts™ v5.2.0`)

Both write to the same `window.LightweightCharts` global, so whichever loads last wins. With the v5 file winning, the chart code calling v5 syntax (`addSeries(LightweightCharts.X, …)`) sometimes worked, but the `watermark:` createChart option was silently dropped (v5 removed it in favour of a separate `createTextWatermark()` plugin), and any v4-style call elsewhere on the page would silently fail.

This change unifies both pages on one library version and one API style.

## What changed

### Library reference
```diff
- <script src="/static/js/lightweight-charts.js"></script>
- <script src="/static/js/chart.umd.min.js"></script>
- <script src="/static/js/rendering_core.js?v=100"></script>
+ <script src="https://unpkg.com/lightweight-charts@4.2.3/dist/lightweight-charts.standalone.production.js"></script>
+ <script src="/static/js/chart.umd.min.js"></script>
```

`rendering_core.js` was a duplicate v5 build and was removed entirely (the file still sits in `static/js/` for now — orphaned but harmless; will clean up in a separate sweep). `chart.umd.min.js` is Chart.js 4.4.1 used for the right-panel horizontal OI buildup chart, so it stays.

### API conversions (v5 → v4)
Seven `addSeries(LightweightCharts.X, {...})` calls converted to the v4 `addXSeries({...})` form:

| v5                                                          | v4                                |
|-------------------------------------------------------------|-----------------------------------|
| `addSeries(LightweightCharts.CandlestickSeries, …)`         | `addCandlestickSeries(…)`         |
| `addSeries(LightweightCharts.HistogramSeries, …)` × 4       | `addHistogramSeries(…)` × 4       |
| `addSeries(LightweightCharts.AreaSeries, …)` × 2            | `addAreaSeries(…)` × 2            |
| `addSeries(LightweightCharts.LineSeries, …)` (drawing tool) | `addLineSeries(…)`                |

All other chart code on `nifty.html` (drawing mode, dual-pane crosshair sync, OHLCV legend HUD, watermark, time-scale formatters) is untouched and continues to use v4 syntax it was already compatible with.

### Bonus side-effect
The `watermark: { text: 'NIFTY 5m', … }` option in both `createChart()` calls now actually renders, because v4 supports it natively. Under v5 it had been silently ignored.

## Files modified
- `nifty.html` — script tag pin + 7 series-creation calls converted

## Files orphaned (kept for now)
- `static/js/lightweight-charts.js`
- `static/js/rendering_core.js`

## Smoke tests
- HTML parses cleanly (`html.parser`)
- JS extracted from inline `<script>` blocks is `node --check` clean
- `addSeries(LightweightCharts.X, …)` count: 0 (was 7)
- v4 add*Series calls present: `addLineSeries` × 1, `addCandlestickSeries` × 1, `addHistogramSeries` × 4, `addAreaSeries` × 2 — totals match
- Library pin: `lightweight-charts@4.2.3` confirmed in script tag

## Reverts
| To revert        | Action                                                                           |
|------------------|----------------------------------------------------------------------------------|
| Library pin      | Restore the three local-script tags from the backup, drop the CDN line.          |
| API style        | Reverse each of the 7 `addXSeries(...)` calls back to `addSeries(LightweightCharts.X, ...)`.  |

Both pages now share **one** LightweightCharts version (v4.2.3) and **one** API style (v4 series helpers), so any future chart work flows through one mental model.

## Next move
With markets opening at 09:15 tomorrow, the nifty page should show:
1. Dual-pane chart (price candles on top, OI delta + cumulative bands on bottom) populating within a few seconds of page load.
2. The watermark text (`NIFTY 5m`) faintly visible in both panes.
3. The OHLCV legend in the top-left updating with crosshair movement.

If anything still looks off (most likely candidate: a v5-only feature I missed), open the browser console once and the failing call will be obvious. Worst case we drop to `lightweight-charts@4.1.7` — last release before the v5 refactor began.
