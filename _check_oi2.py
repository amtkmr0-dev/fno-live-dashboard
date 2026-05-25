#!/usr/bin/env python3
"""Side-by-side check: compute analytics from raw chain ourselves and compare to ws_server's stored values."""
import json, urllib.request, asyncio, aiohttp

SYMBOLS = ['RELIANCE','TCS','INFY','HDFCBANK','ICICIBANK','SBIN']

state = json.load(urllib.request.urlopen('http://localhost:8081/api/state', timeout=5))
stocks = state.get('stocks', {})

TOKEN = None
with open('/home/amitkumar/deploy/config.env') as f:
    for line in f:
        line = line.strip()
        if line.startswith('UPSTOX_ACCESS_TOKEN'):
            TOKEN = line.split('=',1)[1].strip().strip('"').strip("'")
            break


async def fetch_chain(s, ikey, expiry):
    encoded = ikey.replace('|', '%7C')
    url = f'https://api.upstox.com/v2/option/chain?instrument_key={encoded}&expiry_date={expiry}'
    async with s.get(url, headers={'Authorization': f'Bearer {TOKEN}', 'Accept': 'application/json'},
                     timeout=aiohttp.ClientTimeout(total=15)) as r:
        if r.status != 200:
            return None
        return await r.json()


def analyze(chain_data, spot):
    """Apply IDENTICAL logic to ws_server's analyze_chain."""
    if not chain_data: return None
    total_ce_oi = total_pe_oi = 0
    total_ce_oi_chg = total_pe_oi_chg = 0
    n_strikes = 0
    strikes = []
    for item in chain_data:
        sp = item.get('strike_price', 0)
        if not sp: continue
        n_strikes += 1
        ce = (item.get('call_options') or {}).get('market_data') or {}
        pe = (item.get('put_options') or {}).get('market_data') or {}
        ce_oi = ce.get('oi', 0) or 0
        ce_prev = ce.get('prev_oi', 0) or 0
        pe_oi = pe.get('oi', 0) or 0
        pe_prev = pe.get('prev_oi', 0) or 0
        total_ce_oi += ce_oi
        total_pe_oi += pe_oi
        total_ce_oi_chg += (ce_oi - ce_prev)
        total_pe_oi_chg += (pe_oi - pe_prev)
        strikes.append({'strike': sp, 'ce_oi': ce_oi, 'pe_oi': pe_oi})

    # Max pain — same algorithm as ws_server.compute_max_pain
    best = None
    best_pain = float('inf')
    all_k = [s['strike'] for s in strikes]
    for k in all_k:
        pain = 0
        for s in strikes:
            si = s['strike']
            # CE writers lose if expiry > strike
            if k > si:
                pain += s['ce_oi'] * (k - si)
            # PE writers lose if expiry < strike
            if k < si:
                pain += s['pe_oi'] * (si - k)
        if pain < best_pain:
            best_pain = pain
            best = k

    return {
        'n_strikes': n_strikes,
        'total_ce_oi': total_ce_oi,
        'total_pe_oi': total_pe_oi,
        'total_oi': total_ce_oi + total_pe_oi,
        'ce_oi_chg': total_ce_oi_chg,
        'pe_oi_chg': total_pe_oi_chg,
        'pcr': round(total_pe_oi/total_ce_oi, 2) if total_ce_oi else 0,
        'max_pain': best,
    }


async def main():
    async with aiohttp.ClientSession() as s:
        for sym in SYMBOLS:
            st = stocks.get(sym)
            if not st: continue
            data = await fetch_chain(s, st['ikey'], st['expiry'])
            if not data: print(f'{sym}: chain fetch failed'); continue
            t = analyze(data.get('data', []), st.get('ltp'))
            if not t: continue

            print(f'\n=== {sym} (ltp={st.get("ltp")}, exp={st["expiry"]}, n_strikes={t["n_strikes"]}) ===')
            print(f'  PCR        server={st.get("pcr")}     truth={t["pcr"]}')
            print(f'  CE OI Chg  server={int(st.get("ce_oi_chg") or 0):>14,}  truth={int(t["ce_oi_chg"]):>14,}  diff={int((st.get("ce_oi_chg") or 0)-t["ce_oi_chg"]):>+12,}')
            print(f'  PE OI Chg  server={int(st.get("pe_oi_chg") or 0):>14,}  truth={int(t["pe_oi_chg"]):>14,}  diff={int((st.get("pe_oi_chg") or 0)-t["pe_oi_chg"]):>+12,}')
            print(f'  Max Pain   server={st.get("max_pain")}     truth={t["max_pain"]}')
            print(f'  Total OI   truth={t["total_oi"]:>12,}  (CE={t["total_ce_oi"]:,}, PE={t["total_pe_oi"]:,})')

asyncio.run(main())
