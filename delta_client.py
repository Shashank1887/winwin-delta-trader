"""Delta Exchange India REST + CCXT client with HMAC-SHA256 authentication."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import ccxt
import pandas as pd
import requests

from config import DELTA_INDIA_BASE_URL, TradingConfig

CCXT_SYMBOL_MAP: Dict[str, str] = {
    "BTCUSD": "BTC/USD:USD",
    "ETHUSD": "ETH/USD:USD",
    "SOLUSD": "SOL/USD:USD",
}


class DeltaAPIError(RuntimeError):
    """Raised when Delta Exchange India returns an API error."""


class DeltaIndiaClient:
    """Client for Delta Exchange India combining ccxt market data with signed v2 REST calls."""

    def __init__(
        self,
        config: Optional[TradingConfig] = None,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.config = config or TradingConfig()
        self.base_url = self.config.base_url.rstrip("/")
        self.session = session or requests.Session()
        self._ccxt_exchange: Optional[ccxt.delta] = None
        self._product_cache: Dict[str, Dict[str, Any]] = {}

    def _get_ccxt(self) -> ccxt.delta:
        """Lazily initialize ccxt.delta overridden for Delta Exchange India."""
        if self._ccxt_exchange is None:
            self._ccxt_exchange = ccxt.delta(
                {
                    "apiKey": self.config.api_key or None,
                    "secret": self.config.api_secret or None,
                    "enableRateLimit": True,
                    "urls": {
                        "api": {
                            "public": self.base_url,
                            "private": self.base_url,
                        }
                    },
                }
            )
        return self._ccxt_exchange

    @staticmethod
    def to_ccxt_symbol(symbol: str) -> str:
        """Convert Delta contract symbol (e.g. BTCUSD) to CCXT unified perpetual symbol."""
        clean = symbol.strip().upper()
        if clean in CCXT_SYMBOL_MAP:
            return CCXT_SYMBOL_MAP[clean]
        if "/" in clean:
            return clean
        if clean.endswith("USD"):
            base = clean[:-3]
            return f"{base}/USD:USD"
        return clean

    def build_signature(
        self,
        method: str,
        path: str,
        query_string: str = "",
        payload: str = "",
        timestamp: Optional[str] = None,
    ) -> Dict[str, str]:
        """Generate HMAC-SHA256 authentication headers required by Delta v2 API."""
        if not self.config.has_credentials:
            raise DeltaAPIError(
                "Delta India API credentials are missing. Add DELTA_API_KEY and DELTA_API_SECRET to .env."
            )

        ts = timestamp or str(int(time.time()))
        prehash = f"{method.upper()}{ts}{path}{query_string}{payload}"
        signature = hmac.new(
            self.config.api_secret.encode("utf-8"),
            prehash.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        return {
            "api-key": self.config.api_key,
            "timestamp": ts,
            "signature": signature,
            "User-Agent": "winwin-delta-india-bot/1.0",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
        authenticated: bool = False,
        timeout: int = 10,
    ) -> Any:
        """Execute an HTTP request against Delta India REST API and return parsed result."""
        query_string = f"?{urlencode(params)}" if params else ""
        payload = json.dumps(body, separators=(",", ":")) if body is not None else ""
        url = f"{self.base_url}{path}{query_string}"

        if authenticated:
            headers = self.build_signature(method, path, query_string, payload)
        else:
            headers = {
                "Accept": "application/json",
                "User-Agent": "winwin-delta-india-bot/1.0",
            }

        resp = self.session.request(
            method=method.upper(),
            url=url,
            headers=headers,
            data=payload if body is not None else None,
            timeout=timeout,
        )

        try:
            data = resp.json()
        except ValueError as exc:
            raise DeltaAPIError(
                f"Invalid JSON from Delta India ({resp.status_code}): {resp.text[:200]}"
            ) from exc

        if not resp.ok or not data.get("success", False):
            err = data.get("error", data)
            raise DeltaAPIError(f"Delta API error ({resp.status_code}) on {path}: {err}")

        return data.get("result")

    def get_public_ip(self, timeout: int = 5) -> str:
        """Return the current machine's public IPv4 address for Delta IP whitelist checks."""
        endpoints = (
            ("https://api.ipify.org?format=json", True),
            ("https://ifconfig.me/ip", False),
        )
        for url, is_json in endpoints:
            try:
                resp = self.session.get(url, timeout=timeout)
                if resp.ok:
                    if is_json:
                        ip = resp.json().get("ip", "").strip()
                    else:
                        ip = resp.text.strip()
                    if ip:
                        return ip
            except requests.RequestException:
                continue
        return "Unavailable"

    def fetch_ticker(self, symbol: str) -> Dict[str, Any]:
        """Fetch live ticker metadata, mark price, and contract specs for a symbol."""
        result = self._request("GET", f"/v2/tickers/{symbol.upper()}", authenticated=False)
        self._product_cache[symbol.upper()] = result
        return result

    def fetch_perpetual_tickers(self, operational_only: bool = True) -> List[Dict[str, Any]]:
        """Fetch all perpetual futures tickers from Delta India sorted by 24h USD turnover."""
        tickers = (
            self._request(
                "GET",
                "/v2/tickers",
                params={"contract_types": "perpetual_futures"},
                authenticated=False,
            )
            or []
        )
        filtered: List[Dict[str, Any]] = []
        for item in tickers:
            sym = str(item.get("symbol", "")).upper()
            if not sym:
                continue
            if operational_only and item.get("product_trading_status") != "operational":
                continue
            self._product_cache[sym] = item
            filtered.append(item)

        filtered.sort(key=lambda x: float(x.get("turnover_usd") or 0.0), reverse=True)
        return filtered

    def fetch_mark_price(self, symbol: str) -> float:
        """Return the live mark price for a contract symbol."""
        ticker = self.fetch_ticker(symbol)
        mark = ticker.get("mark_price") or ticker.get("close") or ticker.get("spot_price")
        if mark is None:
            raise DeltaAPIError(f"Mark price unavailable for {symbol}")
        return float(mark)

    def fetch_ohlcv_rest(
        self,
        symbol: str,
        timeframe: str = "15m",
        limit: int = 250,
    ) -> pd.DataFrame:
        """Fetch historical OHLCV candles directly via /v2/history/candles sorted oldest-first."""
        clean_symbol = symbol.upper()
        tf_seconds_map = {
            "1m": 60,
            "3m": 180,
            "5m": 300,
            "15m": 900,
            "30m": 1800,
            "1h": 3600,
            "2h": 7200,
            "4h": 14400,
            "1d": 86400,
        }
        step = tf_seconds_map.get(timeframe, 900)
        end_ts = int(time.time())
        start_ts = end_ts - step * (limit + 5)

        candles = self._request(
            "GET",
            "/v2/history/candles",
            params={
                "symbol": clean_symbol,
                "resolution": timeframe,
                "start": start_ts,
                "end": end_ts,
            },
            authenticated=False,
        )
        if not candles:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

        df = pd.DataFrame(candles)
        df = df.rename(columns={"time": "timestamp"})
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", utc=True).dt.tz_convert(
            "Asia/Kolkata"
        )
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = df[col].astype(float)
        return df[["timestamp", "open", "high", "low", "close", "volume"]].sort_values(
            "timestamp"
        ).reset_index(drop=True)

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "15m",
        limit: int = 250,
    ) -> pd.DataFrame:
        """Fetch historical OHLCV candles sorted oldest-to-newest."""
        clean_symbol = symbol.upper()
        try:
            exchange = self._get_ccxt()
            ccxt_sym = self.to_ccxt_symbol(clean_symbol)
            raw = exchange.fetch_ohlcv(ccxt_sym, timeframe=timeframe, limit=limit)
            if raw:
                df = pd.DataFrame(
                    raw, columns=["timestamp", "open", "high", "low", "close", "volume"]
                )
                df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True).dt.tz_convert(
                    "Asia/Kolkata"
                )
                return df.sort_values("timestamp").reset_index(drop=True)
        except Exception:
            # Fallback to direct REST /v2/history/candles if CCXT market lookup fails
            pass

        return self.fetch_ohlcv_rest(clean_symbol, timeframe=timeframe, limit=limit)

    def fetch_ohlcv_batch(
        self,
        symbols: List[str],
        timeframe: str = "15m",
        limit: int = 250,
        max_workers: int = 8,
    ) -> Tuple[Dict[str, pd.DataFrame], List[str]]:
        """Fetch OHLCV candles concurrently for many contracts."""
        frames: Dict[str, pd.DataFrame] = {}
        errors: List[str] = []
        if not symbols:
            return frames, errors

        use_rest_fast_path = (
            len(symbols) > 5 and type(self).fetch_ohlcv is DeltaIndiaClient.fetch_ohlcv
        )

        def _fetch_one(sym: str) -> Tuple[str, pd.DataFrame]:
            if use_rest_fast_path:
                return sym, self.fetch_ohlcv_rest(sym, timeframe=timeframe, limit=limit)
            return sym, self.fetch_ohlcv(sym, timeframe=timeframe, limit=limit)

        workers = min(max_workers, len(symbols))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_fetch_one, sym): sym for sym in symbols}
            for fut in as_completed(futures):
                sym = futures[fut]
                try:
                    _, df = fut.result()
                    frames[sym] = df
                except Exception as exc:
                    errors.append(f"{sym}: {exc}")

        return frames, errors

    def fetch_inr_wallet_balance(self) -> Dict[str, Any]:
        """Fetch authenticated wallet balances and extract INR (or converted USD) balance."""
        balances = self._request("GET", "/v2/wallet/balances", authenticated=True) or []
        inr_entry = None
        usd_entry = None

        for item in balances:
            asset = str(item.get("asset_symbol", "")).upper()
            if asset == "INR":
                inr_entry = item
            elif asset in ("USD", "USDT"):
                usd_entry = item

        if inr_entry is not None:
            available = float(inr_entry.get("available_balance", 0.0))
            total = float(inr_entry.get("balance", available))
            return {
                "currency": "INR",
                "inr_available": available,
                "inr_total": total,
                "raw": balances,
            }

        if usd_entry is not None:
            usd_avail = float(usd_entry.get("available_balance", 0.0))
            usd_total = float(usd_entry.get("balance", usd_avail))
            rate = self.config.usd_to_inr_rate
            return {
                "currency": str(usd_entry.get("asset_symbol", "USD")).upper(),
                "inr_available": usd_avail * rate,
                "inr_total": usd_total * rate,
                "usd_available": usd_avail,
                "usd_total": usd_total,
                "raw": balances,
            }

        return {
            "currency": "INR",
            "inr_available": 0.0,
            "inr_total": 0.0,
            "raw": balances,
        }

    def fetch_open_positions(self) -> List[Dict[str, Any]]:
        """Fetch active margined positions from Delta Exchange India."""
        raw = self._request("GET", "/v2/positions/margined", authenticated=True) or []
        active: List[Dict[str, Any]] = []
        for pos in raw:
            size = float(pos.get("size", 0))
            if size == 0:
                continue
            side = "BUY" if size > 0 else "SELL"
            active.append(
                {
                    "symbol": pos.get("product_symbol", ""),
                    "product_id": int(pos.get("product_id", 0)),
                    "side": side,
                    "contracts": abs(int(size)),
                    "entry_price": float(pos.get("entry_price", 0.0)),
                    "mark_price": float(pos.get("mark_price", pos.get("entry_price", 0.0))),
                    "unrealized_pnl_usd": float(pos.get("unrealized_pnl", 0.0)),
                    "unrealized_pnl_inr": float(pos.get("unrealized_pnl", 0.0))
                    * self.config.usd_to_inr_rate,
                    "margin_inr": float(pos.get("margin", 0.0)) * self.config.usd_to_inr_rate,
                }
            )
        return active

    def set_leverage(self, product_id: int, leverage: int) -> Any:
        """Set order leverage for a product before placing live orders."""
        return self._request(
            "POST",
            f"/v2/products/{product_id}/orders/leverage",
            body={"leverage": str(int(leverage))},
            authenticated=True,
        )

    @staticmethod
    def round_to_tick(price: float, tick_size: float) -> str:
        """Format price rounded cleanly to the contract's tick size."""
        if tick_size <= 0:
            return f"{price:.2f}"
        rounded = round(price / tick_size) * tick_size
        decimals = max(0, -int(math.floor(math.log10(tick_size)))) if tick_size < 1 else 2
        return f"{rounded:.{decimals}f}"

    def calculate_contract_size(
        self,
        symbol: str,
        margin_inr: float,
        leverage: int,
        mark_price: float,
        contract_value: Optional[float] = None,
    ) -> int:
        """Compute integer Delta contracts from INR margin, leverage, and contract value."""
        if contract_value is None:
            ticker = self._product_cache.get(symbol.upper()) or self.fetch_ticker(symbol)
            contract_value = float(ticker.get("contract_value", 0.001))

        notional_usd = (margin_inr / self.config.usd_to_inr_rate) * leverage
        one_contract_usd = mark_price * float(contract_value)
        if one_contract_usd <= 0:
            return 1
        return max(1, int(notional_usd / one_contract_usd))

    def place_bracket_order(
        self,
        symbol: str,
        side: str,
        size: int,
        stop_loss_price: float,
        take_profit_price: float,
        leverage: int = 3,
    ) -> Dict[str, Any]:
        """Place a market entry order with attached bracket SL and TP on Delta India."""
        ticker = self.fetch_ticker(symbol)
        product_id = int(ticker["product_id"])
        tick_size = float(ticker.get("tick_size", 0.5))

        self.set_leverage(product_id, leverage)

        payload = {
            "product_id": product_id,
            "size": int(size),
            "side": "buy" if side.upper() == "BUY" else "sell",
            "order_type": "market_order",
            "bracket_stop_loss_price": self.round_to_tick(stop_loss_price, tick_size),
            "bracket_take_profit_price": self.round_to_tick(take_profit_price, tick_size),
            "bracket_stop_trigger_method": "mark_price",
        }
        return self._request("POST", "/v2/orders", body=payload, authenticated=True)

    def close_position(
        self,
        symbol: str,
        product_id: Optional[int] = None,
        side: Optional[str] = None,
        contracts: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Cancel open orders for the symbol and close the live position with reduce_only."""
        if product_id is None or side is None or contracts is None:
            positions = [p for p in self.fetch_open_positions() if p["symbol"] == symbol.upper()]
            if not positions:
                return {"status": "no_open_position", "symbol": symbol}
            pos = positions[0]
            product_id = pos["product_id"]
            side = pos["side"]
            contracts = pos["contracts"]

        self._request(
            "DELETE",
            "/v2/orders/all",
            body={"product_id": int(product_id)},
            authenticated=True,
        )

        close_side = "sell" if str(side).upper() == "BUY" else "buy"
        return self._request(
            "POST",
            "/v2/orders",
            body={
                "product_id": int(product_id),
                "size": int(contracts),
                "side": close_side,
                "order_type": "market_order",
                "reduce_only": True,
            },
            authenticated=True,
        )

    def close_all_positions(self) -> List[Dict[str, Any]]:
        """Emergency kill-switch action: cancel all pending orders and close all live positions."""
        results: List[Dict[str, Any]] = []
        try:
            self._request("DELETE", "/v2/orders/all", body={}, authenticated=True)
        except DeltaAPIError:
            pass

        try:
            resp = self._request("POST", "/v2/positions/close_all", body={}, authenticated=True)
            results.append({"close_all": resp})
            return results
        except DeltaAPIError:
            pass

        for pos in self.fetch_open_positions():
            results.append(
                self.close_position(
                    symbol=pos["symbol"],
                    product_id=pos["product_id"],
                    side=pos["side"],
                    contracts=pos["contracts"],
                )
            )
        return results
