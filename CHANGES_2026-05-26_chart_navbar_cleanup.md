# Chart pin + TV-buttons removal + navbar revamp + moneyness explainer

**Date:** 2026-05-26 23:14 IST
**Backup:** `~/Desktop/fno-live-dashboard_backup_2026-05-26_23-14-12/`

## What changed

### 1. Chart fix — lightweight-charts pinned to v4.2.3
The script tag was previously `unpkg.com/lightweight-charts/dist/...` with no version pin, so unpkg served whatever the latest was. v5 dropped `addCandlestickSeries` / `addAreaSeries` / `addHistogramSeries` in favour of `addSeries(LightweightCharts.CandlestickSeries, …)`, which silently broke our chart pane (the loading spinner would spin forever — no console error, just no series rendering).

```diff
- <script src="https://unpkg.com/lightweight-charts/dist/lightweight-charts.standalone.production.js"></script>
+ <script src="https://unpkg.com/lightweight-charts@4.2.3/dist/lightweight-charts.standalone.production.js"></script>
```

The codebase still uses the v4 series API. If we ever want to migrate to v5 we change the version *and* refactor the four `addXxxSeries()` calls in `initLightweightChart()`. Until then v4.2.3 is the supported stable.

### 2. Removed Bull TV / Bear TV / Both TV from controls
The three `oi-tv-btn` buttons were taking up real estate in the filter bar without giving much value (nobody on the team uses TradingView watchlist export). Buttons removed; the underlying `exportOiThesisTV()` function is preserved with a comment marker so the buttons can be put back without code changes.

### 3. Top navbar revamp
Three refinements to the existing `.q-nav-links` cluster:

- **Icons** added to each primary link (Dashboard / Sectors / Nifty 50). Inline SVGs, no extra requests.
- **Pill background** on the link cluster — the three links now sit inside a single rounded container that visually groups them.
- **Active state** is now a gradient pill (accent → cyan) with a soft glow, instead of the underline slider. Looks more "trader terminal" and less "marketing site".
- **Brand wordmark** uses the same accent→cyan gradient text so the brand and active link visually agree.
- **Hover** lifts the icon by 1px and brightens the label, with a 180 ms cubic transition.
- **Compact mode** (≤1100 px) hides the labels and shows icons only.

All wrapped inside the existing "OVERRIDE BLOCK — 2026-05-26 sizing tweaks" so a single comment-out reverts everything.

### 4. Threshold alignment
The Top Picks V2 recipe `moneynessPattern()` was using `> 1000 contract` thresholds while the rest of the dashboard (pill display, filter dropdown) uses **`< 500` as the noise floor**. Aligned both to 500 so the pill, the dropdown filter, and the V2 recipe all see the same pattern for any given chain. Smoke tests still pass (16/16).

### 5. New doc — `MONEYNESS_MATH_EXPLAINER.md`
A standalone page-and-a-half explainer covering:

- How the six counters (`atm_ce`, `atm_pe`, `near_ce`, `near_pe`, `deep_ce`, `deep_pe`) are populated server-side from chain ticks
- The exact `getCol(ce, pe)` rules that turn each pair into G / R / N
- Threshold logic (500-contract noise floor) and why
- The 7 patterns (`GGG`, `RRR`, `GGR`, `RRG`, `RGR`, `GRG`, `NNN`) and what each means in trader vocabulary
- A worked example with a synthetic RELIANCE chain
- Tuning knobs (one number, one place)
- File-and-line pointers to every place the math is applied

## Files touched

| File                                    | Change                                                                                |
|-----------------------------------------|---------------------------------------------------------------------------------------|
| `dashboard_live.html`                   | Chart pin, TV buttons removed, navbar icons + revamp CSS, threshold aligned to 500    |
| `_smoke_top_picks_v2.js`                | Threshold aligned to match dashboard `getCol`                                         |
| `MONEYNESS_MATH_EXPLAINER.md` (new)     | Full math + worked example + code references                                          |

## Smoke tests

```
node _smoke_top_picks_v2.js   →   16 passed, 0 failed
HTML parses cleanly (html.parser)
JS is `node --check` clean
Curl on https://unpkg.com/lightweight-charts@4.2.3/...   →   200, addCandlestickSeries present in bundle
```

## Reverts (one-line each)

| To revert        | Action                                                                                |
|------------------|---------------------------------------------------------------------------------------|
| Chart pin        | Change `lightweight-charts@4.2.3` back to `lightweight-charts` (re-introduces the bug)|
| TV buttons       | Restore the `oi-tv-split` div from the backup (`grep -A 8 oi-tv-split` on backup)     |
| Navbar revamp    | Comment out the `Top navbar revamp` section in the OVERRIDE BLOCK at the top of `<style>` |
| Top Picks V2     | `window.TOP_PICKS_V2 = false`                                                         |

## Next move
Reload `dashboard_live` in your browser. The intraday chart should populate with candles + volume + OI series within a couple of seconds. If anything still looks off (loading spinner stuck, or series missing), open the browser console once — that'll tell us whether v4.2.3 has any breaking change vs whatever we were on yesterday. Worst case we drop to v4.1.7 which was the last version before the v5 refactor began.
