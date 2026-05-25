# Quick Start Guide - FNO Live Dashboard

## 🎯 For New Developers

### 1. Clone & Setup (5 minutes)

```bash
# Navigate to project
cd ~/Desktop/FNO\ Dashboard/fno-live-dashboard

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 2. Configure (2 minutes)

```bash
# Copy example configs
cp config.env.example config.env
cp auth_config.json.example auth_config.json

# Edit with your API keys
nano config.env
nano auth_config.json
```

### 3. Initialize Database (1 minute)

```bash
# Create admin user
python3 setup_auth.py

# Follow prompts:
# Username: admin
# Password: [your secure password]
# Role: admin
```

### 4. Run Locally (2 terminals)

**Terminal 1 - Backend Server:**
```bash
# Note: ws_server.py is on GCP server
# For local dev, you'll need to either:
# 1. Copy it from GCP: scp amitkumar@34.132.142.58:~/deploy/ws_server.py .
# 2. Or connect to production backend (not recommended for dev)

python3 ws_server.py
```

**Terminal 2 - Auth Proxy:**
```bash
python3 auth_proxy.py
```

### 5. Access Dashboard

Open browser: http://localhost:8080/

Login with credentials created in step 3.

---

## 🚀 For Production Access

### Access Live Dashboard
**URL**: http://34.132.142.58:8080/

### SSH to Production Server
```bash
ssh amitkumar@34.132.142.58
cd ~/deploy
```

### View Logs
```bash
# Server logs
tail -f ~/deploy/server.log

# Auth logs
tail -f ~/deploy/audit.log

# System logs
sudo journalctl -u ws_server -f
sudo journalctl -u auth_proxy -f
```

### Restart Services
```bash
sudo systemctl restart ws_server
sudo systemctl restart auth_proxy
```

---

## 📚 Key Files to Know

| File | Purpose |
|------|---------|
| `auth_proxy.py` | Main auth server (port 8080) |
| `ws_server.py` | WebSocket backend (port 8081) |
| `db.py` | Database layer |
| `security.py` | Security features |
| `dashboard_live.html` | Main dashboard UI |
| `config.env` | Environment variables |
| `auth_config.json` | Auth configuration |

---

## 🔑 Default Credentials

**⚠️ Change these immediately after first login!**

Check with admin or create new user via `setup_auth.py`

---

## 🐛 Common Issues

### "Port 8080 already in use"
```bash
lsof -i :8080
kill -9 <PID>
```

### "Database locked"
```bash
pkill -f auth_proxy.py
rm quantra.db-wal quantra.db-shm  # If exists
```

### "Module not found"
```bash
pip install -r requirements.txt
```

### "WebSocket connection failed"
- Ensure `ws_server.py` is running on port 8081
- Check firewall settings

---

## 📖 Next Steps

1. Read full [README.md](README.md)
2. Review [DEPLOYMENT.md](DEPLOYMENT.md) for production deployment
3. Check code documentation in Python files
4. Test paper trading features
5. Configure AI chat (optional)

---

## 💡 Tips

- Use `venv` for isolated Python environment
- Never commit `config.env` or `*.db` files
- Test locally before deploying to production
- Monitor logs during development
- Use browser DevTools to debug WebSocket issues

---

**Need Help?** Check the full README.md or contact the team.
