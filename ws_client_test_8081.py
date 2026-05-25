import asyncio
import websockets
import json

async def test_ws():
    uri = "ws://localhost:8081/ws"
    try:
        async with websockets.connect(uri) as websocket:
            msg = await websocket.recv()
            data = json.loads(msg)
            if data.get('type') == 'init':
                stocks = data.get('stocks', {})
                if 'RELIANCE' in stocks:
                    print(f"RELIANCE ltp: {stocks['RELIANCE'].get('ltp')}")
                    print(f"RELIANCE prev_close: {stocks['RELIANCE'].get('prev_close')}")
                    print(f"RELIANCE chg_pct: {stocks['RELIANCE'].get('chg_pct')}")
                if 'FORCEMOT' in stocks:
                    print(f"FORCEMOT ltp: {stocks['FORCEMOT'].get('ltp')}")
                    print(f"FORCEMOT prev_close: {stocks['FORCEMOT'].get('prev_close')}")
                    print(f"FORCEMOT chg_pct: {stocks['FORCEMOT'].get('chg_pct')}")
    except Exception as e:
        print(f"Error: {e}")

asyncio.run(test_ws())
