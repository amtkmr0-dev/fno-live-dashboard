#!/usr/bin/env python3
"""Verify live dashboard data vs Upstox source of truth."""
import json, urllib.request, sys
from datetime import datetime, timedelta

SYMBOLS = ['RELIANCE','TCS','INFY','HDFCBANK','ICICIBANK','SBIN',
           'TATAMOTORS','ITC','BHARTIARTL','LT','MARUTI','SUNPHARMA']

# Pull from running ws_server on localhost
state_url = 'http://localhost:8081/api/state'
try:
    state = json.load(urllib.request.urlopen(state_url, timeout=5))
    stocks = state.get('stocks', {})
except Exception as e:
    print(f'ERROR fetching /api/state: {e}'); sys.exit(1)

# Get token to verify against Upstox
TOKEN = None
with open('/home/amitkumar/deploy/config.env') as f:
    for line in f:
        line = line.strip()
        if line.startswith('UPSTOX_ACCESS_TOKEN'):
            TOKEN = line.split('=',1)[1].strip().strip('"').strip("'")
            break

print(f'\n=== DASHBOARD STATE (live from ws_server) ===\n')
print(f'{"Symbol":12s} {"LTP":>9}  {"Vol":>11}  {"Avg5dVol":>11}  {"Surge":>6}  {"TotOI":>11}  {"CE_OI_Chg":>11}  {"PE_OI_Chg":>11}  {"PCR":>5}')
print('-' * 130)

def fmt(v, w, fmt_str=''):
    if v is None: return f'{"None":>{w}}'
    if fmt_str: return f'{v:{fmt_str}}'.rjust(w)
    return str(v).rjust(w)

for sym in SYMBOLS:
    s = stocks.get(sym)
    if not s:
        print(f'{sym:12s}  NOT IN STATE'); continue
    print(f'{sym:12s}  {fmt(s.get("ltp"), 9)}  {fmt(s.get("vol"), 11)}  {fmt(s.get("avg5d_vol"), 11)}  {fmt(s.get("vol_surge"), 6)}  {fmt(s.get("total_oi"), 11)}  {fmt(s.get("ce_oi_chg"), 11)}  {fmt(s.get("pe_oi_chg"), 11)}  {fmt(s.get("pcr"), 5)}')

# Cross-check the first 3 symbols against Upstox historical candles
print(f'\n=== UPSTOX 5-DAY HISTORICAL VOLUMES (source of truth) ===\n')
import asyncio, aiohttp

async def check_one(s, sym, ikey):
    encoded = ikey.replace('|','%7C')
    to_date = datetime.now().strftime('%Y-%m-%d')
    from_date = (datetime.now() - timedelta(days=12)).strftime('%Y-%m-%d')
    url = f'https://api.upstox.com/v2/historical-candle/{encoded}/day/{to_date}/{from_date}'
    async with s.get(url, headers={'Authorization': f'Bearer {TOKEN}', 'Accept':'application/json'},
                     timeout=aiohttp.ClientTimeout(total=10)) as r:
        if r.status != 200: return f'HTTP {r.status}'
        d = await r.json()
        candles = (d.get('data') or {}).get('candles', [])
        # Take last 5 trading days (skip today if present)
        return [(c[0][:10], c[5]) for c in candles[:6]]

async def main():
    async with aiohttp.ClientSession() as s:
        for sym in SYMBOLS[:6]:
            st = stocks.get(sym, {})
            ikey = st.get('ikey')
            if not ikey:
                print(f'{sym:12s}  (no ikey)'); continue
            res = await check_one(s, sym, ikey)
            if isinstance(res, list):
                vols = [v for _,v in res[:5]]
                avg = sum(vols)//len(vols) if vols else 0
                # Compare with what server has
                server_avg = st.get('avg5d_vol', 0)
                match = '✓ MATCH' if abs(avg - (server_avg or 0)) <= 1 else f'✗ MISMATCH (server has {server_avg})'
                print(f'{sym:12s}  candles: {res}')
                print(f'{"":12s}  upstox_avg5d={avg:>11}  server_avg5d={server_avg:>11}  {match}')

                # Also verify today's vol vs surge
                today_vol = st.get('vol') or 0
                expected_surge = round(today_vol/avg, 2) if avg else 0
                actual_surge = st.get('vol_surge')
                surge_match = '✓' if abs((actual_surge or 0) - expected_surge) < 0.05 else '✗'
                print(f'{"":12s}  today_vol={today_vol:>11}  expected_surge={expected_surge}  actual_surge={actual_surge}  {surge_match}\n')

asyncio.run(main())
