# OI Moneyness Pattern — exact math

How the 3-column "GGG / RRR / GGR / …" pill is computed end-to-end, from raw chain ticks to the colored squares you see on the dashboard.

This is a deterministic function of the live option chain. There is no magic, no smoothing, no AI guessing — just OI deltas binned by distance from ATM.

## TL;DR

```
column 1 = ATM strike            (one strike — the spot's nearest)
column 2 = Near OTM              (the two strikes immediately above + below)
column 3 = Deep OTM              (everything ≥ 2 strikes away from ATM, both sides)

For each column, compare ΣΔ(CE OI) vs ΣΔ(PE OI):
   PE writing > CE writing  → G   (puts being written → support → bullish)
   CE writing > PE writing  → R   (calls being written → resistance → bearish)
   roughly equal  / very small  → N   (no actionable shift)

The pill is just those three letters concatenated: e.g. GGG, RRR, GGR.
```

## Step 1 — Server-side: build the six counters

In `ws_server.py`, inside the chain pipeline (`_compute_chain_metrics_*`), for each strike on the chain we have:

```python
ce_chg = call_options.market_data.oi - call_options.market_data.prev_oi
pe_chg = put_options.market_data.oi  - put_options.market_data.prev_oi
```

These are **raw OI deltas in shares** (Upstox returns OI in shares; the lot multiplier is applied client-side when the user wants "lots" view).

Each strike is then categorized by its distance from the ATM strike, measured as **rank index difference** (not a price band):

```python
sorted_strikes  = sorted({s for s in chain})        # all strikes ascending
atm_index       = sorted_strikes.index(atm_strike)  # ATM's rank
diff_idx        = sorted_strikes.index(this_strike) - atm_index
```

Then we accumulate into six buckets:

| `diff_idx`     | bucket  | counter           |
|----------------|---------|-------------------|
| `== 0`         | ATM     | `atm_ce_chg += ce_chg`, `atm_pe_chg += pe_chg` |
| `== +1`        | Near OTM (call side, above ATM) | `near_ce_chg += ce_chg` |
| `== −1`        | Near OTM (put side, below ATM)  | `near_pe_chg += pe_chg` |
| `>= +2`        | Deep OTM (call side, far above) | `deep_ce_chg += ce_chg` |
| `<= −2`        | Deep OTM (put side, far below)  | `deep_pe_chg += pe_chg` |

A few things to notice about this bucketing:

1. **It's by rank, not by price.** "Near" doesn't mean "within ±1%" — it means "the next strike either side of ATM". For NIFTY that's typically ±50 points; for an illiquid stock that might be ±10. This makes the metric portable across symbols with different strike intervals.
2. **Calls only contribute upward, puts only contribute downward.** A call at strike − 2 is so deeply ITM it has near-zero OI activity, and a put at strike + 2 is the same — so we deliberately skip those mismatched sides. The Near and Deep buckets are *one-sided* on each rank.
3. **ATM is the only bucket that has both sides.** That's why you'll often see ATM dominated by whichever side is fading, while Near/Deep tells you who's getting positioned for the next move.

The server emits the six numbers as one object on every chain refresh:

```json
"moneyness": {
  "atm_ce":  -3260,   "atm_pe":  +12480,
  "near_ce": +1820,   "near_pe": +6340,
  "deep_ce": +740,    "deep_pe": +9120
}
```

That object is what shows up on the WebSocket payload and feeds straight into the table cell + the Top Picks recipe.

## Step 2 — Client-side: collapse each pair into G / R / N

The dashboard has one tiny function — `getCol(ce, pe)` — that turns each `{ce, pe}` pair into a letter. It's used in three places (pill renderer, filter logic, Top Picks V2 recipe) and they all agree on the same threshold:

```javascript
function getCol(ce, pe) {
  if (ce === 0 && pe === 0)                      return 'N';
  if (Math.abs(ce) < 500 && Math.abs(pe) < 500)  return 'N';   // both too small → noise
  if (pe > ce)  return 'G';   // put writing dominates → support → bullish
  if (ce > pe)  return 'R';   // call writing dominates → resistance → bearish
  return 'N';
}
```

Three rules, in order:

| Rule | Trigger | Letter |
|------|---------|--------|
| Both buckets exactly zero (no chain yet) | `ce == 0 && pe == 0` | `N` |
| Both buckets below the noise floor of 500 contracts | `\|ce\| < 500 && \|pe\| < 500` | `N` |
| Otherwise compare absolute values | `pe > ce` | `G` |
| | `ce > pe` | `R` |
| | tie | `N` |

The 500-contract floor is the only "magic" number. It's the same one used by both the pill display (`fmtMoneynessPill`) and the dropdown filter (`bull_support`, `bear_resist`, etc), so what you see and what you filter on always agree.

Important: this is a comparison of **signed** OI changes, treating the sign as direction. So `ce = +5000, pe = +6000` reads as "puts being written *more aggressively* than calls" → G, even though both sides added open interest. Conversely, `ce = -5000, pe = -2000` reads as "calls being unwound much harder than puts" — that's also G (call unwinds = call sellers covering = supply absorbed = bullish).

This is why the Top Picks V2 recipe pairs the moneyness gate with the OI thesis flow gate: GGG alone could be either fresh put-writing or call-unwinding. The thesis flow (`ce_oi_chg < 0 AND pe_oi_chg > 0`) tightens it to "calls unwinding while puts being written" — both directions agree.

## Step 3 — Concatenate to a 3-letter pattern

The pill is just:

```javascript
const pattern =
  getCol(m.atm_ce,  m.atm_pe)   +    // ATM column
  getCol(m.near_ce, m.near_pe)  +    // Near OTM column
  getCol(m.deep_ce, m.deep_pe);      // Deep OTM column
```

That's literally a 3-character string, one of:

```
GGG GGR GRG GRR  RGG RGR RRG RRR
NNN NGN NRN ...     (any column may be N if too quiet)
```

## Step 4 — What each pattern means (trader vocabulary)

| Pattern | Filter dropdown name | Meaning                                                                 | When it's interesting                                                                                  |
|---------|----------------------|-------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------|
| `GGG`   | Support [G\|G\|G]    | Put writers active across ATM, near, and deep OTM. Wall of puts forming. | Bullish bias — deep buyers think downside is capped.                                                   |
| `RRR`   | Resist [R\|R\|R]     | Call writers active across all three buckets. Wall of calls forming.    | Bearish bias — sellers think upside is capped.                                                          |
| `GGR`   | Capped [G\|G\|R]     | Bullish ATM/Near, but call writers still leaning at the deep OTM strikes. | "Buy the dip" with a target ceiling — short-term up, capped before it runs.                             |
| `RRG`   | Trap [R\|R\|G]       | Bearish ATM/Near, but puts being written deep OTM.                       | Down move likely, but deep OTM put writers expect a hard floor — so don't chase short past the floor. |
| `RGR`   | Strangled [R\|G\|R]  | Calls written at ATM and far OTM, puts written in the middle ring.       | Range-bound; both ends being defended; iron-condor friendly.                                           |
| `GRG`   | Breakout [G\|R\|G]   | Puts written ATM and far OTM, call writers in the middle.                 | Compression that often resolves into a directional move.                                                |
| `NNN`   | (no filter)          | Whole chain quiet (under 500-contract noise floor everywhere).           | No actionable read — usually small-cap or pre-open / post-close.                                       |

The Top Picks V2 recipe uses the two extreme patterns (`GGG` and `RRR`) because they're the only ones where all three columns confirm the same direction. Mixed patterns are good for context but ambiguous for picking a single "BUY CE" or "BUY PE" card.

## Worked example

Suppose at 10:15 AM, RELIANCE chain delta dump looks like:

| Strike | CE Δ OI | PE Δ OI |
|--------|---------|---------|
| 1280   | -100    | +180    |
| 1290 (Near put) | -80 | **+8200** |
| 1300 (ATM)     | **-3450** | **+12800** |
| 1310 (Near call) | **+1900** | -120 |
| 1320           | +940    | -60   |
| 1330           | +540    | -40   |

Bucketing with ATM = 1300, sorted strikes [1280, 1290, 1300, 1310, 1320, 1330]:
- `atm_index = 2`. Each row's `diff_idx = its rank − 2`.
- Row 1280 → diff_idx = -2 → deep PE side: `deep_pe_chg += 180`
- Row 1290 → diff_idx = -1 → near PE side: `near_pe_chg += 8200`
- Row 1300 → diff_idx = 0 → ATM: `atm_ce_chg += -3450`, `atm_pe_chg += 12800`
- Row 1310 → diff_idx = +1 → near CE side: `near_ce_chg += 1900`
- Rows 1320, 1330 → diff_idx ≥ +2 → deep CE side: `deep_ce_chg += 940 + 540 = 1480`

Counters land at:

```
atm:   ce = -3450,  pe = +12800
near:  ce = +1900,  pe = +8200
deep:  ce = +1480,  pe = +180
```

Apply `getCol`:

- ATM: `|−3450| ≥ 500`, `|12800| ≥ 500`, both above floor. `pe (12800) > ce (−3450)` → **G**
- Near: `1900` vs `8200`, both above floor. `pe > ce` → **G**
- Deep: `1480` vs `180`. `1480 > 180`, so `ce > pe` → **R**

Pattern: **`GGR`** — "Capped". The Top Picks V2 recipe rejects this (`pattern !== 'GGG' && pattern !== 'RRR'`) because the deep OTM column dissents. The dropdown filter `bull_capped` would surface it though, if you wanted to play the bullish-but-capped scenario explicitly.

If instead the deep CE side had only added +200 (below noise floor) and the deep PE side stayed at +180 (also below floor), the deep column would be **N**, giving pattern `GGN`. Top Picks would still reject it (not pure GGG), but the pill would render with the deep square dimmed grey.

## Where in the code

| What                          | File                | Function / region                           |
|-------------------------------|---------------------|---------------------------------------------|
| Server-side bucketing         | `ws_server.py`      | `~ line 870` — `Universal rank-index moneyness buckets` block |
| WebSocket payload field       | `ws_server.py`      | `moneyness_data` dict, attached as `"moneyness"` |
| Pill rendering (3 squares)    | `dashboard_live.html` | `fmtMoneynessPill(m)`                       |
| Dropdown filter (`bull_support` etc.) | `dashboard_live.html` | the `getCol(c, p)` inside `applyFilters()` |
| Top Picks V2 gate             | `dashboard_live.html` | `computeHotlistUniverse()` / `moneynessPattern()` |
| Sortable column comparator    | `dashboard_live.html` | `getSortValue(stock, 'moneyness')` — sorts by `deep_pe − deep_ce` (Put-Deep dominance) |

All five touch points use **the same threshold (500)** and **the same comparator (sign of `pe − ce`)**, so the pill, the filter, the recipe, and the sort always agree.

## Tuning knobs (if behaviour ever needs to change)

If the pattern mix is ever too noisy or too quiet, only one number needs adjusting in one place. Lowering the floor adds more letters but also more false reads on slow stocks. Raising it makes the metric more conservative.

```javascript
// dashboard_live.html, search for: Math.abs(ce) < 500 && Math.abs(pe) < 500
//   500   → current default (noise floor of 500 contracts)
//   1000  → tighter, fewer GGG/RRR reads
//   200   → looser, more reads but more noise
```

The 500 floor was chosen because Indian F&O lots are typically 500–4000 contracts, so 500 is roughly "less than one full lot's worth of writing" — by definition not enough to claim a directional view.
