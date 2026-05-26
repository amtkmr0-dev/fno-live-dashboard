#!/usr/bin/env python3
"""
auth_helpers.py — Shared session/role helpers for auth_proxy.py.

Removes the copy-paste ``token = get_session_from_request(...) ; session =
validate_session(token) ; if not session: ...`` block that was repeated in
~30 handlers, and centralizes admin-role checks.
"""

from __future__ import annotations

from functools import wraps
from typing import Callable, Awaitable

from aiohttp import web


SESSION_COOKIE = "quantra_session"


def _extract_token(request: web.Request) -> str | None:
    return request.cookies.get(SESSION_COOKIE)


def make_session_decorators(validate_session_fn: Callable[[str | None], dict | None]):
    """
    Build ``require_session`` and ``require_admin`` decorators bound to the
    given ``validate_session_fn`` (passed in from auth_proxy to avoid a
    circular import).

    The decorated handler receives the ``session`` dict as a third positional
    arg: ``async def handler(request, session) -> web.Response``.
    """

    def require_session(handler: Callable[[web.Request, dict], Awaitable[web.Response]]):
        @wraps(handler)
        async def wrapper(request: web.Request) -> web.Response:
            session = validate_session_fn(_extract_token(request))
            if not session:
                return web.json_response({"error": "Unauthorized"}, status=401)
            return await handler(request, session)
        return wrapper

    def require_admin(handler: Callable[[web.Request, dict], Awaitable[web.Response]]):
        @wraps(handler)
        async def wrapper(request: web.Request) -> web.Response:
            session = validate_session_fn(_extract_token(request))
            if not session:
                return web.json_response({"error": "Unauthorized"}, status=401)
            if session.get("role") != "admin":
                return web.json_response({"error": "Admin access required"}, status=403)
            return await handler(request, session)
        return wrapper

    return require_session, require_admin


def compute_option_buyer_pnl(entry_premium: float | None,
                             exit_premium: float | None,
                             qty: int,
                             flat_cost: float = 40.0) -> dict | None:
    """
    Standard option-buyer P&L calculation used by both manual exits and
    paper-trade updates. Returns ``None`` if inputs are missing.

    The legacy code branched on ``direction == "SELL"`` which never matched
    the actual stored values (``BULLISH/BEARISH/CE/PE``), causing bearish
    trades to be booked with the wrong sign. Option *buyers* always profit
    when premium rises regardless of CE/PE — that's the model used by
    ws_server.handle_paper_exit, and we standardize on it here.
    """
    if entry_premium is None or exit_premium is None:
        return None
    try:
        entry = float(entry_premium)
        exit_ = float(exit_premium)
        q = int(qty) if qty else 1
    except (TypeError, ValueError):
        return None
    pnl = round((exit_ - entry) * q, 2)
    notional = entry * q
    pnl_pct = round((pnl / notional) * 100, 2) if notional else 0.0
    return {
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "costs_estimated": flat_cost,
        "net_pnl": round(pnl - flat_cost, 2),
    }
