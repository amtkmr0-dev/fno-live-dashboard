# Login / Register / Verify-Email redesign + Bloomberg Pro restore

**Date:** 2026-05-26 21:09 IST
**Branch:** `feat/security-themes-2026-05-26`
**Commit:** `28bc31f`
**Backup:** `~/Desktop/fno-live-dashboard_backup_2026-05-26_21-09-30/`

## What changed

### Auth pages (full rewrite)
- `login.html`: 2203 → 830 lines
- `register.html`: 2458 → 662 lines
- `verify_email.html`: 675 → 519 lines

All three rebuilt from scratch on the same modern shell:
- Split-screen layout (brand canvas on the left, form on the right)
- Animated grid + three drifting glow orbs in CSS only (no JS, no images)
- Gradient headlines, monospace eyebrows, JetBrains Mono accents
- Default theme: **Quantra Onyx** (the dark variant)
- Top-right theme switcher with all 12 themes grouped by Quantra / Premium
  · choice persists in `localStorage` under `quantra_theme`
- Tagline on login: **"The trader's edge, decoded."**
- Subtag: "Real-time options chain. Live OI flow. Smart signals."
- Live ticker strip on login canvas (cosmetic, no API call)
- Trust strip on register canvas (200+ stocks, <1s tick latency, etc.)
- Password strength meter on register (4-segment bar)
- Paste-aware 6-digit OTP row on verify-email with arrow-key navigation
- Show/hide password toggles
- Responsive: collapses to single column at ≤920px, tightens at ≤480px

### Wire compatibility (preserved)
| Page             | Endpoint                    | Body fields                                                           | Redirect on success                          |
|------------------|-----------------------------|-----------------------------------------------------------------------|----------------------------------------------|
| login.html       | `POST /api/auth/login`      | `username`, `password`                                                | `/`                                          |
| register.html    | `POST /api/auth/register`   | `username`, `password`, `confirm_password`, `display_name`, `email?`  | `/verify-email?uid=…&email=…` or `/login?registered=1` |
| verify_email.html| `POST /api/auth/verify-email` | `otp`, `user_id?`                                                  | `/`                                          |
| verify_email.html| `POST /api/auth/resend-otp` | `user_id?`                                                            | (stays on page, restarts 30 s timer)         |

Same field IDs (`username`, `password`, `display-name`, `confirm-password`, `email`, `otp0..otp5`), same client-side validators (`USERNAME_RE = /^[A-Za-z0-9_]+$/`, password upper/lower/number), same `sessionStorage` keys (`verify_uid`, `verify_email`).

### Bloomberg Pro theme (restored)
Re-added `:root[data-theme="bloomberg-pro"]` block to `static/css/themes.css` with the authentic terminal palette:
- `--bg: #000000` · `--accent: #ffb000` (signature amber) · `--cyan: #00bfff` (header text) · `--green: #00cc66` · `--red: #ff3333`
- Cards default to amber-on-black with cyan section headers — what you'd see on a real Bloomberg desk.
- Distinct from `trader-pro` (which uses warmer orange `#ff9800` and lighter background tones).

Also added the matching `theme-card[data-theme="bloomberg-pro"]` swatch rules and the entry in `theme_explorer.html`'s `themesList`.

**Theme count: 12 working themes** (5 Quantra + 7 Premium).

## Smoke tests run

```
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8080/login         → 200 (26750 bytes)
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8080/register      → 200 (34022 bytes)
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8080/verify-email  → 200 (20494 bytes)
curl -s -X POST .../api/auth/login -d '{"username":"_","password":"_"}'      → {"error":"Invalid credentials"}  ← endpoint reachable
```

HTML parsed cleanly with `html.parser` — no unbalanced tags. Themes CSS still has balanced `{` `}` (226/226).

## Manual visual check (still pending)

Open these in a browser and click through each theme in the switcher:
- [ ] http://localhost:8080/login
- [ ] http://localhost:8080/register
- [ ] http://localhost:8080/verify-email?uid=1&email=test%40example.com

Confirm:
- [ ] Default theme renders as Quantra Onyx (dark + cyan accents)
- [ ] Theme switcher opens and 12 themes are listed with swatches
- [ ] Selecting a theme visually changes the page and persists across pages
- [ ] Bloomberg Pro shows amber-on-black with cyan header text
- [ ] Form submission still works end-to-end with a real account

## Files modified
- `login.html`             (rewrite)
- `register.html`          (rewrite)
- `verify_email.html`      (rewrite)
- `static/css/themes.css`  (added bloomberg-pro block + swatches)
- `static/theme_explorer.html` (added bloomberg-pro entry to themesList)

## Files NOT modified
- `auth_proxy.py` — unchanged; serves the HTML files raw, so no template breakage risk
- All other dashboard pages — they use the same `themes.css`, so they automatically gain Bloomberg Pro

## Next move
- **Test the redesign visually**, especially under Quantra Paper (light) and Bloomberg Pro to make sure the gradient h1 and orbs read correctly on both extremes.
- Once happy, open the GitHub PR for `feat/security-themes-2026-05-26` and merge.
