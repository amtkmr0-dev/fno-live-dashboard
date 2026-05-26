# Top Picks V2 + Hot list + UI sizing tweaks

**Date:** 2026-05-26 23:03 IST
**Backup:** `~/Desktop/fno-live-dashboard_backup_2026-05-26_23-03-25/`
**File touched:** `dashboard_live.html` only

## What changed

### 1. Top Picks navbar — strict recipe (V2)
Replaced the conviction-tier-based picks with a deterministic OI-thesis recipe:

```
universe = {
  total_oi >= 100,000
  AND vol_surge is not null
  AND moneyness pattern == 'GGG' or 'RRR'
  AND
    bull side: ce_oi_chg < 0  AND  pe_oi_chg > 0
    bear side: ce_oi_chg > 0  AND  pe_oi_chg < 0
}
bull cards = top 2 of (universe ∩ bull) sorted by vol_surge desc
bear cards = top 2 of (universe ∩ bear) sorted by vol_surge desc
```

- Bull cards label `BUY CE` with green left border.
- Bear cards label `BUY PE` with red left border.
- Each card now shows its `#1 / #2` rank and the `vol_surge` value as the headline metric (replacing the composite Score).
- Detail line shows `LTP · Premium · IV · CE OI Δ / PE OI Δ` for at-a-glance confirmation.
- If 0 candidates match, the bar shows `"No symbols match the OI thesis + GGG/RRR + surge filter yet."` instead of waiting indefinitely.

### 2. Hot list filter (footer)
New footer button `🔥 Hot (n)` next to `★ Watchlist`. Toggling it filters the table to the same V2 universe (full bull + bear set, not just the 4 cards). The set is auto-recomputed on every chain refresh so it stays fresh without manual intervention.

- Counter (`hotCount`) updates in place each render.
- Toggle is session-only (not persisted), so a fresh reload always starts with Hot = off.

### 3. UI sizing
A single contiguous CSS block at the bottom of the inline `<style>` overrides the original sizes:

| Element            | Before       | After        |
|--------------------|--------------|--------------|
| Index ticker bar   | 44 px tall   | 56 px tall   |
| Index value font   | 13 px        | 17 px        |
| Index label font   | 9 px         | 11 px        |
| Index change font  | 10 px        | 12 px        |
| Index tile width   | 110 px       | 130 px       |
| Heat-cell padding  | 4 × 8 px     | 7 × 12 px    |
| Heat-cell font     | 10 px        | 11 px        |
| Heat-cell width    | 60 px        | 78 px        |
| Heat-cell h-name   | semibold     | bold + tracking |

Heatmap-bar gap goes from 4 px to 6 px.

## Reverts (one-line each)

| To revert        | Action                                                                                  |
|------------------|-----------------------------------------------------------------------------------------|
| Top Picks logic  | In `dashboard_live.html` set `window.TOP_PICKS_V2 = false;` (line near end of script).  |
| UI sizing        | Comment out the `OVERRIDE BLOCK — 2026-05-26 sizing tweaks` section in the inline CSS.  |
| Hot button       | Remove the `<button class="footer-hot-btn" ...>` element from the footer markup.        |

The legacy renderer is preserved as `_renderTopPicksLegacy()` and is invoked automatically when the V2 flag is `false`.

## Smoke tests (all passed)
`_smoke_top_picks_v2.js` exercises `computeHotlistUniverse` against a synthetic `stocks` dict and asserts:

- bull/bear sides have the right cardinality
- ranking is by `vol_surge` desc
- exclusion holds for: pattern ≠ GGG/RRR, total_oi below floor, null surge, no thesis flow, missing moneyness object
- top-2 truncation works on both sides
- sparse universe (1 bull / 0 bear) returns 1 + 0 with no padding

```
16 passed, 0 failed
```

Also: HTML parses cleanly (`html.parser`), JS is `node --check` clean, CSS braces balance.

## What this still won't do (deliberate)
- No tier-2 backfill. If only 1 bull symbol matches the strict recipe, you get 1 bull card. We don't relax the moneyness gate to fill the slot.
- No score column. The card head shows surge multiple, not the composite 0–100 score, because surge is what the recipe ranks by.
- The Hot list is auto, not manual. There is no "pin to hot" gesture; the set comes from the recipe only.

## Next move
Watch the markets open at 09:15 with the dashboard up. The Top Picks bar should populate with up to 4 cards within ~15 minutes (after the first full chain sweep). Note any symbols that show up — if the recipe is too restrictive (often empty) we relax to include `GG_` / `RR_` patterns; if it's too noisy, we tighten the surge threshold.
