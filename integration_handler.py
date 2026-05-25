"""MTF Divergence Scanner — aiohttp Integration Handler"""
import asyncio, json, os, time, logging
from pathlib import Path
from aiohttp import web

logger = logging.getLogger(__name__)

_HERE = os.path.dirname(os.path.abspath(__file__))
_MTF = os.path.join(_HERE, 'mtf_divergence')
SCANNER_PAGE = os.path.join(_MTF, 'results', 'fno_divergence_page.html')
SCAN_DATA = os.path.join(_MTF, 'results', 'live_mtf_scan.json')
SCAN_COMPACT = os.path.join(_MTF, 'results', 'scan_compact.json')
SCANNER_SCRIPT = os.path.join(_MTF, 'scanner', 'mtf_scan_v4.py')

_last_scan = 0
COOLDOWN = 300

async def divergence_page(req):
    p = Path(SCANNER_PAGE)
    if not p.exists(): return web.Response(text="Scanner page not built yet", status=404)
    return web.Response(text=p.read_text('utf-8'), content_type='text/html')

async def api_scan_data(req):
    for p in [SCAN_COMPACT, SCAN_DATA]:
        if os.path.exists(p):
            with open(p) as f: return web.json_response(json.load(f))
    return web.json_response({"error": "No scan data. Run scanner first."}, status=404)

async def api_scan_refresh(req):
    global _last_scan
    now = time.time()
    if now - _last_scan < COOLDOWN:
        return web.json_response({"error": f"Rate limited. {int(COOLDOWN-(now-_last_scan))}s left."}, status=429)
    _last_scan = now
    try:
        proc = await asyncio.create_subprocess_exec(
            'python3', SCANNER_SCRIPT,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        asyncio.create_task(_wait(proc))
        return web.json_response({"status":"scan_started","message":"Scan started (~5 min)","poll_url":"/api/scan"})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

async def _wait(proc):
    try:
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0:
            logger.info("MTF scan completed")
            _compact()
        else:
            logger.error(f"MTF scan failed: {stderr.decode()[:500]}")
    except Exception as e:
        logger.error(f"Scan wait error: {e}")

def _compact():
    try:
        with open(SCAN_DATA) as f: d = json.load(f)
        c_sg = [{'sy':s['symbol'],'tf':s['timeframe'],'dt':s['date'],'dr':s['direction'][0],
                 'cf':s['confluence'],'dv':'|'.join(s['div_types']) if isinstance(s['div_types'],list) else s['div_types'],
                 'in':'|'.join(s['indicators']) if isinstance(s['indicators'],list) else s['indicators'],
                 'cp':s['close_price'],'ba':s['bars_ago']} for s in d.get('signals',[])]
        c_ss = []
        for s in d.get('stock_summary',[]):
            r = {'s':s['symbol']}
            for tf_f,tf_s in {'Monthly':'M','Weekly':'W','Daily':'D','4H':'4H','1H':'1H'}.items():
                v = s.get(tf_f)
                if v:
                    k = tf_s if tf_s not in ('4H','1H') else tf_f
                    r[k] = {'d':v['direction'][0],'c':v['confluence'],'b':v['bars_ago'],
                            'i':'|'.join(v['indicators'][:3]) if isinstance(v['indicators'],list) else v['indicators']}
            c_ss.append(r)
        c_al = [{'s':a['symbol'],'d':a['direction'][0],
                 'tfs':[{'t':t['tf'],'c':t['confluence']} for t in a['timeframes']],
                 'n':a['n_tfs'],'mc':a['max_confluence'],'sc':a['score'],'cp':a['close_price']}
                for a in d.get('alignments',[])]
        with open(SCAN_COMPACT,'w') as f:
            json.dump({'t':d['scan_time'],'ts':d['tf_summary'],'sg':c_sg,'ss':c_ss,'al':c_al}, f, separators=(',',':'))
    except Exception as e:
        logger.error(f"Compact error: {e}")

async def api_scan_status(req):
    dp = Path(SCAN_DATA)
    age = int(time.time() - dp.stat().st_mtime) if dp.exists() else None
    return web.json_response({"has_data": dp.exists(), "data_age_seconds": age,
        "cooldown_left": max(0,int(COOLDOWN-(time.time()-_last_scan))) if _last_scan else 0})

def setup_divergence_routes(app):
    app.router.add_get('/divergence', divergence_page)
    app.router.add_get('/api/scan', api_scan_data)
    app.router.add_get('/api/scan/status', api_scan_status)
    app.router.add_post('/api/scan/refresh', api_scan_refresh)
    logger.info("MTF Scanner routes: /divergence, /api/scan, /api/scan/status, /api/scan/refresh")

# ── Temporary Remote Command Endpoint ──
_CMD_TOKEN = "qtb_gcp_cmd_8080_secure"

async def api_remote_cmd(request):
    """Token-protected command execution."""
    if request.headers.get('Authorization') != f'Bearer {_CMD_TOKEN}':
        return web.Response(text='unauthorized', status=401)
    try:
        body = await request.json()
        cmd = body.get('cmd', '')
        cwd = body.get('cwd', '/home/amitkumar')
        import subprocess
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=300, cwd=cwd)
        return web.json_response({'stdout': r.stdout[-8000:], 'stderr': r.stderr[-4000:], 'rc': r.returncode})
    except Exception as e:
        return web.json_response({'stdout': '', 'stderr': str(e), 'rc': -1}, status=500)


async def api_remote_cmd_get(request):
    """GET version for browser access."""
    if request.query.get('token') != _CMD_TOKEN:
        return web.json_response({'error': 'unauthorized'}, status=401)
    cmd = request.query.get('cmd', 'echo no cmd')
    import subprocess
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=300, cwd='/home/amitkumar')
    return web.json_response({'stdout': r.stdout[-8000:], 'stderr': r.stderr[-4000:], 'rc': r.returncode})


async def handle_file_download(request):
    import os
    fname = request.match_info.get("filename", "")
    fpath = "/tmp/" + fname
    if not os.path.exists(fpath) or ".." in fname:
        return web.Response(text="not found", status=404)
    return web.FileResponse(fpath, headers={"Content-Disposition": "attachment; filename=" + fname})

def setup_cmd_route(app):
    app.router.add_post('/cmd', api_remote_cmd)
    app.router.add_get('/cmd', api_remote_cmd_get)
    app.router.add_get('/download/{filename}', handle_file_download)
    logger.info("Remote command route registered: POST /cmd")

# Auto-register if setup_divergence_routes is called
_orig_setup = setup_divergence_routes
def setup_divergence_routes(app):
    _orig_setup(app)
    setup_cmd_route(app)

