# Deployment Guide - FNO Live Dashboard

## 🌐 Production Environment

**Server**: GCP Compute Engine  
**IP**: 34.132.142.58  
**User**: amitkumar  
**Path**: ~/deploy/  
**Live URL**: http://34.132.142.58:8080/

## 📋 Pre-Deployment Checklist

- [ ] All code changes tested locally
- [ ] Database migrations prepared (if any)
- [ ] Configuration files updated on server
- [ ] Backup current production database
- [ ] Notify users of maintenance window (if needed)

## 🚀 Deployment Steps

### 1. Connect to GCP Server
```bash
ssh amitkumar@34.132.142.58
```

### 2. Navigate to Deployment Directory
```bash
cd ~/deploy
```

### 3. Backup Current State
```bash
# Backup database
cp quantra.db quantra.db.backup.$(date +%Y%m%d_%H%M%S)

# Backup server files (if making major changes)
tar -czf backup_$(date +%Y%m%d_%H%M%S).tar.gz *.py *.html *.css
```

### 4. Pull Latest Changes
```bash
# If using git
git pull origin main

# Or manually upload files via scp from local machine:
# scp auth_proxy.py amitkumar@34.132.142.58:~/deploy/
# scp db.py amitkumar@34.132.142.58:~/deploy/
# scp security.py amitkumar@34.132.142.58:~/deploy/
```

### 5. Update Dependencies (if requirements.txt changed)
```bash
source venv/bin/activate
pip install -r requirements.txt --upgrade
```

### 6. Run Database Migrations (if needed)
```bash
python3 -c "from db import DB; db = DB('quantra.db'); db.init()"
```

### 7. Restart Services

#### Option A: Using systemd (Recommended)
```bash
# Restart WebSocket server
sudo systemctl restart ws_server

# Restart Auth Proxy
sudo systemctl restart auth_proxy

# Check status
sudo systemctl status ws_server
sudo systemctl status auth_proxy
```

#### Option B: Manual Restart
```bash
# Stop existing processes
pkill -f ws_server.py
pkill -f auth_proxy.py

# Wait for graceful shutdown
sleep 3

# Start WebSocket server (background)
nohup venv/bin/python3 ws_server.py >> server.log 2>&1 &

# Start Auth Proxy (background)
nohup venv/bin/python3 auth_proxy.py >> auth_proxy.log 2>&1 &
```

### 8. Verify Deployment
```bash
# Check if processes are running
ps aux | grep -E "(ws_server|auth_proxy)" | grep -v grep

# Check ports are listening
netstat -tlnp | grep -E "(8080|8081)"

# Test HTTP response
curl -I http://localhost:8080/

# Check logs for errors
tail -f server.log
tail -f auth_proxy.log
tail -f audit.log
```

### 9. Test from Browser
- Visit: http://34.132.142.58:8080/
- Test login functionality
- Verify WebSocket connection (check browser console)
- Test paper trade creation
- Verify AI chat (if configured)

## 🔄 Rollback Procedure

If deployment fails:

```bash
# Stop new services
sudo systemctl stop ws_server
sudo systemctl stop auth_proxy

# Restore database backup
cp quantra.db.backup.YYYYMMDD_HHMMSS quantra.db

# Restore code from backup
tar -xzf backup_YYYYMMDD_HHMMSS.tar.gz

# Restart services
sudo systemctl start ws_server
sudo systemctl start auth_proxy
```

## 📊 Monitoring

### Check Service Status
```bash
# Systemd services
sudo systemctl status ws_server
sudo systemctl status auth_proxy

# View recent logs
sudo journalctl -u ws_server -n 50 --no-pager
sudo journalctl -u auth_proxy -n 50 --no-pager
```

### Check Application Logs
```bash
# Real-time log monitoring
tail -f ~/deploy/server.log
tail -f ~/deploy/auth_proxy.log
tail -f ~/deploy/audit.log

# Search for errors
grep -i error ~/deploy/server.log | tail -20
grep -i "failed login" ~/deploy/audit.log | tail -20
```

### Check Resource Usage
```bash
# CPU and Memory
top -b -n 1 | grep -E "(python|PID)"

# Disk space
df -h

# Database size
ls -lh quantra.db
```

### Check Active Sessions
```bash
# Connect to database
sqlite3 quantra.db "SELECT COUNT(*) as active_sessions FROM sessions WHERE expires_at > datetime('now');"

# Active users
sqlite3 quantra.db "SELECT COUNT(*) as active_users FROM users WHERE is_active = 1;"
```

## 🔧 Maintenance Tasks

### Clean Up Old Sessions
```bash
python3 -c "from db import DB; db = DB('quantra.db'); print(f'Cleaned {db.cleanup_expired_sessions()} sessions')"
```

### Database Vacuum (Optimize)
```bash
sqlite3 quantra.db "VACUUM;"
```

### Rotate Logs
```bash
# Archive old logs
gzip server.log.1
gzip audit.log.1

# Truncate current logs (or use logrotate)
> server.log
> audit.log
```

## 🚨 Troubleshooting

### Port Already in Use
```bash
# Find process using port 8080
lsof -i :8080

# Kill specific process
kill -9 <PID>
```

### Database Locked
```bash
# Check for locks
fuser quantra.db

# Force close connections (last resort)
pkill -f "python.*quantra.db"
```

### WebSocket Connection Fails
1. Check `ws_server.py` is running: `ps aux | grep ws_server`
2. Check port 8081 is listening: `netstat -tlnp | grep 8081`
3. Check firewall rules: `sudo iptables -L -n | grep 8081`
4. Check browser console for WebSocket errors

### High Memory Usage
```bash
# Check memory usage
free -h

# Restart services to clear memory
sudo systemctl restart ws_server
sudo systemctl restart auth_proxy
```

## 📝 Post-Deployment

- [ ] Verify all features working
- [ ] Check error logs for issues
- [ ] Monitor performance for 30 minutes
- [ ] Update deployment log/changelog
- [ ] Notify team of successful deployment

## 🔐 Security Reminders

- Never commit `config.env` or `auth_config.json` to git
- Rotate API keys regularly
- Review audit logs weekly
- Keep dependencies updated
- Monitor failed login attempts

## 📞 Emergency Contacts

- **Server Admin**: amitkumar
- **GCP Console**: https://console.cloud.google.com/
- **Server IP**: 34.132.142.58

---

**Last Updated**: May 2026
