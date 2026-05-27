# How to use the Vol Spread column

The new "Vol Spread" column on the dashboard is the **Cremers-Weinbaum
volatility spread** at the ATM strike. Math:

```
vol_spread_atm  =  IV(ATM call)  −  IV(ATM put)
```

Both legs come straight from your existing chain payload. Zero new API calls.

## What the sign means

| Value | Reading | What it suggests |
|-------|---------|------------------|
| **≥ +2.0** | calls aggressively bid up vs puts | strongly bullish |
| **+1.0 to +2.0** | calls richer than puts | bullish hint (paper-cited threshold) |
| **+0.3 to +1.0** | slight call premium | mildly bullish, weaker signal |
| **−0.3 to +0.3** | balanced | neutral, no read |
| **−0.3 to −1.0** | slight put premium | mildly bearish |
| **−1.0 to −2.0** | puts richer than calls | bearish hint |
| **≤ −2.0** | puts aggressively bid up vs calls | strongly bearish |

The paper found **~50 basis points per week** of expected outperformance for
stocks in the calls-rich bucket vs the puts-rich bucket, holding for ~1 week
before unwinding.

> Citation: Cremers & Weinbaum, "Deviations from Put-Call Parity and Stock
> Return Predictability", *Journal of Financial and Quantitative Analysis*,
> 2010.
> *Content was rephrased for compliance with licensing restrictions.*

## How it complements your existing recipe

Your current Top Picks V2 logic:

> `total_oi >= 100k` AND `vol_surge != null` AND moneyness ∈ {GGG, RRR}
> AND (CE↓+PE↑ for bull / CE↑+PE↓ for bear), top 2 each side

That captures **OI flow direction**. Vol spread captures **price-pressure
direction** — what informed buyers are willing to *pay up for*. They're
independent signals. When they agree, the conviction is much higher.

### Three concrete ways to use it

#### 1. Confluence filter on Top Picks (recommended starting point)

Of the 4 cards Top Picks shows you, focus on the ones where the sign of
vol_spread *matches* the side:

| Top Picks side | Vol Spread that confirms | Vol Spread that contradicts |
|---|---|---|
| **BUY CE** (bull) | ≥ +1.0 (calls richer) → take it | ≤ −1.0 (puts richer) → skip or wait |
| **BUY PE** (bear) | ≤ −1.0 (puts richer) → take it | ≥ +1.0 (calls richer) → skip or wait |

Mismatched cards aren't necessarily wrong — they're just lower-conviction.
The simplest discipline: **skip mismatches for the first week** to see how
they actually behave.

#### 2. Disqualify, don't qualify

Don't add it as another required gate (you'd cut your candidates from ~5/day
to maybe 1/day). Instead:

- Pass-existing-recipe AND `|vol_spread| < 0.5` → take it but at smaller
  size. Price market is *neutral* on the call/put balance.
- Pass-existing-recipe BUT vol_spread is **opposite-sign and bigger than ±2**
  → that's a *warning*. Price market disagrees with your flow read. Either
  skip, or wait for the next chain refresh to see if it converges.

#### 3. The "hidden bias" scan — leading indicator view

Click the column header to **sort by vol_spread descending**. The top of the
list is where call-buyers are most aggressive. If a stock there is ALSO
showing GGG moneyness and a recent volume surge but isn't yet on Top Picks,
it's a setup the binary OI-rule may have missed because the OI delta isn't
yet large enough to cross the threshold.

Same on the bottom of the sort (most negative spread + RRR + surge = bearish
setup that may be brewing before OI confirms).

This is your **leading indicator** view. The Top Picks bar is the lagging
confirmation.

## Worked example — 5 hypothetical reads

| Stock | OI Thesis | Moneyness | Vol Spread | Action |
|-------|-----------|-----------|-----------|---------|
| RELIANCE | bull | GGG | **+2.1** | Both signals agree → high-conviction BUY CE |
| HDFCBANK | bull | GGG | **+0.3** | Flow says bull, price flat → smaller-size BUY CE |
| TCS | bull | GGG | **−1.4** | Flow says bull, price says bear → **skip** or wait |
| INFY | bear | RRR | **−2.5** | Both agree → high-conviction BUY PE |
| ICICIBANK | (no thesis) | mixed | **+3.0** | Doesn't pass existing recipe, but call-buyers aggressive → **watch closely** |

## Filter button: "Calls Rich (≥+1)"

The new toggle next to "NIFTY 50" in the controls bar filters the entire
table to symbols where `vol_spread_atm ≥ +1.0`. Useful for the "hidden bias
scan" — turn it on, then sort by your other column of interest (vol surge,
score, OI change) to see the bullish leading indicators.

## Validation discipline

Don't trust this signal blindly. The recommendation: watch what happens to
symbols where vol_spread strongly *agreed* vs. strongly *disagreed* with the
OI thesis for **5 trading sessions**. If after a week you find the
agreement set actually trades better than the existing recipe alone, layer
it permanently into Top Picks. If not, it's adding noise on Indian weeklies
and we drop it.

Track this in a simple spreadsheet:
- Date / Symbol / OI Thesis side / Vol Spread / Took the trade? / +1d return / Match?

## Tooltip on hover

Each cell now shows a rich, value-specific tooltip on hover. The tooltip
explains:
- Exact value
- What the math is (call IV − put IV)
- A trader-vocabulary reading (strongly bullish / mildly bearish / neutral)
- The general rule of thumb thresholds
- The paper citation

## Caveats

- Indian-market specific: the original paper was on US equity options.
  Magnitudes may compress on Indian weeklies due to the shorter expiry cycle.
- Thinly-traded ATMs can show large absolute spreads (e.g. ±5 or more)
  that are real but reflect a wider bid-ask, not informed flow. Use the
  filter `Calls Rich (≥+1)` rather than the column-sort extreme to avoid
  acting on noise from low-liquidity symbols.
- Spread can flip rapidly intraday — you'll see it move in real time as
  IV updates each chain refresh. Trust the *sustained* reading over a 15-min
  window, not single ticks.

## Where in the code

| What | File | Function / region |
|------|------|-------------------|
| Server-side computation | `ws_server.py` | After the `atm_iv` block — `vol_spread_atm = atm_ce_iv - atm_pe_iv` (both legs guarded > 0) |
| WebSocket payload field | `ws_server.py` | `vol_spread_atm` in chain return dict |
| Column header | `dashboard_live.html` | `<th data-key="vol_spread_atm">` between IV% and MaxPain |
| Cell renderer | `dashboard_live.html` | `fmtVolSpread`, `volSpreadClass`, `volSpreadTooltip` |
| Filter toggle | `dashboard_live.html` | `toggleVolSpread()` + `filters.volSpread` |
| Live update path | `dashboard_live.html` | inside the chain-refresh handler, `updateCellHTML(sym, 'vol_spread_atm', …)` |

## Tuning knob (only if needed)

If the threshold ever needs to change, it lives in two places that must agree:

```javascript
// dashboard_live.html, function volSpreadClass(v):
//   change the 1.0 cutoff to whatever you decide on.

// dashboard_live.html, function toggleVolSpread / applyFilters:
//   d.vol_spread_atm >= 1.0  ← the same number
```

The default of 1.0 IV-points matches the cutoff used in the
Cremers-Weinbaum paper for the "expensive call" bucket.
