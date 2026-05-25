# 🚀 LOCAL DEVELOPMENT GUIDE

## ✅ **Setup Complete!**

Your local environment is ready to run.

---

## 📋 **Prerequisites Installed**

- ✅ Python 3.9 virtual environment
- ✅ aiohttp 3.13.5
- ✅ aiohttp-cors 0.8.1
- ✅ requests 2.32.5
- ✅ protobuf 6.33.6
- ✅ python-dotenv 1.2.1
- ✅ All dependencies installed

---

## 🔐 **Security Status**

### **Malicious IP Blocked** ✅
- **IP**: 52.13.106.180 (AWS)
- **Reason**: 2 failed admin login attempts
- **Action**: Blocked via GCP firewall rule
- **Rule Name**: `block-malicious-ip`
- **Status**: Active

**Verification**:
```bash
gcloud compute firewall-rules describe block-malicious-ip
```

---

## 🏃 **How to Run Locally**

### **Terminal 1: WebSocket Server**
```bash
cd ~/Desktop/FNO\ Dashboard/fno-live-dashboard
source venv/bin/activate
python3 ws_server.py
```

**Expected Output**:
```
[INFO] Loaded config from config.env
[INFO] Starting Quantra Terminal on port 8081
[INFO] WebSocket server ready
```

---

### **Terminal 2: Auth Proxy**
```bash
cd ~/Desktop/FNO\ Dashboard/fno-live-dashboard
source venv/bin/activate
python3 auth_proxy.py
```

**Expected Output**:
```
[AUTH] INFO Loaded config
[AUTH] INFO Database ready: X user(s)
[AUTH] INFO Starting auth proxy on port 8080
```

---

### **Access Dashboard**
Open browser: **http://localhost:8080/**

**Login Credentials**:
- Check with admin or create via `python3 setup_auth.py`

---

## 🔧 **Configuration Files**

### **config.env** (Local)
- ✅ Created with production token
- ⚠️ Token expires daily at ~03:30 IST
- 🔄 Refresh from: https://account.upstox.com/developer/apps

### **auth_config.json** (Local)
- ✅ Fetched from production
- Contains AI API keys (Perplexity/NVIDIA)
- Contains admin paths configuration

---

## 🐛 **Troubleshooting**

### **Port Already in Use**
```bash
# Check what's using port 8080/8081
lsof -i :8080
lsof -i :8081

# Kill process
kill -9 <PID>
```

### **Module Not Found**
```bash
# Reinstall dependencies
source venv/bin/activate
pip install -r requirements.txt
```

### **Database Locked**
```bash
# Remove lock files
rm quantra.db-wal quantra.db-shm
```

### **WebSocket Connection Failed**
1. Ensure ws_server.py is running on port 8081
2. Check browser console for errors
3. Verify no firewall blocking localhost

---

## 📊 **Development vs Production**

| Feature | Local | Production |
|---------|-------|------------|
| **ws_server** | Port 8081 | Port 8081 |
| **auth_proxy** | Port 8080 | Port 8080 |
| **Database** | quantra.db (local) | quantra.db (GCP) |
| **Logs** | Console output | server.log, audit.log |
| **SSL** | No (HTTP) | No (HTTP) |
| **Upstox Token** | Same as production | Auto-refreshed |

---

## 🔄 **Sync with Production**

### **Pull Latest Code**
```bash
./fetch_from_gcp.sh
```

### **Push Local Changes**
```bash
./sync_to_gcp.sh --dry-run  # Test first
./sync_to_gcp.sh            # Actually deploy
```

---

## 🧪 **Testing**

### **Test WebSocket Connection**
```bash
# In browser console
const ws = new WebSocket('ws://localhost:8081/ws');
ws.onopen = () => console.log('Connected!');
ws.onmessage = (e) => console.log('Data:', e.data);
```

### **Test API Endpoints**
```bash
# Health check
curl http://localhost:8081/api/health

# State (requires auth)
curl http://localhost:8080/api/state
```

---

## 📝 **Next Steps**

1. **Start both servers** (Terminal 1 & 2)
2. **Open dashboard** (http://localhost:8080/)
3. **Login** with your credentials
4. **Test features**:
   - Real-time data updates
   - Paper trading
   - AI chat (if configured)
   - Admin panel

---

## 🚨 **Important Notes**

- ⚠️ **Never commit** `config.env` or `auth_config.json` to git
- ⚠️ **Upstox token expires daily** - refresh each morning
- ⚠️ **Local database** is separate from production
- ⚠️ **Test thoroughly** before deploying to production

---

## 🔗 **Useful Commands**

```bash
# Activate venv
source venv/bin/activate

# Deactivate venv
deactivate

# Check Python packages
pip list

# Update requirements.txt
pip freeze > requirements.txt

# Create new admin user
python3 setup_auth.py

# Check database
sqlite3 quantra.db "SELECT * FROM users;"
```

---

**Last Updated**: May 18, 2026  
**Status**: ✅ Ready for local development  
**Security**: ✅ Malicious IP blocked
