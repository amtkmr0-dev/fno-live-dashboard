# Deferred tasks — single source of truth

Every feature, optimization, and refactor we've discussed but **not yet shipped**.
Top of list = highest priority. Update this file every time we defer something or finish a P0 item.

Last updated: **2026-05-27 08:10 IST**

---

## P0 — must do soon (before / on next session)

| # | Task | Why it's P0 | Estimated effort |
|---|------|-------------|------------------|
| 1 | **Cremers-Weinbaum vol spread** as a column on the dashboard table | The single most-cited research signal we haven't shipped. 5 lines server-side, 1 column on table, 1 filter toggle. No new API calls. The "concrete improvement to the trading edge" the user explicitly asked for. | 15-25 min |
| 2 | **30 s TTL cache on `/api/candles`** | Drops Upstox call rate sharply on every page that polls candles. The single highest-leverage operational change. ~5 lines wrap on `handle_api_candles`. | 5-10 min |
| 3 | **Theme color editor (Part B from the bloomberg-pro discussion)** | Allow per-color overrides on top of any theme; save as "Custom" 13th theme via localStorage. Bloomberg Pro consistency is now fixed (Part A done) but the editor lets the user fine-tune any future theme. | 60-90 min |

---

## P1 — high priority, queued

| # | Task | Why | Effort |
|---|------|-----|--------|
| 4 | **Pan-Poteshman magnitude ranking** in Top Picks | Replace binary `CE↓ + PE↑` with continuous `Σpe_chg / Σce_chg` ratio for ranking. Picks lopsided OI flow over barely-passing setups. | 30 min |
| 5 | **Xing-Zhang-Zhao smirk slope** as a column | `pe_iv[atm-2] − avg(ce_iv[atm], pe_iv[atm])`. Use as regime filter, not buy/sell trigger. | 30 min |
| 6 | **Roll-Schwartz-Subrahmanyam O/S ratio** as first-stage filter | Cuts a 200-symbol universe to ~10-20 with unusual options activity. Needs a 20-day rolling baseline in SQLite. | 90 min |
| 7 | **Composite `bull_conviction` score (0-100)** | Combine #4-6 + Cremers-Weinbaum into one ranking. Only act on top quartile. Depends on all four signals being live first. | 60 min |
| 8 | **Validation harness** | Offline backfill on `chain_snapshot` + `chain_strike` for 30 days, plot hit rate by score bucket. Then 5-day shadow mode on dashboard. | 2-3 hours |
| 9 | **Storage-key mismatch fix** | Login/register/verify pages write `quantra_theme` (underscore); rest of app reads `quantra-theme` (hyphen). Pick a winner, migrate the other side. | 10 min |
| 10 | **TATAMOTORS demerger decision** | NSE split into `TMCV` (equity) + `TMPV` (F&O). Codebase now picks both up after the latest restart (201 stocks). Verify both behave correctly during market hours, then either keep both or drop one. | 30 min on review |

---

## P2 — operational / hygiene

| # | Task | Why | Effort |
|---|------|-----|--------|
| 11 | **Adaptive chain refresh tiers (Option A)** | Full universe every 15 min, hot tier (watchlist + paper-trade-open + recipe-matched) every 2-3 min within same broker budget. | 90-120 min |
| 12 | **Append latest tick to candle stream client-side** | Instead of repolling `/api/candles`, append the WS tick to the local series. Best long-term fix. | 60 min |
| 13 | **Snapshot intraday candles to SQLite** | Makes `/api/candles` SQLite-first, Upstox only for the latest tail. | 90 min |
| 14 | **Tools dropdown nav consistency** | Add icons to OI Scanner, OI Thesis, RSI, Advanced Desk, Profile, Paper Trades, Settings, Admin, Logout. | 20 min |
| 15 | **Cleanup orphaned chart libraries** | Delete `static/js/lightweight-charts.js` + `static/js/rendering_core.js` (both pages now use CDN). ~800 KB of dead bytes. | 5 min |
| 16 | **Stale `.ws_server.pid` file** | Cosmetic but inaccurate. Server writes pidfile but old one lingers across restarts. | 10 min |

---

## P3 — bigger / longer-term

| # | Task | Why | Effort |
|---|------|-----|--------|
| 17 | **Full Nifty 50 page rewrite** | Currently paused via splash + nav-link hide. The reskin shipped is a stylesheet over the existing HTML; the *real* rewrite (matching auth pages — clean structure, ~1100 lines, modular layout) is queued. | 3-4 hours |
| 18 | **Refactor `ws_server.DashboardServer`** | The 6,287-line single class is the biggest source of future bugs. Split into `MarketDataPoller`, `OptionChainAnalyzer`, `PaperTradeService`, `BroadcastHub`, `AdminAPI`, `PublicAPI`. Staged refactor across multiple PRs. | 1-2 weeks elapsed |
| 19 | **2FA / TOTP via Google Authenticator** | Trivial server-side, nice user-trust win. | 90 min |
| 20 | **NSE holiday calendar gate** | Auto-trades currently fire on Republic Day, etc. Pull NSE calendar, gate `is_trading_window`. | 60 min |
| 21 | **FUTSTK expiry filter** | Today's Upstox NSE.csv.gz is clean (already removed expired May 26), but on a future expiry day where NSE keeps the just-expired contract for one extra day, the parser would pick stale. Filter to `expiry > today` in `download_and_parse_instruments`. | 15 min |

---

## P4 — defer indefinitely / on hold

| # | Task | Why on hold |
|---|------|-------------|
| 22 | **Replace pytz with stdlib zoneinfo** | One less dependency. Low value, low risk — defer. |
| 23 | **Move secrets out of `config.env`** | Was discussed in GCP context; user is local-only, so lower priority. Could revisit if deploying. |
| 24 | **Push to git main** | Branch `feat/security-themes-2026-05-26` is 17+ commits ahead of main and 27 behind. PR-create link: `https://github.com/amtkmr0-dev/fno-live-dashboard/pull/new/feat/security-themes-2026-05-26`. Decide on PR vs. merge strategy. |
| 25 | **Decide on the leaked-token-in-history situation** | Expired Upstox JWT in commit `c0d0d3a` on `main`. Token is dead (expired May 22). Could `git filter-repo` it out, or leave it. Risk is low. |

---

## Done in past sessions (for reference, do not re-do)

- ✅ Security hardening (binds, signed identity, OTP, cookies, CSP)
- ✅ WS feed speed pass (OHLC poll 5s→60s, full-feed mode confirmed)
- ✅ REST rate meter + admin UI panel
- ✅ Dashboard freshness pill (top bar)
- ✅ Top Picks V2 (strict OI thesis + GGG/RRR + vol-surge ranking)
- ✅ Hot list filter button (auto-rebuilds each chain refresh)
- ✅ Bull TV / Bear TV / Both TV buttons removed
- ✅ Navbar revamp + icon pills propagated to all 17 pages
- ✅ Login / register / verify-email pages full rewrite
- ✅ 12 themes available (5 Quantra + 7 Premium incl. Bloomberg Pro restored)
- ✅ Chart libraries pinned to `lightweight-charts@4.2.3` on both dashboard + nifty
- ✅ Nifty page paused (splash + nav hide)
- ✅ RSI scanner paused (gated WS + auto-scan)
- ✅ Index ticker bar + sector heatmap enlarged
- ✅ Broker Health Check card with one-click "Test Both Brokers"
- ✅ TATAMOTORS demerger picked up (201 stocks now, was 199)
- ✅ Bloomberg Pro consistency overrides (Part A) — sector chips, signal pills, buildup pills, conviction badges, nav active state, ticker numbers, freshness pill
- ✅ Research deep-dive doc with 4 cited papers (Pan-Poteshman, Cremers-Weinbaum, Xing-Zhang-Zhao, Roll-Schwartz-Subrahmanyam)
- ✅ Layman summary of research papers
- ✅ Upstox API per-page usage audit
- ✅ Backups pruned (kept only latest two)

---

## How to use this file

- When we discuss a feature but don't ship it, **add it here under the right priority bucket**.
- When we ship something on this list, **move it to the "Done" section** at the bottom and delete the entry.
- P0 items should **drive the next session's work**. If something has been P0 for more than 3 sessions, either ship it or downgrade it.
- Effort estimates are wall-clock for the AI assistant in a focused session. User review/testing time is on top.
