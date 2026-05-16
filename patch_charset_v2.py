#!/usr/bin/env python3
"""
Patch ws_server.py to add charset=utf-8 to ALL HTML responses.

The v1 middleware approach doesn't work with aiohttp's FileResponse (static files).
This v2 uses on_response_prepare signal which fires for ALL response types.

Run on GCP server: cd ~/deploy && python3 patch_charset_v2.py
"""

import re

SERVER_FILE = "ws_server.py"
html = open(SERVER_FILE, 'r').read()

# Remove old v1 middleware if present
if 'charset_middleware' in html:
    print("Removing old v1 charset_middleware...")
    # Remove the middleware function definition
    html = re.sub(
        r'\n# ── Charset middleware.*?async def charset_middleware.*?return response\n',
        '\n',
        html, flags=re.DOTALL
    )
    # Remove from middlewares= arg
    html = re.sub(r'middlewares=\[charset_middleware\],?\s*', '', html)
    html = re.sub(r',\s*middlewares=\[charset_middleware\]', '', html)
    print("  Old middleware removed.")

# Check if v2 already applied
if 'charset_signal' in html or 'on_response_prepare' in html:
    print("Already has response_prepare signal, skipping.")
    open(SERVER_FILE, 'w').write(html)
    exit(0)

# Strategy: Find where the app is created (web.Application) and add the signal right after.
# Pattern: app = web.Application(...)
app_match = re.search(r'(app\s*=\s*web\.Application\([^)]*\))', html)
if not app_match:
    print("ERROR: Could not find 'app = web.Application(...)' in ws_server.py")
    exit(1)

app_line_end = app_match.end()
print(f"Found app creation at position {app_match.start()}-{app_line_end}")

# Insert the signal handler right after app creation
signal_code = """

# ── Charset fix: force UTF-8 on all HTML responses (including FileResponse) ──
async def charset_signal(request, response):
    ct = response.headers.get('Content-Type', '')
    if ct.startswith('text/html') and 'charset' not in ct:
        response.headers['Content-Type'] = 'text/html; charset=utf-8'

app.on_response_prepare.append(charset_signal)
"""

html = html[:app_line_end] + signal_code + html[app_line_end:]

open(SERVER_FILE, 'w').write(html)
print(f"Patched {SERVER_FILE} ({len(html)} bytes)")
print("  Added on_response_prepare signal for charset=utf-8")
print("  This works with FileResponse, Response, and all other response types.")
