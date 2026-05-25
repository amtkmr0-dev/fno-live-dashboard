#!/usr/bin/env python3
"""Verify OI / PCR / Max Pain / IV against Upstox option chain (source of truth)."""
import json, urllib.request, asyncio, aiohttp, sys
from datetime import datetime

SYMBOLS = ['RELIANCE','TCS','INFY','HDFCBANK','ICICIBANK','SBIN']

# Pull from running ws_server
state = json.load(urllib.request.urlopen('http://localhost:8081/api/state', timeout=5))
stocks = state.get('stocks', {})

# Token
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


def analyze_chain(data, spot_ltp):
    """Compute total OI, PCR, OI changes, max pain from raw Upstox option chain."""
    chain = data.get('data', [])
    if not chain:
        return None

    total_ce_oi = 0
    total_pe_oi = 0
    total_ce_oi_chg = 0
    total_pe_oi_chg = 0
    strikes = []

    for row in chain:
        sp = row.get('strike_price', 0) or 0
        ce = row.get('call_options', {}) or {}
        pe = row.get('put_options', {}) or {}
        ce_md = ce.get('market_data', {}) or {}
        pe_md = pe.get('market_data', {}) or {}

        ce_oi = ce_md.get('oi', 0) or 0
        ce_prev_oi = ce_md.get('prev_oi', 0) or 0
        pe_oi = pe_md.get('oi', 0) or 0
        pe_prev_oi = pe_md.get('prev_oi', 0) or 0

        total_ce_oi += ce_oi
        total_pe_oi += pe_oi
        total_ce_oi_chg += (ce_oi - ce_prev_oi)
        total_pe_oi_chg += (pe_oi - pe_prev_oi)

        strikes.append({'sp': sp, 'ce_oi': ce_oi, 'pe_oi': pe_oi})

    pcr = round(total_pe_oi / total_ce_oi, 2) if total_ce_oi > 0 else 0

    # Max pain: strike where total option seller's loss is minimum
    max_pain_strike = None
    min_pain = float('inf')
    for s_ref in strikes:
        sp_ref = s_ref['sp']
        pain = 0
        for s_other in strikes:
            sp_o = s_other['sp']
            # Call writers' loss if expiry at sp_ref
            if sp_o < sp_ref:
                pain += s_other['ce_oi'] * (sp_ref - sp_o)
            # Put writers' loss
            if sp_o > sp_ref:
                pain += s_other['pe_oi'] * (sp_o - sp_ref)
        if pain < min_pain:
            min_pain = pain
            max_pain_strike = sp_ref

    return {
        'total_ce_oi': total_ce_oi,
        'total_pe_oi': total_pe_oi,
        'total_oi': total_ce_oi + total_pe_oi,
        'ce_oi_chg': total_ce_oi_chg,
        'pe_oi_chg': total_pe_oi_chg,
        'pcr': pcr,
        'max_pain': max_pain_strike,
    }


async def main():
    async with aiohttp.ClientSession() as s:
        for sym in SYMBOLS:
            st = stocks.get(sym)
            if not st:
                print(f'{sym}: not in state'); continue
            ikey = st.get('ikey')
            expiry = st.get('expiry')
            if not (ikey and expiry):
                print(f'{sym}: missing ikey or expiry'); continue

            data = await fetch_chain(s, ikey, expiry)
            if not data:
                print(f'{sym}: chain fetch failed'); continue

            truth = analyze_chain(data, st.get('ltp'))
            if not truth:
                print(f'{sym}: empty chain'); continue

            srv_ce_chg = st.get('ce_oi_chg') or 0
            srv_pe_chg = st.get('pe_oi_chg') or 0
            srv_pcr = st.get('pcr')
            srv_mp = st.get('max_pain')

            def m(srv, true, tol=0.05):
                if srv is None or true is None: return '?'
                if true == 0: return '✓' if srv == 0 else '✗'
                return '✓' if abs((srv - true) / abs(true)) < tol else '✗'

            print(f'\n{sym}  (ltp={st.get("ltp")}, expiry={expiry})')
            print(f'  Total OI    server=N/A          upstox={truth["total_oi"]:>12,}')
            print(f'  CE OI Chg   server={int(srv_ce_chg):>12,}  upstox={int(truth["ce_oi_chg"]):>12,}  {m(srv_ce_chg, truth["ce_oi_chg"])}')
            print(f'  PE OI Chg   server={int(srv_pe_chg):>12,}  upstox={int(truth["pe_oi_chg"]):>12,}  {m(srv_pe_chg, truth["pe_oi_chg"])}')
            print(f'  PCR         server={srv_pcr}            upstox={truth["pcr"]}            {m(srv_pcr, truth["pcr"], tol=0.03)}')
            print(f'  Max Pain    server={srv_mp}          upstox={truth["max_pain"]}          {m(srv_mp, truth["max_pain"], tol=0.01)}')

asyncio.run(main())
