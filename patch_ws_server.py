#!/usr/bin/env python3
"""
patch_ws_server.py — Add /rsi and /rsi-analysis routes to ws_server.py
Run this in ~/deploy/ on the GCP server.
"""
import re

path = "ws_server.py"
code = open(path).read()

# Check if routes already exist
if "handle_rsi_page" in code:
    print("Routes already exist — skipping")
    exit(0)

# 1. Add handler methods after handle_admin_page
handler_code = """
    async def handle_rsi_page(self, request: web.Request) -> web.Response:
        \"\"\"Serve the RSI Scanner page.\"\"\"
        html_path = Path(__file__).parent / "rsi.html"
        if not html_path.exists():
            return web.json_response({"error": "File not found"})
        return web.FileResponse(html_path)

    async def handle_rsi_analysis_page(self, request: web.Request) -> web.Response:
        \"\"\"Serve the RSI Stock Analysis page.\"\"\"
        html_path = Path(__file__).parent / "rsi-analysis.html"
        if not html_path.exists():
            return web.json_response({"error": "File not found"})
        return web.FileResponse(html_path)
"""

# Insert after handle_admin_page method
# Find the end of handle_admin_page (the next method starts with "    async def handle_admin_status")
marker = "    async def handle_admin_status"
if marker in code:
    code = code.replace(marker, handler_code + "\n" + marker)
    print("✓ Handler methods added")
else:
    print("✗ Could not find insertion point for handlers")
    exit(1)

# 2. Add routes in create_app after sectors route
route_code = """
        # RSI pages
        app.router.add_get("/rsi", self.handle_rsi_page)
        app.router.add_get("/rsi-analysis", self.handle_rsi_analysis_page)
"""

# Insert after the sectors route line
sectors_route = '        app.router.add_get("/sectors", self.handle_sectors_page)'
if sectors_route in code:
    code = code.replace(sectors_route, sectors_route + "\n" + route_code)
    print("✓ Routes added")
else:
    print("✗ Could not find sectors route for insertion")
    exit(1)

# Write back
open(path, "w").write(code)
print(f"✓ ws_server.py patched ({len(code)} bytes)")
