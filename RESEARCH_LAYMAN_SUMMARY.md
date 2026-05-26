# What the research papers actually say — plain English version

Your current rule:

> "If puts are being written and calls are being unwound (`CE↓ + PE↑`)
> AND the moneyness pill is GGG, buy the ATM call."

That's a perfectly fine starting rule. Researchers have spent 20+ years
studying exactly this kind of thinking and have proposed four
refinements that consistently work. Here's what each one says, in
plain words.

---

## Paper 1 — "How aggressively are puts being bought?" (Pan & Poteshman)

### What they noticed

When something good is about to happen to a stock, smart money doesn't
just buy *some* call options — they buy *a lot* compared to puts. The
ratio between the two changes long before the news hits.

### The simple version

Your rule today is **binary**: it asks "is `CE OI` falling AND `PE OI`
rising? yes/no". The paper's rule is **a number**:

> ratio = (puts being added) / (calls being added)

A ratio of **1.5** means 50 % more put-writing than call-writing —
mildly bullish.
A ratio of **5** means **5×** more put-writing than call-writing —
*aggressively* bullish.
A ratio of **0.3** means much more call-writing than put-writing —
mildly bearish.

Both the "1.5" symbol and the "5" symbol pass your current binary gate,
but the paper says **the 5 is meaningfully more likely to go up**. So
instead of treating all GGG symbols equally, rank them by how
lopsided the ratio is.

### Why it matters in plain words
Your rule says "vote happened, side picked". The paper says "now also
look at the *margin of victory*."

---

## Paper 2 — "Are calls overpriced compared to puts?" (Cremers & Weinbaum)

### What they noticed

Two options on the same stock with the same expiry and same strike —
one call, one put — should mathematically have the same implied
volatility ("IV"). When they don't, **someone is bidding one side up**.
That someone usually knows something.

### The simple version

For every stock, look at the ATM strike. Compare:

> the ATM call's IV  vs  the ATM put's IV

- If **call IV > put IV by more than 1 point** → calls are "expensive" →
  bullish hint. Stocks like this beat the market by ~50 basis points
  *over the next week* in the paper's data.
- If **put IV > call IV by more than 1 point** → puts are
  "expensive" → bearish hint.
- If they're within 1 point of each other → no signal.

### Why it matters in plain words

OI tells you who's *placing bets*. The IV gap tells you who's *willing
to pay up to place those bets*. Two different things. The paper found
the IV gap was a stronger predictor than just looking at OI changes
alone.

This is also the **easiest** of the four to add to our system — every
chain refresh already includes `ce_iv` and `pe_iv`. It would be ~5
lines of server code to start tracking.

---

## Paper 3 — "How scared is everyone of a crash?" (Xing, Zhang, Zhao)

### What they noticed

Out-of-the-money puts (e.g., NIFTY 23000-PE when the index is at 23800)
always have higher IV than ATM options. That's "the smirk" — people
pay a premium for crash insurance. But the *steepness* of that
premium varies week to week.

When the smirk gets **steeper** for a particular stock — meaning OTM
puts are getting *unusually* expensive vs. ATM — somebody is paying
heavily for downside insurance. The paper found that stocks with the
**steepest** smirks underperformed stocks with the **flattest**
smirks by about 11 % per year.

### The simple version

For each stock, compare:

> IV of an OTM put (2 strikes below ATM)  vs  IV at ATM

- If the OTM put is **a lot** more expensive than ATM → smirk is steep →
  someone is heavily insuring downside → next month tends to be worse.
- If the OTM put is **only slightly** more expensive than ATM → smirk
  is flat → no fear premium → next month tends to be normal/better.

### Why it matters in plain words

This is a **"what's the mood?"** signal. Not a buy/sell trigger by
itself. It tells you: even if everything else looks bullish today, when
the smirk is steepening, the medium-term tape has a heavy undertone.
You'd take the trade smaller, or you'd skip it.

---

## Paper 4 — "Are options unusually busy compared to the stock?" (Roll, Schwartz, Subrahmanyam)

### What they noticed

When something is brewing, options trading volume goes up **before**
stock trading volume does. The reason: options give 5×–10× leverage,
so an informed trader gets more bang per rupee in the options market.

### The simple version

For each stock:

> O/S = (today's CE volume + today's PE volume) / today's stock volume

Compare today's O/S to its 20-day average:

- O/S > **1.5×** the 20-day average → options are *unusually* busy →
  pay attention, something is happening.
- O/S near 1× → normal day, ignore.

The paper found that stocks with low O/S (calm, no unusual options
flow) outperformed stocks with high O/S by about 1.47 % per month. The
counterintuitive direction is because high O/S often means *too much*
speculation — the news is already in the price.

### Why it matters in plain words

This is your **"which symbols deserve attention today?"** filter — the
*first* gate before applying anything else. It cuts a 200-symbol
universe down to ~10–20 symbols where something interesting is
*possibly* happening.

---

## Putting them all together

Imagine your current rule is one criterion: "the OI flow agrees with a
bullish trade." That's fine, but ~30 stocks pass that gate every day,
most of which won't do anything special.

Now stack the four refinements on top:

1. **Roll-Schwartz O/S spike** → "is anything actually unusual here?" —
   cuts 30 → ~12.
2. **Pan-Poteshman ratio magnitude** → "how strong is the OI flow?" —
   cuts 12 → top 5.
3. **Cremers-Weinbaum IV gap** → "are buyers willing to pay up?" —
   cuts 5 → top 2.
4. **Xing-Zhang-Zhao smirk** → "is the medium-term tape supportive?" —
   sizes the trade smaller if not.

That's the kind of stack institutional desks use. Each layer has been
shown to work *independently* in published research, so combining them
is mathematically reasonable (they don't overlap much).

---

## What I'd actually recommend doing first

Just **one** of these, the easiest to add:

> **Cremers-Weinbaum IV gap = `ce_iv[ATM] - pe_iv[ATM]`**

Reason:
- 5 lines of server code (you already have both IVs in the chain payload).
- No new API calls.
- Surface as a single column on the dashboard table next to PCR.
- Add a filter toggle "IV gap > +1" to the controls bar.
- Run it for 5 trading days alongside your current rule and watch
  whether the symbols passing **both** filters behave better than ones
  passing only your current rule.

If yes → keep it, layer in the next signal (smirk).
If no → drop it, try the smirk first.

This is the same way the academic papers were validated — not with
philosophy but with **tracking real outcomes for a few weeks**.

Either way, **don't try to add all four at once** — you won't be able
to tell which one is helping and which is just noise.
