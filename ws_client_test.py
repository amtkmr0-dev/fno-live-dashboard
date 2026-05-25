import asyncio
import websockets
import json

async def test_ws():
    uri = "ws://localhost:8080/ws"
    try:
        async with websockets.connect(uri) as websocket:
            msg = await websocket.recv()
            data = json.loads(msg)
            print(f"Received msg type: {data.get('type')}")
            if data.get('type') == 'init':
                stocks = data.get('stocks', {})
                print(f"Number of stocks: {len(stocks)}")
                if 'RELIANCE' in stocks:
                    print(f"RELIANCE data keys: {list(stocks['RELIANCE'].keys())}")
                    print(f"RELIANCE ltp: {stocks['RELIANCE'].get('ltp')}")
                    print(f"RELIANCE chg_pct: {stocks['RELIANCE'].get('chg_pct')}")
                elif len(stocks) > 0:
                    first = list(stocks.keys())[0]
                    print(f"{first} data keys: {list(stocks[first].keys())}")
                    print(f"{first} ltp: {stocks[first].get('ltp')}")
                    print(f"{first} chg_pct: {stocks[first].get('chg_pct')}")
    except Exception as e:
        print(f"Error: {e}")

asyncio.run(test_ws())
