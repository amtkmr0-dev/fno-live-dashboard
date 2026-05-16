#!/usr/bin/env python3
"""
Patch ws_server.py to add charset=utf-8 middleware.
This fixes ALL junk/garbled characters (₹, ▲, ▼, —, etc).
Run on GCP server: python3 patch_charset.py
"""

import re

SERVER_FILE = "ws_server.py"
html = open(SERVER_FILE, 'r').read()

# Check if already patched
if 'charset_middleware' in html:
    print("Already patched!")
    exit(0)

# 1. Add the middleware import/definition after the existing imports
# Find the 'from aiohttp import web' line
import_section_end = html.find('\nclass ')
if import_section_end < 0:
    import_section_end = html.find('\ndef ')

middleware_code = """

# ── Charset middleware (fixes garbled ₹, ▲, ▼ characters) ──
@web.middleware
async def charset_middleware(request, handler):
    response = await handler(request)
    if hasattr(response, 'content_type') and response.content_type == 'text/html':
        response.charset = 'utf-8'
    return response

"""

html = html[:import_section_end] + middleware_code + html[import_section_end:]

# 2. Add middleware to web.Application() call
# Find: web.Application(
html = re.sub(
    r'web\.Application\(\)',
    'web.Application(middlewares=[charset_middleware])',
    html
)
# Also handle if it already has some args
html = re.sub(
    r'web\.Application\((?!middlewares)([^)]+)\)',
    'web.Application(middlewares=[charset_middleware], \\1)',
    html
)

open(SERVER_FILE, 'w').write(html)
print(f"✓ Patched {SERVER_FILE} ({len(html)} bytes)")
print("  Added charset_middleware for Content-Type: text/html; charset=utf-8")
