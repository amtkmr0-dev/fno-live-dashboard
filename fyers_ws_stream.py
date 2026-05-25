import asyncio
import logging
from typing import Dict, Any, Callable, Coroutine, List
from threading import Thread

from fyers_apiv3.FyersWebsocket.data_ws import FyersDataSocket

log = logging.getLogger("fyers_ws")

class FyersStreamer:
    """Wrapper around Fyers APIv3 WebSocket to bridge with our AsyncIO event loop."""
    
    def __init__(
        self,
        token: str,
        on_tick: Callable[[Dict[str, Dict[str, Any]]], Coroutine],
        on_status: Callable[[str, str], Coroutine],
        loop: asyncio.AbstractEventLoop
    ):
        self.token = token
        self.on_tick = on_tick
        self.on_status = on_status
        self.loop = loop
        
        self.connected = False
        self._ws = None
        self._thread = None
        self._subscribed_symbols = set()

    def update_token(self, new_token: str):
        self.token = new_token
        if self._ws:
            # Fyers DataSocket token update usually requires a reconnection.
            # For safety, we close and restart.
            self.stop_sync()
            self.start()

    def get_status(self) -> dict:
        return {
            "enabled": bool(self.token),
            "connected": self.connected,
            "subscriptions": len(self._subscribed_symbols),
        }

    def _on_message(self, message):
        try:
            delta = {}
            
            # Fyers usually sends a dict or a list of dicts.
            messages = message if isinstance(message, list) else [message]
            
            for item in messages:
                if not isinstance(item, dict) or "symbol" not in item:
                    continue
                
                sym = item["symbol"]
                mapped = {}
                
                if "ltp" in item:
                    mapped["ltp"] = item["ltp"]
                if "vol_traded_today" in item:
                    mapped["vtt"] = item["vol_traded_today"]
                if "oi" in item:
                    mapped["oi"] = item["oi"]
                if "prev_close_price" in item:
                    mapped["close"] = item["prev_close_price"]
                
                if mapped:
                    delta[sym] = mapped
                    
            if delta:
                asyncio.run_coroutine_threadsafe(self.on_tick(delta), self.loop)
                
        except Exception as e:
            log.error("Fyers WS Message Parse Error: %s", e)

    def _on_error(self, message):
        log.error("Fyers WS Error: %s", message)
        asyncio.run_coroutine_threadsafe(self.on_status("error", str(message)), self.loop)

    def _on_close(self, message):
        self.connected = False
        log.info("Fyers WS Closed: %s", message)
        asyncio.run_coroutine_threadsafe(self.on_status("closed", "Connection closed"), self.loop)

    def _on_connect(self):
        self.connected = True
        log.info("Fyers WS Connected successfully!")
        asyncio.run_coroutine_threadsafe(self.on_status("connected", "Connected"), self.loop)
        
        if self._subscribed_symbols:
            self._do_subscribe(list(self._subscribed_symbols))

    def _do_subscribe(self, symbols: List[str]):
        if not self.connected or not self._ws:
            return
            
        # Fyers supports bulk subscribe, but let's chunk safely
        chunk_size = 50
        for i in range(0, len(symbols), chunk_size):
            chunk = symbols[i:i+chunk_size]
            try:
                self._ws.subscribe(symbols=chunk, data_type="SymbolUpdate")
                log.debug("Fyers WS subscribed to %d symbols", len(chunk))
            except Exception as e:
                log.error("Fyers WS subscription failed: %s", e)

    def subscribe(self, symbols: List[str]):
        new_syms = set(symbols) - self._subscribed_symbols
        if new_syms:
            self._subscribed_symbols.update(new_syms)
            self._do_subscribe(list(new_syms))

    def start(self):
        if not self.token:
            log.warning("Fyers WS Streamer disabled: No FYERS_ACCESS_TOKEN provided.")
            return

        log.info("Initializing Fyers WS Streamer...")
        self._ws = FyersDataSocket(
            access_token=self.token,
            write_to_file=False,
            log_path=None,
            litemode=False,
            reconnect=True,
            on_message=self._on_message,
            on_error=self._on_error,
            on_connect=self._on_connect,
            on_close=self._on_close
        )
        
        def run_ws():
            self._ws.connect()
            self._ws.keep_running()

        self._thread = Thread(target=run_ws, daemon=True, name="FyersWSThread")
        self._thread.start()

    def stop_sync(self):
        if self._ws:
            try:
                self._ws.close_connection()
            except Exception as e:
                log.error("Error closing Fyers WS: %s", e)
        self.connected = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
            
    async def stop(self):
        self.stop_sync()
