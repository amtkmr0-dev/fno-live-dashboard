# FNO Live Dashboard - Quantra Terminal

A real-time F&O (Futures & Options) trading dashboard with AI-powered analysis, paper trading, and automated signal generation.

## 🌐 Live Production
**URL**: http://34.132.142.58:8080/

## 🏗️ Architecture

```
┌─────────────────┐
│   Frontend      │  HTML/CSS/JS (dashboard_live.html, index.html, etc.)
│   (Port 8080)   │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  auth_proxy.py  │  Authentication & Security Layer
│   (Port 8080)   │  - User auth, sessions, CSRF protection
└────────┬────────┘  - Rate limiting, brute force guard
         │           - Paper trades, AI chat
         │ Proxies to
         ▼
┌─────────────────┐
│  ws_server.py   │  Core WebSocket Server
│   (Port 8081)   │  - Real-time market data
└─────────────────┘  - Upstox API integration
         │
         ▼
┌─────────────────┐
│   SQLite DB     │  quantra.db
│  (db.py)        │  - Users, sessions, trades, audit logs
└─────────────────┘
```

## 📦 Components

### Backend (Python)
- **`auth_proxy.py`** - Main authentication proxy server (port 8080)
- **`ws_server.py`** - WebSocket server for real-time data (port 8081) *[On GCP server]*
- **`db.py`** - SQLite database layer with PBKDF2 password hashing
- **`security.py`** - Rate limiting, CSRF, brute force protection
- **`chat_analysis.py`** - AI-powered market analysis (Perplexity/NVIDIA NIM)
- **`auto_trader.py`** - Automated trading signal generation

### Frontend (HTML/CSS/JS)
- **`index.html`** - Main landing page
- **`dashboard_live.html`** - Real-time F&O dashboard
- **`login.html`** / **`register.html`** - Authentication pages
- **`profile.html`** - User profile & settings
- **`paper_trades.html`** - Paper trading interface
- **`admin.html`** - Admin panel
- **`divergence.html`** - MTF divergence scanner
- **`rsi.html`** / **`rsi-analysis.html`** - RSI analysis tools
- **`sectors.html`** - Sector heatmap

### Utilities
- **`setup_auth.py`** - Initial user setup script
- **`patch_*.py`** - Server patching utilities
- **`fix_dashboard.sh`** - Deployment helper script

## 🚀 Setup & Installation

### Prerequisites
- Python 3.8+
- SQLite3 (built-in)
- Upstox API credentials
- (Optional) Perplexity or NVIDIA NIM API key for AI chat

### Local Development Setup

1. **Clone the repository**
   ```bash
   cd ~/Desktop/FNO\ Dashboard/fno-live-dashboard
   ```

2. **Create virtual environment**
   ```bash
   python3 -m venv venv
   source venv/bin/activate  # On macOS/Linux
   # venv\Scripts\activate   # On Windows
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure environment**
   ```bash
   cp config.env.example config.env
   cp auth_config.json.example auth_config.json
   # Edit config.env and auth_config.json with your API keys
   ```

5. **Initialize database**
   ```bash
   python3 setup_auth.py
   # Follow prompts to create admin user
   ```

6. **Run the servers**
   
   **Terminal 1 - Backend WebSocket Server:**
   ```bash
   python3 ws_server.py
   # Runs on port 8081
   ```
   
   **Terminal 2 - Auth Proxy:**
   ```bash
   python3 auth_proxy.py
   # Runs on port 8080
   ```

7. **Access the dashboard**
   ```
   http://localhost:8080/
   ```

## 🔐 Security Features

- **PBKDF2-HMAC-SHA256** password hashing (600K iterations)
- **Account lockout** after 5 failed login attempts (15-min cooldown)
- **Rate limiting** on all endpoints (login, register, API, chat)
- **CSRF protection** with double-submit cookie pattern
- **Session management** with IP binding and expiry
- **Audit logging** for all security events
- **Input validation** and sanitization
- **Security headers** (CSP, HSTS, X-Frame-Options, etc.)

## 📊 Features

### Real-Time Dashboard
- Live F&O data with WebSocket updates
- Sector heatmap with momentum indicators
- Multi-timeframe analysis
- Trade-ready signals with confidence scores
- TradingView chart integration

### Paper Trading
- Manual trade entry with SL/Target levels
- Automated trade execution based on signals
- P&L tracking and performance analytics
- Per-user trade limits and capital management

### AI Chat Assistant
- Context-aware market analysis
- Integration with live dashboard data
- Deep Upstox API analysis
- Multi-provider support (Perplexity, NVIDIA NIM)

### User Management
- Role-based access control (admin/user)
- Profile customization
- Trading preferences and settings
- Session management across devices

## 🗄️ Database Schema

### Tables
- **`users`** - User accounts with PBKDF2 hashed passwords
- **`sessions`** - Active user sessions with IP tracking
- **`user_settings`** - Per-user trading preferences
- **`paper_trades`** - Manual and automated paper trades
- **`auto_signals`** - AI-generated trading signals
- **`login_audit`** - Security audit trail

## 🔧 Deployment (GCP)

### Current Production Setup
- **Server**: GCP Compute Engine (instance-20260412-171736)
- **IP**: 34.132.142.58
- **User**: amitkumar
- **Path**: ~/deploy/
- **Services**: 
  - `ws_server.service` (systemd)
  - `auth_proxy.service` (systemd)

### Deployment Commands
```bash
# SSH to server
ssh amitkumar@34.132.142.58

# Navigate to deployment directory
cd ~/deploy

# Pull latest changes
git pull origin main

# Restart services
sudo systemctl restart ws_server
sudo systemctl restart auth_proxy

# Check status
sudo systemctl status ws_server
sudo systemctl status auth_proxy

# View logs
tail -f server.log
tail -f audit.log
```

## 📝 Configuration Files

### `config.env`
Environment variables for API keys, tokens, and server settings.
**⚠️ Never commit this file to git!**

### `auth_config.json`
Authentication and security configuration:
- Admin paths
- Session settings
- AI provider configuration
- Rate limit rules

### `quantra.db`
SQLite database (auto-created on first run)

## 🧪 Testing

```bash
# Run tests (if implemented)
pytest tests/

# Test auth endpoints
curl -X POST http://localhost:8080/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"your_password"}'

# Check server health
curl -I http://localhost:8080/
```

## 📚 API Endpoints

### Authentication
- `POST /api/auth/login` - User login
- `POST /api/auth/register` - User registration
- `GET /api/auth/verify` - Verify session
- `GET /api/auth/logout` - Logout

### User Management
- `GET /api/user/profile` - Get user profile
- `POST /api/user/profile` - Update profile
- `POST /api/user/settings` - Update trading settings
- `POST /api/user/change-password` - Change password
- `GET /api/user/stats` - Trading statistics

### Paper Trading
- `GET /api/user/trades` - List trades
- `POST /api/user/trades` - Create trade
- `PUT /api/user/trades/:id` - Update trade
- `DELETE /api/user/trades/:id` - Delete trade

### AI Chat
- `POST /api/chat` - Send chat message
- `POST /api/chat/setup` - Configure AI provider

## 🐛 Troubleshooting

### Server won't start
```bash
# Check if ports are in use
lsof -i :8080
lsof -i :8081

# Kill existing processes
pkill -f auth_proxy.py
pkill -f ws_server.py
```

### Database locked
```bash
# Check for stale connections
fuser quantra.db
# Restart services
```

### WebSocket connection fails
- Verify `ws_server.py` is running on port 8081
- Check firewall rules
- Inspect browser console for errors

## 📄 License

Proprietary - All rights reserved

## 👤 Author

Amit Kumar

## 🔗 Links

- Production: http://34.132.142.58:8080/
- GCP Server: `amitkumar@instance-20260412-171736:~/deploy`

---

## 🧠 For AI agents continuing development

If you're an AI session picking up after the developer, **start by reading**:

```
data/research/_TODO_NEXT_SESSION.md     # what's queued
data/research/zerodha_varsity_m5_notes.md   # India-specific options grounding
data/research/max_pain_notes.md         # what max pain is/isn't
```

These files capture mid-session decisions and research findings that
chat history alone cannot preserve. The `data/` folder is gitignored
(local-only), so rely on the developer's machine state, not the repo.

---

**Last Updated**: May 2026
