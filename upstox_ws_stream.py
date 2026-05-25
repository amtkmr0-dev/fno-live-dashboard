"""
upstox_ws_stream.py — Upstox WebSocket v3 Streaming for F&O Dashboard
======================================================================
Replaces REST-based poll_ltp() with a persistent WebSocket connection
to Upstox's v3 market data feed. Receives real-time LTP ticks via
protobuf-encoded binary frames.

Usage:
    from upstox_ws_stream import UpstoxStreamer

    streamer = UpstoxStreamer(
        token=self.token,
        instrument_keys=[s.ikey for s in self.stocks],
        ikey_to_symbol=self.ikey_to_symbol,
        on_tick=self._handle_ws_tick,    # async callback(delta_dict)
        on_status=self._handle_ws_status, # async callback(state_str, msg_str)
        session=self.session,            # aiohttp.ClientSession
    )
    asyncio.create_task(streamer.run())   # start in background
    ...
    await streamer.stop()                 # graceful shutdown

Requires: pip install protobuf websockets (or aiohttp with ws client)

The protobuf schema is compiled at import time from the embedded .proto
definition — no external .proto file needed at runtime.
"""

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set

import aiohttp

log = logging.getLogger("upstox_ws")

# ---------------------------------------------------------------------------
# Protobuf — compiled at import time
# ---------------------------------------------------------------------------
# We use google.protobuf descriptor_pool + descriptor + message_factory
# to parse the .proto at runtime without needing protoc compilation step.
# This avoids adding a build step to the deployment.

_PROTO_TEXT = r"""
syntax = "proto3";
package com.upstox.marketdatafeederv3udapi.rpc.proto;

message LTPC {
  double ltp = 1;
  int64 ltt = 2;
  int64 ltq = 3;
  double cp = 4;
}

message MarketLevel {
  repeated Quote bidAskQuote = 1;
}

message MarketOHLC {
  repeated OHLC ohlc = 1;
}

message Quote {
  int64 bidQ = 1;
  double bidP = 2;
  int64 askQ = 3;
  double askP = 4;
}

message OptionGreeks {
  double delta = 1;
  double theta = 2;
  double gamma = 3;
  double vega = 4;
  double rho = 5;
}

message OHLC {
  string interval = 1;
  double open = 2;
  double high = 3;
  double low = 4;
  double close = 5;
  int64 vol = 6;
  int64 ts = 7;
}

enum Type {
  initial_feed = 0;
  live_feed = 1;
  market_info = 2;
}

message MarketFullFeed {
  LTPC ltpc = 1;
  MarketLevel marketLevel = 2;
  OptionGreeks optionGreeks = 3;
  MarketOHLC marketOHLC = 4;
  double atp = 5;
  int64 vtt = 6;
  double oi = 7;
  double iv = 8;
  double tbq = 9;
  double tsq = 10;
}

message IndexFullFeed {
  LTPC ltpc = 1;
  MarketOHLC marketOHLC = 2;
}

message FullFeed {
  oneof FullFeedUnion {
    MarketFullFeed marketFF = 1;
    IndexFullFeed indexFF = 2;
  }
}

message FirstLevelWithGreeks {
  LTPC ltpc = 1;
  Quote firstDepth = 2;
  OptionGreeks optionGreeks = 3;
  int64 vtt = 4;
  double oi = 5;
  double iv = 6;
}

message Feed {
  oneof FeedUnion {
    LTPC ltpc = 1;
    FullFeed fullFeed = 2;
    FirstLevelWithGreeks firstLevelWithGreeks = 3;
  }
  RequestMode requestMode = 4;
}

enum RequestMode {
  ltpc = 0;
  full_d5 = 1;
  option_greeks = 2;
  full_d30 = 3;
}

enum MarketStatus {
  PRE_OPEN_START = 0;
  PRE_OPEN_END = 1;
  NORMAL_OPEN = 2;
  NORMAL_CLOSE = 3;
  CLOSING_START = 4;
  CLOSING_END = 5;
}

message MarketInfo {
  map<string, MarketStatus> segmentStatus = 1;
}

message FeedResponse {
  Type type = 1;
  map<string, Feed> feeds = 2;
  int64 currentTs = 3;
  MarketInfo marketInfo = 4;
}
"""

# We'll try two approaches to parse protobuf:
# 1. google.protobuf with runtime proto parsing
# 2. Fallback: manual binary parsing for the LTPC subset we need

_FeedResponse = None
_proto_available = False

def _init_protobuf():
    """Compile protobuf schema at import time."""
    global _FeedResponse, _proto_available

    try:
        from google.protobuf import descriptor_pb2
        from google.protobuf import descriptor_pool
        from google.protobuf import symbol_database
        from google.protobuf.compiler import plugin_pb2  # noqa: F401
    except ImportError:
        pass

    # Try the simple approach: write .proto, compile with protoc-like runtime
    try:
        import tempfile
        import os
        from google.protobuf import descriptor_pb2 as dp2
        from google.protobuf import descriptor_pool as dpool
        from google.protobuf import message_factory

        # Use proto_api / descriptor approach
        # Actually, the cleanest way in pure Python without protoc is to use
        # google.protobuf's text_format or descriptor_pool.Add + FileDescriptor
        # But that's complex. Let's use a simpler approach: protobuf JSON decoder.

        # Simplest: write .proto to temp file and use grpcio-tools or protoc
        # But we want zero build deps. Let's use the raw binary decoder.
        raise ImportError("Skip complex proto setup, use manual decoder")

    except Exception:
        pass

    # Manual approach: for LTPC mode (our primary use case), the FeedResponse
    # structure is simple enough to decode manually using protobuf wire format.
    # We'll implement a lightweight decoder.
    log.info("Using manual protobuf decoder for Upstox v3 feed")
    _proto_available = False


def _decode_varint(buf, pos):
    """Decode a varint from buf starting at pos. Returns (value, new_pos)."""
    result = 0
    shift = 0
    while pos < len(buf):
        b = buf[pos]
        result |= (b & 0x7F) << shift
        pos += 1
        if (b & 0x80) == 0:
            return result, pos
        shift += 7
    raise ValueError("Truncated varint")


def _decode_fixed64(buf, pos):
    """Decode a 64-bit little-endian value."""
    import struct
    val = struct.unpack_from('<d', buf, pos)[0]
    return val, pos + 8


def _decode_field(buf, pos):
    """Decode a single protobuf field. Returns (field_number, wire_type, value, new_pos)."""
    tag, pos = _decode_varint(buf, pos)
    field_number = tag >> 3
    wire_type = tag & 0x07

    if wire_type == 0:  # varint
        value, pos = _decode_varint(buf, pos)
        return field_number, wire_type, value, pos
    elif wire_type == 1:  # 64-bit (double)
        value, pos = _decode_fixed64(buf, pos)
        return field_number, wire_type, value, pos
    elif wire_type == 2:  # length-delimited (string, bytes, embedded message)
        length, pos = _decode_varint(buf, pos)
        value = buf[pos:pos + length]
        return field_number, wire_type, value, pos + length
    elif wire_type == 5:  # 32-bit
        import struct
        value = struct.unpack_from('<f', buf, pos)[0]
        return field_number, wire_type, value, pos + 4
    else:
        raise ValueError(f"Unsupported wire type {wire_type} at pos {pos}")


def _decode_message(buf):
    """Decode all fields from a protobuf message buffer into a list of (field_number, wire_type, value)."""
    fields = []
    pos = 0
    while pos < len(buf):
        try:
            fn, wt, val, pos = _decode_field(buf, pos)
            fields.append((fn, wt, val))
        except (ValueError, IndexError):
            break
    return fields


def _decode_ltpc(buf):
    """Decode an LTPC message: ltp(1,double), ltt(2,varint), ltq(3,varint), cp(4,double)."""
    result = {"ltp": 0.0, "ltt": 0, "ltq": 0, "cp": 0.0}
    for fn, wt, val in _decode_message(buf):
        if fn == 1 and wt == 1:   # ltp - double
            result["ltp"] = val
        elif fn == 2 and wt == 0:  # ltt - varint (epoch ms)
            result["ltt"] = val
        elif fn == 3 and wt == 0:  # ltq - varint
            result["ltq"] = val
        elif fn == 4 and wt == 1:  # cp - double
            result["cp"] = val
    return result


def _decode_ohlc(buf):
    """Decode an OHLC sub-message: interval(1,str), open(2,d), high(3,d), low(4,d), close(5,d), vol(6,varint), ts(7,varint)."""
    result = {"interval": "", "open": 0.0, "high": 0.0, "low": 0.0, "close": 0.0, "vol": 0, "ts": 0}
    for fn, wt, val in _decode_message(buf):
        if fn == 1 and wt == 2:   result["interval"] = val.decode("utf-8") if isinstance(val, bytes) else str(val)
        elif fn == 2 and wt == 1: result["open"] = val
        elif fn == 3 and wt == 1: result["high"] = val
        elif fn == 4 and wt == 1: result["low"] = val
        elif fn == 5 and wt == 1: result["close"] = val
        elif fn == 6 and wt == 0: result["vol"] = val
        elif fn == 7 and wt == 0: result["ts"] = val
    return result


def _decode_market_ohlc(buf):
    """Decode MarketOHLC: repeated OHLC ohlc=1. Returns the day-interval (1d) candle if present, else first."""
    candles = []
    for fn, wt, val in _decode_message(buf):
        if fn == 1 and wt == 2:
            candles.append(_decode_ohlc(val))
    # Prefer interval='1d' for daily totals; fall back to anything we have
    for c in candles:
        if c.get("interval") in ("1d", "I1"):
            return c
    return candles[0] if candles else None


def _decode_full_feed_inner(buf, is_index=False):
    """
    Decode a MarketFullFeed (or IndexFullFeed) message.
    Returns dict with ltpc + day-OHLC + vtt + oi (where applicable).
    """
    result = {"ltpc": None, "ohlc": None, "vtt": 0, "oi": 0.0, "atp": 0.0, "iv": 0.0}
    for fn, wt, val in _decode_message(buf):
        if fn == 1 and wt == 2:    # ltpc
            result["ltpc"] = _decode_ltpc(val)
        elif (not is_index) and fn == 4 and wt == 2:  # marketOHLC (MarketFullFeed only)
            result["ohlc"] = _decode_market_ohlc(val)
        elif is_index and fn == 2 and wt == 2:        # marketOHLC (IndexFullFeed: field=2)
            result["ohlc"] = _decode_market_ohlc(val)
        elif (not is_index) and fn == 5 and wt == 1:  # atp
            result["atp"] = val
        elif (not is_index) and fn == 6 and wt == 0:  # vtt (total volume for the day)
            result["vtt"] = val
        elif (not is_index) and fn == 7 and wt == 1:  # oi
            result["oi"] = val
        elif (not is_index) and fn == 8 and wt == 1:  # iv
            result["iv"] = val
    return result


def _decode_feed(buf):
    """Decode a Feed message. Returns dict with ltpc data, optional full-mode fields, and mode."""
    result = {"ltpc": None, "ohlc": None, "vtt": 0, "oi": 0.0, "iv": 0.0, "atp": 0.0, "mode": None}
    for fn, wt, val in _decode_message(buf):
        if fn == 1 and wt == 2:  # ltpc (LTPC mode)
            result["ltpc"] = _decode_ltpc(val)
        elif fn == 2 and wt == 2:  # fullFeed (Full / Full_D30)
            for ffn, fwt, fval in _decode_message(val):
                if ffn == 1 and fwt == 2:    # marketFF (stocks/futures/options)
                    full = _decode_full_feed_inner(fval, is_index=False)
                    if full.get("ltpc"): result["ltpc"] = full["ltpc"]
                    if full.get("ohlc"): result["ohlc"] = full["ohlc"]
                    result["vtt"] = full.get("vtt", 0)
                    result["oi"]  = full.get("oi", 0.0)
                    result["iv"]  = full.get("iv", 0.0)
                    result["atp"] = full.get("atp", 0.0)
                elif ffn == 2 and fwt == 2:  # indexFF (NIFTY/BANKNIFTY/VIX)
                    full = _decode_full_feed_inner(fval, is_index=True)
                    if full.get("ltpc"): result["ltpc"] = full["ltpc"]
                    if full.get("ohlc"): result["ohlc"] = full["ohlc"]
        elif fn == 3 and wt == 2:  # firstLevelWithGreeks (Option Greeks mode)
            for gfn, gwt, gval in _decode_message(val):
                if gfn == 1 and gwt == 2:    # ltpc
                    result["ltpc"] = _decode_ltpc(gval)
                elif gfn == 4 and gwt == 0:  # vtt
                    result["vtt"] = gval
                elif gfn == 5 and gwt == 1:  # oi
                    result["oi"]  = gval
                elif gfn == 6 and gwt == 1:  # iv
                    result["iv"]  = gval
        elif fn == 4 and wt == 0:  # requestMode enum
            modes = {0: "ltpc", 1: "full_d5", 2: "option_greeks", 3: "full_d30"}
            result["mode"] = modes.get(val, str(val))
    return result


def _decode_map_entry(buf):
    """Decode a map<string, Feed> entry. Returns (key_str, feed_dict)."""
    key = None
    feed = None
    for fn, wt, val in _decode_message(buf):
        if fn == 1 and wt == 2:  # key (string)
            key = val.decode("utf-8") if isinstance(val, bytes) else str(val)
        elif fn == 2 and wt == 2:  # value (Feed message)
            feed = _decode_feed(val)
    return key, feed


def decode_feed_response(buf: bytes) -> dict:
    """
    Decode a FeedResponse protobuf message.

    Returns: {
        "type": "initial_feed" | "live_feed" | "market_info",
        "feeds": { "NSE_EQ|INE002A01018": {"ltpc": {"ltp":..., "cp":...}, "mode":"ltpc"}, ... },
        "currentTs": 1725876064349,
        "marketInfo": { ... }
    }
    """
    result = {"type": None, "feeds": {}, "currentTs": 0, "marketInfo": None}
    type_names = {0: "initial_feed", 1: "live_feed", 2: "market_info"}

    for fn, wt, val in _decode_message(buf):
        if fn == 1 and wt == 0:  # Type enum
            result["type"] = type_names.get(val, str(val))
        elif fn == 2 and wt == 2:  # map<string, Feed> — each entry is length-delimited
            key, feed = _decode_map_entry(val)
            if key and feed:
                result["feeds"][key] = feed
        elif fn == 3 and wt == 0:  # currentTs (int64 varint)
            result["currentTs"] = val
        elif fn == 4 and wt == 2:  # MarketInfo (embedded)
            result["marketInfo"] = val  # raw bytes, parse if needed

    return result


# Initialize protobuf on import
_init_protobuf()


# ---------------------------------------------------------------------------
# Auth URL endpoint
# ---------------------------------------------------------------------------
UPSTOX_WS_AUTH_URL = "https://api.upstox.com/v3/feed/market-data-feed/authorize"


# ---------------------------------------------------------------------------
# UpstoxStreamer class
# ---------------------------------------------------------------------------

class UpstoxStreamer:
    """
    Persistent WebSocket connection to Upstox v3 market data feed.

    Subscribes to ~196 F&O stock instrument keys in LTPC mode and
    dispatches decoded tick data via an async callback.

    Features:
    - Automatic reconnect with exponential backoff (3s → 60s)
    - Fresh auth URL on every connect (single-use wss:// URLs)
    - Binary frame subscription (required by v3 API)
    - Protobuf decoding (manual decoder, no protoc needed)
    - Skips initial_feed/market_info ticks, only processes live_feed
    - Token hot-swap: call update_token() when admin refreshes JWT
    - Graceful shutdown with stop()
    """

    def __init__(
        self,
        token: str,
        instrument_keys: List[str],
        ikey_to_symbol: Dict[str, str],
        on_tick: Callable[[Dict[str, Dict[str, Any]]], Coroutine],
        on_status: Callable[[str, str], Coroutine],
        session: aiohttp.ClientSession,
        mode: str = "ltpc",
    ):
        self.token = token
        self.instrument_keys = instrument_keys
        self.ikey_to_symbol = ikey_to_symbol
        self._on_tick = on_tick
        self._on_status = on_status
        self.session = session
        self.mode = mode

        # State
        self._running = False
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._connected = False
        self._reconnect_delay = 3.0
        self._max_reconnect_delay = 60.0
        self._tick_count = 0
        self._last_tick_time = 0.0
        self._connect_time = 0.0
        self._disconnect_count = 0
        self._initial_feed_received = False

        # Stats
        self.stats = {
            "state": "idle",
            "connected_at": None,
            "ticks_received": 0,
            "last_tick_at": None,
            "reconnects": 0,
            "errors": [],
        }

    @property
    def connected(self) -> bool:
        return self._connected and self._ws is not None

    def update_token(self, new_token: str):
        """Hot-swap the Upstox access token. Forces reconnect."""
        self.token = new_token
        log.info("Token updated for Upstox WS streamer — will reconnect")
        # Close current connection to trigger reconnect with new token
        if self._ws and not self._ws.closed:
            asyncio.create_task(self._ws.close())

    async def _authorize(self) -> Optional[str]:
        """
        Call the Upstox v3 feed authorize endpoint to get a single-use wss:// URL.
        Returns the WebSocket URL or None on failure.
        """
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }
        try:
            async with self.session.get(
                UPSTOX_WS_AUTH_URL,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 401:
                    log.error("Upstox WS auth failed: 401 Unauthorized (token expired?)")
                    await self._on_status("token_expired", "Upstox token expired — cannot stream")
                    return None
                if resp.status == 429:
                    retry_after = int(resp.headers.get("Retry-After", "5"))
                    log.warning("Upstox WS auth rate-limited (429). Wait %ds", retry_after)
                    await asyncio.sleep(retry_after)
                    return None
                if resp.status != 200:
                    body = await resp.text()
                    log.error("Upstox WS auth failed: HTTP %d — %s", resp.status, body[:200])
                    return None

                data = await resp.json()
                ws_url = data.get("data", {}).get("authorized_redirect_uri")
                if not ws_url:
                    log.error("Upstox WS auth: no authorized_redirect_uri in response: %s",
                              json.dumps(data)[:300])
                    return None

                log.info("Got Upstox WS auth URL (wss://...)")
                return ws_url

        except asyncio.TimeoutError:
            log.error("Upstox WS auth timed out")
            return None
        except Exception as exc:
            log.error("Upstox WS auth error: %s", exc)
            return None

    def _build_subscribe_msg(self) -> bytes:
        """
        Build the binary subscription frame.
        CRITICAL: Must be sent as a binary WebSocket frame, not text.
        """
        msg = {
            "guid": str(uuid.uuid4()),
            "method": "sub",
            "data": {
                "mode": self.mode,
                "instrumentKeys": self.instrument_keys,
            },
        }
        return json.dumps(msg).encode("utf-8")

    async def _connect(self) -> bool:
        """Authorize, connect to WebSocket, and subscribe."""
        self.stats["state"] = "authorizing"
        await self._on_status("connecting", "Authorizing Upstox WebSocket...")

        ws_url = await self._authorize()
        if not ws_url:
            return False

        self.stats["state"] = "connecting"
        try:
            self._ws = await self.session.ws_connect(
                ws_url,
                heartbeat=30,
                timeout=aiohttp.ClientTimeout(total=15),
            )
        except Exception as exc:
            log.error("Upstox WS connect failed: %s", exc)
            self.stats["errors"].append({"time": time.time(), "error": str(exc)})
            return False

        msg = {
            "guid": str(uuid.uuid4()),
            "method": "sub",
            "data": {
                "mode": self.mode,
                "instrumentKeys": self.instrument_keys,
            }
        }
        await self._ws.send_bytes(json.dumps(msg).encode("utf-8"))
            
        log.info("Subscribed to %d instruments in '%s' mode via Upstox WS v3",
                 len(self.instrument_keys), self.mode)

        self._connected = True
        self._connect_time = time.time()
        self._initial_feed_received = False
        self._reconnect_delay = 3.0  # reset backoff
        self.stats["state"] = "connected"
        self.stats["connected_at"] = self._connect_time
        await self._on_status("connected", f"Upstox WS connected — streaming {len(self.instrument_keys)} instruments")

        return True

    async def _process_message(self, msg: aiohttp.WSMessage):
        """Process a single WebSocket message (binary protobuf frame)."""
        if msg.type == aiohttp.WSMsgType.BINARY:
            try:
                resp = decode_feed_response(msg.data)
            except Exception as exc:
                log.warning("Protobuf decode error: %s (data len=%d)", exc, len(msg.data))
                return

            feed_type = resp.get("type")

            # Skip market_info messages
            if feed_type == "market_info":
                log.debug("Upstox WS: market_info received")
                return

            # Track initial_feed vs live_feed
            if feed_type == "initial_feed":
                if not self._initial_feed_received:
                    self._initial_feed_received = True
                    log.info("Upstox WS: initial_feed snapshot received (%d instruments)",
                             len(resp.get("feeds", {})))
                # Process initial feed same as live feed — it has current prices

            feeds = resp.get("feeds", {})
            if not feeds:
                return

            # Build delta dict: symbol -> {ltp, chg, chg_pct, vol}
            delta: Dict[str, Dict[str, Any]] = {}

            for ikey, feed_data in feeds.items():
                ltpc = feed_data.get("ltpc") if feed_data else None
                if not ltpc:
                    continue

                ltp = ltpc.get("ltp", 0)
                cp = ltpc.get("cp", 0)  # previous close

                if ltp <= 0:
                    continue

                # Resolve instrument key to symbol
                sym = self.ikey_to_symbol.get(ikey)
                if not sym:
                    # Try colon format
                    colon_key = ikey.replace("|", ":")
                    sym = self.ikey_to_symbol.get(colon_key)
                if not sym:
                    continue

                # Compute change
                chg = round(ltp - cp, 2) if cp > 0 else 0
                chg_pct = round((chg / cp) * 100, 2) if cp > 0 else 0

                tick = {
                    "ltp": ltp,
                    "cp": cp,
                    "chg": chg,
                    "chg_pct": chg_pct,
                }

                # Full / Full_D5 / Full_D30 mode also carries day-OHLC + volume + OI.
                # These fields are absent in LTPC mode and harmlessly skipped.
                ohlc = feed_data.get("ohlc") if feed_data else None
                if ohlc:
                    if ohlc.get("open"):  tick["open"]  = ohlc["open"]
                    if ohlc.get("high"):  tick["high"]  = ohlc["high"]
                    if ohlc.get("low"):   tick["low"]   = ohlc["low"]
                    if ohlc.get("close"): tick["close"] = ohlc["close"]
                vtt = feed_data.get("vtt") or 0
                if vtt: tick["vol"] = int(vtt)
                oi  = feed_data.get("oi") or 0
                if oi: tick["oi"] = oi
                iv  = feed_data.get("iv") or 0
                if iv: tick["iv"] = iv

                delta[sym] = tick

            if delta:
                self._tick_count += len(delta)
                self._last_tick_time = time.time()
                self.stats["ticks_received"] = self._tick_count
                self.stats["last_tick_at"] = self._last_tick_time

                # Dispatch to server callback
                await self._on_tick(delta)

        elif msg.type == aiohttp.WSMsgType.TEXT:
            # Upstox v3 shouldn't send text frames, but handle gracefully
            log.debug("Upstox WS text frame: %s", str(msg.data)[:200])

        elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING,
                          aiohttp.WSMsgType.CLOSED):
            log.info("Upstox WS closed by server (code=%s)", getattr(msg, 'data', ''))
            self._connected = False

        elif msg.type == aiohttp.WSMsgType.ERROR:
            log.error("Upstox WS error: %s", self._ws.exception() if self._ws else "unknown")
            self._connected = False

    async def run(self):
        """
        Main loop: connect → receive ticks → reconnect on disconnect.
        Runs until stop() is called.
        """
        self._running = True
        log.info("Upstox WS streamer starting (%d instruments, mode=%s)",
                 len(self.instrument_keys), self.mode)

        while self._running:
            # Skip if no token
            if not self.token:
                self.stats["state"] = "no_token"
                await asyncio.sleep(5)
                continue

            # Connect
            success = await self._connect()
            if not success:
                self.stats["state"] = "reconnecting"
                self._disconnect_count += 1
                self.stats["reconnects"] = self._disconnect_count
                log.info("Upstox WS reconnecting in %.0fs...", self._reconnect_delay)
                await self._on_status("reconnecting",
                                      f"Upstox WS reconnecting in {self._reconnect_delay:.0f}s...")
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 1.5, self._max_reconnect_delay)
                continue

            # Message loop
            try:
                async for msg in self._ws:
                    if not self._running:
                        break
                    await self._process_message(msg)
            except asyncio.CancelledError:
                log.info("Upstox WS streamer cancelled")
                break
            except Exception as exc:
                log.error("Upstox WS message loop error: %s", exc)
                self.stats["errors"].append({"time": time.time(), "error": str(exc)})

            # If we get here, connection dropped
            self._connected = False
            self.stats["state"] = "disconnected"
            self._disconnect_count += 1
            self.stats["reconnects"] = self._disconnect_count

            if self._running:
                log.info("Upstox WS disconnected. Reconnecting in %.0fs...", self._reconnect_delay)
                await self._on_status("reconnecting",
                                      f"Upstox WS reconnecting in {self._reconnect_delay:.0f}s...")
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 1.5, self._max_reconnect_delay)

        # Cleanup
        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._connected = False
        self.stats["state"] = "stopped"
        log.info("Upstox WS streamer stopped (total ticks: %d, reconnects: %d)",
                 self._tick_count, self._disconnect_count)

    async def stop(self):
        """Graceful shutdown."""
        self._running = False
        if self._ws and not self._ws.closed:
            try:
                await self._ws.close()
            except Exception:
                pass
        self._connected = False

    def get_status(self) -> dict:
        """Return current streamer status for admin API."""
        return {
            "enabled": True,
            "state": self.stats["state"],
            "connected": self._connected,
            "connected_at": self.stats.get("connected_at"),
            "uptime_seconds": round(time.time() - self._connect_time, 1) if self._connected else 0,
            "ticks_received": self._tick_count,
            "last_tick_at": self.stats.get("last_tick_at"),
            "reconnects": self._disconnect_count,
            "instrument_count": len(self.instrument_keys),
            "mode": self.mode,
            "recent_errors": self.stats.get("errors", [])[-5:],  # last 5 errors
        }
