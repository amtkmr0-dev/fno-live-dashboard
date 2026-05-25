import asyncio, aiohttp, json
from datetime import datetime, timedelta

async def main():
    token = 'eyJ0eXAiOiJKV1QiLCJrZXlfaWQiOiJza192MS4wIiwiYWxnIjoiSFMyNTYifQ.eyJzdWIiOiIySEE4TUciLCJqdGkiOiI2YTBlNjMxZjIzY2QxODE1MzYzMGQ0YmMiLCJpc011bHRpQ2xpZW50IjpmYWxzZSwiaXNQbHVzUGxhbiI6ZmFsc2UsImlhdCI6MTc3OTMyNzc3NSwiaXNzIjoidWRhcGktZ2F0ZXdheS1zZXJ2aWNlIiwiZXhwIjoxNzc5NDAwODAwfQ.YoWN0atC_NK5jTz1DJ09KDflD0nRqps9UbI52aepxx8'
    ikey = 'NSE_EQ|INE466L01038'
    encoded = ikey.replace('|', '%7C')
    to_date = datetime.now().strftime('%Y-%m-%d')
    from_date = (datetime.now() - timedelta(days=12)).strftime('%Y-%m-%d')
    url = f'https://api.upstox.com/v2/historical-candle/{encoded}/day/{to_date}/{from_date}'
    async with aiohttp.ClientSession() as s:
        async with s.get(url, headers={'Authorization': f'Bearer {token}', 'Accept': 'application/json'}, timeout=aiohttp.ClientTimeout(total=10)) as r:
            print('Status:', r.status)
            text = await r.text()
            print('Body:', text[:400])
            try:
                d = json.loads(text)
                candles = (d.get('data') or {}).get('candles', [])
                print('Candles count:', len(candles))
                if candles: print('First:', candles[0])
            except Exception as e:
                print('JSON parse error:', e)

asyncio.run(main())
