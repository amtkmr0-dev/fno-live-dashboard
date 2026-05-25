def analyze_chain(chain_data: List[Dict[str, Any]], spot: float,
                  price_chg_pct: float,
                  pcr_bull_thr: float = 0.5,
                  pcr_bear_thr: float = 0.85,
                  lot_size: int = 1,
                  expiry: Optional[str] = None) -> Dict[str, Any]:
    """
    Analyze an option chain and return computed analytics including institutional
    market microstructure variables: Skew, INR Delta Notional Flow, GEX, Zero-Gamma.
    """
    result: Dict[str, Any] = {
        "pcr": None, "pcr_sig": "NEUTRAL", "buildup": "NEUTRAL",
        "ce_oi_chg": 0, "pe_oi_chg": 0, "net_oi": 0, "vol_oi": 0.0,
        "atm_iv": None, "max_pain": None, "mp_dist": None,
        "atm_ce": None, "atm_pe": None, "prem_ok": False, "atm_strike": None,
        "opt_vol": 0,
        "strike_map": {},  # strike -> data dict
    }

    if not chain_data or spot <= 0:
        return result

    # Automatically resolve expiry date from options data if not passed in
    if not expiry:
        for item in chain_data:
            co = item.get("call_options") or {}
            if co.get("expiry_date"):
                expiry = co.get("expiry_date")
                break
            po = item.get("put_options") or {}
            if po.get("expiry_date"):
                expiry = po.get("expiry_date")
                break

    total_ce_oi = 0
    total_pe_oi = 0
    total_ce_oi_chg = 0
    total_pe_oi_chg = 0
    total_ce_vol = 0
    total_pe_vol = 0

    strikes_for_mp: List[Dict[str, Any]] = []

    # 1. Pre-pass to find ATM strike
    atm_strike = None
    atm_dist = float("inf")
    sorted_strikes = sorted([float(x.get("strike_price", 0)) for x in chain_data if x.get("strike_price", 0)])
    
    for strike in sorted_strikes:
        dist = abs(strike - spot)
        if dist < atm_dist:
            atm_dist = dist
            atm_strike = strike
            
    atm_index = -1
    if atm_strike is not None:
        try:
            atm_index = sorted_strikes.index(atm_strike)
        except ValueError:
            pass

    atm_ce_ltp = None
    atm_pe_ltp = None
    atm_ce_iv = None
    atm_pe_iv = None

    for item in chain_data:
        strike_price = item.get("strike_price", 0)
        if not strike_price:
            continue

        # Call side
        call_data = item.get("call_options") or {}
        call_md = call_data.get("market_data") or {}
        call_greeks = call_data.get("option_greeks") or {}
        ce_oi = call_md.get("oi", 0) or 0
        ce_prev_oi = call_md.get("prev_oi", 0) or 0
        ce_vol = call_md.get("volume", 0) or 0
        ce_ltp = call_md.get("ltp", 0) or 0
        try:
            ce_iv = float(call_greeks.get("iv", 0) or call_md.get("iv", 0) or 0)
        except (ValueError, TypeError):
            ce_iv = 0.0

        # Put side
        put_data = item.get("put_options") or {}
        put_md = put_data.get("market_data") or {}
        put_greeks = put_data.get("option_greeks") or {}
        pe_oi = put_md.get("oi", 0) or 0
        pe_prev_oi = put_md.get("prev_oi", 0) or 0
        pe_vol = put_md.get("volume", 0) or 0
        pe_ltp = put_md.get("ltp", 0) or 0
        try:
            pe_iv = float(put_greeks.get("iv", 0) or put_md.get("iv", 0) or 0)
        except (ValueError, TypeError):
            pe_iv = 0.0

        # Extract Greeks with robust typing
        try:
            ce_delta = float(call_greeks.get("delta", 0) or 0)
        except (ValueError, TypeError):
            ce_delta = 0.0
            
        try:
            pe_delta = float(put_greeks.get("delta", 0) or 0)
        except (ValueError, TypeError):
            pe_delta = 0.0

        # Sign standardization: Call delta is positive (0 to 1), Put delta is negative (-1 to 0)
        ce_delta = abs(ce_delta) if ce_delta != 0 else 0.0
        pe_delta = -abs(pe_delta) if pe_delta != 0 else 0.0

        try:
            ce_gamma = float(call_greeks.get("gamma", 0) or 0)
        except (ValueError, TypeError):
            ce_gamma = 0.0
            
        try:
            pe_gamma = float(put_greeks.get("gamma", 0) or 0)
        except (ValueError, TypeError):
            pe_gamma = 0.0

        # Fallback Gamma calculation using Black-Scholes model if broker Greeks are empty
        if ce_gamma <= 0 and ce_iv > 0:
            ce_gamma = calculate_bs_gamma(spot, strike_price, ce_iv, expiry)
        if pe_gamma <= 0 and pe_iv > 0:
            pe_gamma = calculate_bs_gamma(spot, strike_price, pe_iv, expiry)

        # Determine if strike is in the Active Window (ATM ± 6)
        is_active_strike = False
        if atm_index != -1:
            try:
                strike_idx = sorted_strikes.index(strike_price)
                if abs(strike_idx - atm_index) <= 10:
                    is_active_strike = True
            except ValueError:
                pass
        else:
            is_active_strike = True  # Fallback to all if ATM not found

        if is_active_strike:
            total_ce_oi += ce_oi
            total_pe_oi += pe_oi
            total_ce_oi_chg += (ce_oi - ce_prev_oi)
            total_pe_oi_chg += (pe_oi - pe_prev_oi)
            total_ce_vol += ce_vol
            total_pe_vol += pe_vol

        strikes_for_mp.append({
            "strike": strike_price,
            "ce_oi": ce_oi,
            "pe_oi": pe_oi,
        })

        # Build detailed strike→ Greeks & notional map
        result["strike_map"][float(strike_price)] = {
            "ce_ltp": ce_ltp,
            "pe_ltp": pe_ltp,
            "ce_iv":  ce_iv,
            "pe_iv":  pe_iv,
            "ce_oi":  ce_oi,
            "pe_oi":  pe_oi,
            "ce_oi_chg": ce_oi - ce_prev_oi,
            "pe_oi_chg": pe_oi - pe_prev_oi,
            "ce_vol": ce_vol,
