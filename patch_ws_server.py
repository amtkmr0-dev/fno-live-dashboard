import sys

with open('ws_server.py', 'r') as f:
    content = f.read()

# Add the handler function right after handle_api_chain
if "async def handle_api_advanced_chain" not in content:
    handler_code = """
    async def handle_api_advanced_chain(self, request):
        \"\"\"GET /api/advanced-chain?symbol=XYZ
        Returns a sorted list of up to 15 strikes (ATM, ATM-7 to ATM+7)
        with their CE/PE OI, LTP, IV, Volume, derived directly from the cached strike_map.
        \"\"\"
        from aiohttp import web
        symbol = request.query.get("symbol", "").upper().strip()
        if not symbol:
            return web.json_response({"error": "symbol query param is required"}, status=400)
            
        st = self.state.get(symbol)
        if not st or "strike_map" not in st or not st.get("atm_strike"):
            return web.json_response({"error": "Chain data not available yet"}, status=404)
            
        atm_strike = float(st["atm_strike"])
        strike_map = st["strike_map"]
        
        all_strikes = sorted([float(k) for k in strike_map.keys()])
        if not all_strikes:
            return web.json_response({"error": "No strikes in strike map"}, status=404)
            
        atm_idx = min(range(len(all_strikes)), key=lambda i: abs(all_strikes[i] - atm_strike))
        
        start_idx = max(0, atm_idx - 7)
        end_idx = min(len(all_strikes), atm_idx + 8)
        selected_strikes = all_strikes[start_idx:end_idx]
        
        result = []
        for s in selected_strikes:
            data = strike_map[str(s)]
            ce = data.get("CE", {})
            pe = data.get("PE", {})
            result.append({
                "strikePrice": s,
                "is_atm": (s == all_strikes[atm_idx]),
                "CE": {"openInterest": ce.get("openInterest", 0), "lastPrice": ce.get("lastPrice", 0), "totalTradedVolume": ce.get("totalTradedVolume", 0), "impliedVolatility": ce.get("impliedVolatility", 0)},
                "PE": {"openInterest": pe.get("openInterest", 0), "lastPrice": pe.get("lastPrice", 0), "totalTradedVolume": pe.get("totalTradedVolume", 0), "impliedVolatility": pe.get("impliedVolatility", 0)}
            })
            
        return web.json_response({"records": {"data": result}, "timestamp": st.get("timestamp", "")})
"""
    content = content.replace('    async def handle_api_chain(self, request: web.Request) -> web.Response:', handler_code + '\n    async def handle_api_chain(self, request: web.Request) -> web.Response:')

# Add the route
if 'app.router.add_get("/api/advanced-chain"' not in content:
    content = content.replace('app.router.add_get("/api/chain", self.handle_api_chain)', 'app.router.add_get("/api/chain", self.handle_api_chain)\n        app.router.add_get("/api/advanced-chain", self.handle_api_advanced_chain)')

with open('ws_server.py', 'w') as f:
    f.write(content)
