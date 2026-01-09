import math
import time
from typing import Any, Dict, Optional

import ccxt  # type: ignore

from logger import log_event


class ExchangeManager:
    """Wrapper around CCXT with simple retry and optional paper trading."""

    def __init__(self, config: Dict[str, Any], logger, position_manager=None) -> None:
        self.config = config
        self.logger = logger
        self.position_manager = position_manager
        self.paper = config.get("paper_trading", True)
        self.retry_attempts = 3
        self.backoff_base = 2
        self.exchange_id = config.get("exchange", "binance")
        self.client = self._init_client()

        if self.paper and self.position_manager is not None and self.position_manager.paper_balance == 0.0:
            self.position_manager.paper_balance = float(config.get("starting_balance", 10000.0))

    def _init_client(self):
        exchange_class = getattr(ccxt, self.exchange_id)
        params: Dict[str, Any] = {"enableRateLimit": True, "options": self.config.get("exchange_params", {})}
        if not self.paper:
            params.update({"apiKey": self.config.get("api_key"), "secret": self.config.get("api_secret")})
        client = exchange_class(params)
        if self.config.get("sandbox", False) and hasattr(client, "set_sandbox_mode"):
            try:
                client.set_sandbox_mode(True)
            except Exception:
                pass
        try:
            client.load_markets()
        except Exception as exc:
            log_event(self.logger, "ERROR", "Failed to load markets", {"error": str(exc)})
        return client

    def _call_with_retries(self, func, *args, **kwargs):
        for attempt in range(self.retry_attempts):
            try:
                return func(*args, **kwargs)
            except ccxt.RateLimitExceeded as exc:
                delay = self.backoff_base ** attempt
                log_event(self.logger, "WARN", "Rate limit hit, backing off", {"delay": delay, "error": str(exc)})
                time.sleep(delay)
            except ccxt.NetworkError as exc:
                delay = self.backoff_base ** attempt
                log_event(self.logger, "WARN", "Network error, retrying", {"delay": delay, "error": str(exc)})
                time.sleep(delay)
        raise RuntimeError("Max retries exceeded for exchange call")

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int):
        return self._call_with_retries(self.client.fetch_ohlcv, symbol, timeframe, limit=limit)

    def fetch_price(self, symbol: str) -> Optional[float]:
        try:
            ticker = self._call_with_retries(self.client.fetch_ticker, symbol)
            return ticker.get("last") or ticker.get("close")
        except Exception as exc:
            log_event(self.logger, "ERROR", "Failed to fetch ticker", {"symbol": symbol, "error": str(exc)})
            return None

    def fetch_balance(self) -> float:
        if self.paper:
            if self.position_manager is None:
                return float(self.config.get("starting_balance", 10000.0))
            return float(self.position_manager.paper_balance)
        try:
            balance = self._call_with_retries(self.client.fetch_balance)
            total = balance.get("total") or balance.get("free") or {}
            usdt = total.get("USDT") or total.get("USD")
            return float(usdt) if usdt is not None else 0.0
        except Exception as exc:
            log_event(self.logger, "ERROR", "Failed to fetch balance", {"error": str(exc)})
            return 0.0

    def create_market_order(self, symbol: str, side: str, amount: float) -> Dict[str, Any]:
        if self.paper:
            price = self.fetch_price(symbol)
            if price is None:
                raise RuntimeError("Price unavailable for paper order")
            cost = price * amount
            if self.position_manager is not None and cost > self.position_manager.paper_balance:
                raise RuntimeError("Insufficient paper balance")
            if self.position_manager is not None:
                self.position_manager.paper_balance -= cost
            order = {
                "id": f"paper-{int(time.time() * 1000)}",
                "symbol": symbol,
                "side": side,
                "amount": amount,
                "price": price,
                "timestamp": int(time.time() * 1000),
                "status": "closed",
            }
            log_event(self.logger, "INFO", "Paper market order executed", order)
            return order

        order = self._call_with_retries(self.client.create_order, symbol, "market", side, amount)
        log_event(self.logger, "INFO", "Live market order sent", {"id": order.get("id"), "symbol": symbol, "side": side, "amount": amount})
        return order

    def create_stop_order(self, symbol: str, side: str, amount: float, stop_price: float) -> Dict[str, Any]:
        if self.paper:
            order = {
                "id": f"paper-sl-{int(time.time() * 1000)}",
                "symbol": symbol,
                "side": side,
                "amount": amount,
                "stop": stop_price,
            }
            log_event(self.logger, "INFO", "Paper stop order recorded", order)
            return order

        params = {"stopPrice": stop_price}
        try:
            order = self._call_with_retries(self.client.create_order, symbol, "stop", side, amount, stop_price, params)
        except Exception:
            order = self._call_with_retries(self.client.create_order, symbol, "stop_market", side, amount, None, params)
        log_event(self.logger, "INFO", "Live stop order sent", {"id": order.get("id"), "stop": stop_price})
        return order

    def create_take_profit_order(self, symbol: str, side: str, amount: float, price: float) -> Dict[str, Any]:
        if self.paper:
            order = {
                "id": f"paper-tp-{int(time.time() * 1000)}",
                "symbol": symbol,
                "side": side,
                "amount": amount,
                "price": price,
            }
            log_event(self.logger, "INFO", "Paper take-profit order recorded", order)
            return order

        order = self._call_with_retries(self.client.create_order, symbol, "limit", side, amount, price)
        log_event(self.logger, "INFO", "Live take-profit order sent", {"id": order.get("id"), "price": price})
        return order

    def minimum_order_size(self, symbol: str) -> Optional[float]:
        try:
            market = self.client.market(symbol)
            return market.get("limits", {}).get("amount", {}).get("min")
        except Exception:
            return None
