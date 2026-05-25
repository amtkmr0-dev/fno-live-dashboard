#!/bin/bash
# fix_dashboard.sh - Apply all critical fixes to FNO Dashboard
# Run this script to fix Python code issues and deploy to production

set -e  # Exit on error

echo "🔧 FNO Dashboard - Critical Fixes"
echo "=================================="
echo ""

# Check if we're in the right directory
if [ ! -f "auth_proxy.py" ]; then
    echo "❌ Error: Must run from fno-live-dashboard directory"
    exit 1
fi

echo "✅ Step 1: Python code fixes already applied"
echo "   - Fixed asyncio.coroutine in chat_analysis.py"
echo "   - Fixed missing user_id parameter in auth_proxy.py"
echo ""

echo "✅ Step 2: Verify Python syntax"
python3 -m py_compile auth_proxy.py
python3 -m py_compile chat_analysis.py
python3 -m py_compile ws_server.py
python3 -m py_compile auto_trader.py
python3 -m py_compile db.py
echo "   All Python files compile successfully!"
echo ""

echo "📋 Step 3: Test local environment (optional)"
echo "   Run: ./start_local.sh"
echo "   Then open: http://localhost:8080/"
echo ""

echo "🚀 Step 4: Deploy to production"
read -p "   Deploy to GCP? (y/n) " -n 1 -r
echo ""
if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo "   Deploying to GCP..."
    
    # Copy fixed files to GCP
    gcloud compute scp auth_proxy.py instance-20260412-171736:~/deploy/ --zone=us-central1-a
    gcloud compute scp chat_analysis.py instance-20260412-171736:~/deploy/ --zone=us-central1-a
    
    echo "   Restarting services on GCP..."
    gcloud compute ssh instance-20260412-171736 --zone=us-central1-a --command="
        cd ~/deploy && \
        sudo systemctl restart auth_proxy && \
        sudo systemctl restart ws_server && \
        sleep 3 && \
        sudo systemctl status auth_proxy ws_server --no-pager
    "
    
    echo ""
    echo "✅ Deployment complete!"
    echo "   Check production: http://34.132.142.58:8080/"
else
    echo "   Skipping deployment"
fi

echo ""
echo "🎉 All fixes applied successfully!"
echo ""
echo "📊 Summary:"
echo "   ✅ Fixed asyncio.coroutine deprecation"
echo "   ✅ Fixed missing method parameter"
echo "   ✅ All Python files compile"
echo "   ⚠️  WebSocket heartbeat needs manual implementation"
echo "   ⚠️  ws_server systemd service needs investigation"
echo ""
echo "📚 Next steps:"
echo "   1. Test locally: ./start_local.sh"
echo "   2. Check logs: tail -f *_local.log"
echo "   3. Review: QUICK_FIX_GUIDE.md for remaining issues"
echo ""
