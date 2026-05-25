#!/usr/bin/env python3
"""Test the new Upstox PCR/MaxPain/OI/ChangeOI endpoints."""
import asyncio, aiohttp, json
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

TOKEN = None
with open('/home/amitkumar/deploy/config.env') as f:
    for line in f:
        line = line.strip()
        if line.startswith('UPSTOX_ACCESS_TOKEN'):
            TOKEN = line.split('=',1)[1].strip().strip('"').strip("'")
            break

BASE = 'https://api.upstox.com/v2/market'
TODAY = datetime.now(IST).strftime('%Y-%m-%d')
EXPIRY = '2026-05-29'  # current weekly expiry
IKEY = 'NSE_EQ|INE002A01018'  # RELIANCE

async def test(session, name, url, params):
    print(f'\n--- {name} ---')
    print(f'  URL: {url}')
    print(f'  Params: {params}')
    try:
        async with session.get(url, params=params,
                               headers={'Authorization': f'Bearer {TOKEN}', 'Accept': 'application/json'},
                               timeout=aiohttp.ClientTimeout(total=15)) as r:
            print(f'  Status: {r.status}')
            text = await r.text()
            if r.status == 200:
                d = json.loads(text)
                data = d.get('data', {})
                # Print summary, not full response
                if 'pcr' in data:
                    print(f'  PCR: {data["pcr"]}, insights: {len(data.get("insights",[]))} points')
                    if data.get('insights'):
                        print(f'  Latest: {data["insights"][-1]}')
                elif 'max_pain' in data:
                    print(f'  Max Pain: {data["max_pain"]}, insights: {len(data.get("insights",[]))} points')
                    if data.get('insights'):
                        print(f'  Latest: {data["insights"][-1]}')
                elif 'total_puts' in data:
                    print(f'  Total Puts: {data["total_puts"]}, Total Calls: {data["total_calls"]}')
                    print(f'  Strikes: {len(data.get("call_put_oi_data_list",[]))}')
                elif 'total_put_change_oi' in data:
                    print(f'  Put Change: {data["total_put_change_oi"]}, Call Change: {data["total_call_change_oi"]}')
                    print(f'  Strikes: {len(data.get("call_put_oi_data_list",[]))}')
                else:
                    print(f'  Keys: {list(data.keys())}')
            else:
                print(f'  Error: {text[:300]}')
    except Exception as e:
        print(f'  Exception: {e}')

async def main():
    async with aiohttp.ClientSession() as s:
        # PCR with 1-min bucket
        await test(s, 'PCR (1-min bucket)', f'{BASE}/pcr',
                   {'instrument_key': IKEY, 'expiry': EXPIRY, 'date': TODAY, 'bucket_interval': '1'})

        # Max Pain with 1-min bucket
        await test(s, 'Max Pain (1-min bucket)', f'{BASE}/max-pain',
                   {'instrument_key': IKEY, 'expiry': EXPIRY, 'date': TODAY, 'bucket_interval': '1'})

        # OI snapshot
        await test(s, 'OI Snapshot', f'{BASE}/oi',
                   {'instrument_key': IKEY, 'expiry': EXPIRY, 'date': TODAY})

        # Change in OI (1-day interval)
        await test(s, 'Change in OI (1-day)', f'{BASE}/change-oi',
                   {'instrument_key': IKEY, 'expiry': EXPIRY, 'date': TODAY, 'interval': '1'})

        # Also test with expiry 2026-05-26 (this week's)
        await test(s, 'PCR (expiry 05-26)', f'{BASE}/pcr',
                   {'instrument_key': IKEY, 'expiry': '2026-05-26', 'date': TODAY, 'bucket_interval': '5'})

asyncio.run(main())
