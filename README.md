# QUANTRA Terminal — F&O Live Dashboard

Real-time NSE Futures & Options analytics dashboard with WebSocket streaming, option chain analysis, and paper trading.

## Architecture

```
Browser → auth_proxy.py (8080) → ws_server.py (8081)
                ↓
           quantra.db (SQLite)
```

- **auth_proxy.py** — HTTPS reverse proxy with session auth, serves HTML pages
- **ws_server.py** — WebSocket server streaming live market data from Upstox API
- **dashboard_live.html** — Main dashboard (Bloomberg Terminal-style UI)

## Setup

### Prerequisites
- Python 3.10+
- Upstox Developer Account ([register here](https://account.upstox.com/developer/apps))

### Install
```bash
pip install -r requirements.txt
cp config.env.example config.env
# Edit config.env with your Upstox API credentials
```

### Run
```bash
# Start WebSocket server
python3 ws_server.py &

# Start auth proxy (serves the dashboard)
python3 auth_proxy.py
```

### Access
Open `https://your-server:8080` in browser.

## Pages

| Page | Path | Description |
|------|------|-------------|
| Dashboard | `/` | Main F&O scanner with live data |
| Paper Trades | `/paper` | Paper trading journal |
| Sectors | `/sectors` | Sector-wise analysis |
| RSI Scanner | `/rsi` | RSI-based stock scanner |
| Stock Analysis | `/rsi-analysis` | Individual stock deep-dive |
| Divergence | `/divergence` | MACD divergence signals |
| Profile | `/profile` | User settings |
| Admin | `/admin` | Admin panel (admin role only) |

## Token Rotation

Upstox access tokens expire daily at ~03:30 IST. Regenerate at:
https://account.upstox.com/developer/apps

## Tech Stack

- **Backend**: Python (aiohttp)
- **Frontend**: Vanilla HTML/CSS/JS
- **Data**: WebSocket (Upstox API v2)
- **Auth**: Session-based (bcrypt + aiosqlite)
- **Fonts**: DM Sans + JetBrains Mono
