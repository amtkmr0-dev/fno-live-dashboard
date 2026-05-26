# Theme picker fix + Quantra brand themes + logo — 2026-05-26 13:11 IST

Backup: `~/Desktop/fno-live-dashboard_backup_2026-05-26_13-11-11/`

## What was actually broken

The theme explorer let you click a theme card, but the visual change
didn't happen for any theme except `midnight-blue` (the default).

**Root cause:** `static/css/themes.css` had every non-default theme
selector written with **literal backslash-escaped quotes**:

```css
:root[data-theme=\"obsidian-black\"] { ... }   /* WRONG — invalid CSS */
```

CSS doesn't parse `\"` — browsers treat the entire selector as invalid
and silently drop the whole rule block. So all 5 classic themes
(`obsidian-black`, `emerald-dark`, `royal-purple`, `crimson-red`,
`arctic-blue`) were dead on arrival. The DB-write was succeeding, the
JS was setting `data-theme` correctly — but the CSS rules attached to
those selectors never matched anything.

This is also why hitting Apply seemed to "lock" — the theme was saved
fine, you just couldn't tell because nothing rendered.

## The fix

```css
:root[data-theme="obsidian-black"] { ... }     /* RIGHT */
```

One-character search-and-replace across themes.css. All 5 classic
themes now apply correctly.

## What's added — Quantra brand themes (5)

Five new themes designed for the terminal's identity:

| ID                | Vibe                                            |
| ----------------- | ----------------------------------------------- |
| `quantra-carbon`  | Refined dark, true-black bg, blue accent        |
| `quantra-slate`   | Soft slate-navy, indigo accent, eye-friendly    |
| `quantra-paper`   | Clean light, daytime / printing                 |
| `quantra-onyx`    | Pure-black OLED with neon-cyan / neon-green     |
| `quantra-mint`    | Warm grey + mint-green / terracotta semantics   |

Each follows the existing CSS-variable contract (every theme defines
the same `--bg`, `--surface*`, `--border*`, `--text*`, `--accent*`,
`--green/--red/--blue/etc` set), so adding a new theme costs only
~50 lines of variables — no need to touch other CSS files.

A new section "Quantra Brand Themes" appears at the top of the
explorer, ahead of "Classic Interfaces" and "Premium Visual Concepts".

## What's added — Logo set

Three SVG files in `static/img/`:

- **`quantra-monogram.svg`** — geometric "Q" formed from a rounded
  square with a diagonal slash and a tick mark. Single-color via
  `currentColor`, scales to favicon size cleanly.
- **`quantra-wordmark.svg`** — monogram + "QUANTRA" text in Inter 800
  with 3px letter-spacing. Drop-in for the existing nav-bar
  `<img src="/static/img/logo.png" /> <span>QUANTRA</span>` pattern.
- **`favicon.svg`** — same monogram with a dark background tile and a
  fixed accent color, suitable for browser favicons.

I deliberately did **not** swap the existing `logo.png` references
across the dozen+ HTML pages. That's a full visual pass and benefits
from a single coordinated commit when you're ready.

## What's still NOT working (pre-existing, not regressed)

The 8 "premium" themes in the explorer's `themesList` reference IDs
that have no CSS rules in themes.css:

- `obsidian-quantum`, `neon-cyber`, `glassmorphism`, `minimal-pro`,
  `terminal-hacker`, `gradient-luxury`, `neumorphism`, `bloomberg-pro`

These cards still show in the "Premium Visual Concepts" section but
selecting them will just fall back to the default theme. They were
broken before this pass too. If you want any of them, we'd need to
write the CSS rules — same shape as the Quantra themes.

## What's still NOT done (deliberate, flagged for next pass)

1. **Full re-skin of dashboard / sub-pages.** The new themes plug into
   the existing variable system, so most surfaces inherit them
   automatically. But individual screens (chat panel, paper-trades
   table, admin cards) have hardcoded colors that bypass `var()` calls.
   Those need a sweep for a true brand-consistent look.
2. **Logo swap on every page.** Dashboard, login, register, profile,
   admin, paper, sectors, oi-thesis, oi-scanner, nifty,
   advanced-analytics, billing — each currently uses `<img
   src="/static/img/logo.png" />`. Replace with the new wordmark when
   you're ready.
3. **TATAMOTORS data.** While debugging earlier today I found Tata
   Motors was demerged on NSE: equity is now `TMCV`, F&O contracts are
   under `TMPV` (passenger vehicles, the equivalent of old TATAMOTORS).
   Our universe still expects `TATAMOTORS`. This is a data/business
   call, not a code bug — explained in detail in our chat earlier
   today, queued for the next data-update pass.

## Verified now

- `themes.css` selectors: all 10 explicit themes (5 classic + 5
  Quantra) have valid CSS selectors. `midnight-blue` is the default
  `:root` and works.
- No stray escape characters anywhere in themes.css.
- All 3 SVG files parse as valid XML.
- `static/theme_explorer.html` HTML structure is balanced.
- `themesList` contains 19 entries (5 Quantra + 6 classic + 8 premium
  premium-broken-not-fixed).

## How to verify yourself

Tomorrow morning when you load the dashboard:

1. Open `http://localhost:8080/static/theme_explorer.html`
2. Click any "Quantra" or "Classic" card — the page should re-render
   with the new theme **immediately** (live preview).
3. Click "Apply Selected" — toast says saved, prompts to return to
   dashboard.
4. Reload dashboard — theme persists.
5. Repeat with another theme. Picker is no longer "stuck".

If a Premium theme is selected, you'll see a fallback to the default —
that's the pre-existing limitation, not a regression.
