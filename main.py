import argparse
import json
import math
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List

from dotenv import load_dotenv

from exchange_manager import ExchangeManager
from logger import log_event, setup_logging
from position_manager import PositionManager
from strategy import (
    calculate_macd,
    calculate_position_size,
    check_entry_trigger,
    detect_fvg,
    execute_trade,
    fetch_ohlcv_data,
    manage_positions,
)


def load_config(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def wait_for_next_close(buffer_seconds: int = 30) -> None:
    interval = 4 * 60 * 60
    now = time.time()
    next_close = (math.floor(now / interval) + 1) * interval
    wait_seconds = max(0, next_close - now + buffer_seconds)
    time.sleep(wait_seconds)


def process_symbol(
    symbol: str,
    config: Dict[str, Any],
    exchange: ExchangeManager,
    position_manager: PositionManager,
    logger,
) -> None:
    timeframe = config.get("timeframe", "4h")
    limit = config.get("ohlcv_limit", 200)

    if position_manager.total_open_positions() >= config.get("max_concurrent_positions", 5):
        log_event(logger, "WARN", "Max concurrent positions reached, skipping cycle", {})
        return

    if position_manager.has_open_position(symbol):
        log_event(logger, "INFO", "Position already open, skipping new signals", {"symbol": symbol})
        return

    try:
        df = fetch_ohlcv_data(exchange, symbol, timeframe, limit)
    except Exception as exc:
        log_event(logger, "ERROR", "Failed to fetch OHLCV", {"symbol": symbol, "error": str(exc)})
        return

    if df.empty:
        log_event(logger, "WARN", "Empty OHLCV data", {"symbol": symbol})
        return

    df.attrs["symbol"] = symbol
    df = calculate_macd(df, config.get("macd_fast", 12), config.get("macd_slow", 26), config.get("macd_signal", 9))

    existing_fvgs = position_manager.get_fvgs(symbol)
    active_fvgs = detect_fvg(df, existing_fvgs)
    position_manager.update_fvgs(symbol, active_fvgs)

    for fvg in active_fvgs:
        log_event(
            logger,
            "INFO",
            "FVG detected",
            {
                "symbol": symbol,
                "type": fvg["type"],
                "top": fvg["top"],
                "bottom": fvg["bottom"],
                "mid": fvg["mid"],
                "expiry_index": fvg["expiry_index"],
            },
        )

    signals = check_entry_trigger(
        df,
        active_fvgs,
        macd_recent_crossover=config.get("macd_recent_crossover", True),
        crossover_lookback=config.get("crossover_lookback", 6),
    )

    if not signals:
        return

    balance = exchange.fetch_balance()
    if position_manager.hit_daily_loss_limit(balance):
        log_event(logger, "WARN", "Daily loss limit reached", {"balance": balance})
        return

    for signal in signals:
        entry = signal["entry_price"]
        sl = signal["sl"]
        amount = calculate_position_size(balance, config.get("risk_per_trade", 0.01), entry, sl)
        min_size = exchange.minimum_order_size(symbol)
        if min_size and amount < min_size:
            log_event(
                logger,
                "WARN",
                "Position size below minimum",
                {"symbol": symbol, "amount": amount, "min_size": min_size},
            )
            continue

        try:
            position = execute_trade(
                exchange,
                symbol,
                signal["side"],
                amount,
                sl,
                signal["tp"],
                config.get("paper_trading", True),
                position_manager=position_manager,
            )
            log_event(
                logger,
                "INFO",
                "Entry signal executed",
                {
                    "symbol": symbol,
                    "direction": signal["direction"],
                    "entry_price": position.get("entry_price"),
                    "sl": sl,
                    "tp": signal["tp"],
                    "amount": amount,
                    "macd": signal["macd"],
                    "signal_line": signal["signal_line"],
                },
            )
        except Exception as exc:
            log_event(logger, "ERROR", "Trade execution failed", {"symbol": symbol, "error": str(exc)})


def run_live(config: Dict[str, Any], exchange: ExchangeManager, position_manager: PositionManager, logger) -> None:
    while True:
        wait_for_next_close()
        manage_positions(exchange, position_manager, config["symbols"])
        for symbol in config["symbols"]:
            try:
                process_symbol(symbol, config, exchange, position_manager, logger)
            except Exception as exc:
                log_event(logger, "ERROR", "Symbol processing failed", {"symbol": symbol, "error": str(exc)})
        position_manager.save_state()


def run_backtest(config: Dict[str, Any], exchange: ExchangeManager, logger) -> None:
    timeframe = config.get("timeframe", "4h")
    days = config.get("backtest_days", 90)
    limit = days * 6 + 50
    symbols = config.get("symbols", [])
    results: List[Dict[str, Any]] = []

    for symbol in symbols:
        df = fetch_ohlcv_data(exchange, symbol, timeframe, limit)
        if df.empty:
            continue
        df.attrs["symbol"] = symbol
        df = calculate_macd(df, config.get("macd_fast", 12), config.get("macd_slow", 26), config.get("macd_signal", 9))
        active: List[Dict[str, Any]] = []
        balance = config.get("starting_balance", 10000.0)
        equity = balance
        open_pos: Dict[str, Any] = {}

        for idx in range(3, len(df)):
            window = df.iloc[: idx + 1]
            active = detect_fvg(window, active)
            signals = check_entry_trigger(
                window,
                active,
                macd_recent_crossover=config.get("macd_recent_crossover", True),
                crossover_lookback=config.get("crossover_lookback", 6),
            )
            if open_pos:
                price = window["close"].iloc[-1]
                if open_pos["side"] == "buy":
                    if price <= open_pos["stop_loss"] or price >= open_pos["take_profit"]:
                        pnl = (price - open_pos["entry_price"]) * open_pos["amount"]
                        equity += pnl
                        open_pos = {}
                else:
                    if price >= open_pos["stop_loss"] or price <= open_pos["take_profit"]:
                        pnl = (open_pos["entry_price"] - price) * open_pos["amount"]
                        equity += pnl
                        open_pos = {}

            if open_pos:
                continue
            if not signals:
                continue
            signal = signals[-1]
            entry = signal["entry_price"]
            sl = signal["sl"]
            amount = calculate_position_size(equity, config.get("risk_per_trade", 0.01), entry, sl)
            if amount <= 0:
                continue
            open_pos = {
                "side": signal["side"],
                "entry_price": entry,
                "stop_loss": sl,
                "take_profit": signal["tp"],
                "amount": amount,
            }

        results.append({"symbol": symbol, "equity": equity})
        log_event(logger, "INFO", "Backtest completed", {"symbol": symbol, "equity": equity})

    log_event(logger, "INFO", "Backtest summary", {"results": results})


def main() -> None:
    parser = argparse.ArgumentParser(description="Swing FVG trading bot")
    parser.add_argument("--config", default="config.json", help="Path to config JSON")
    parser.add_argument("--backtest", action="store_true", help="Run backtest instead of live loop")
    args = parser.parse_args()

    load_dotenv()
    config = load_config(args.config)
    logger = setup_logging(config.get("log_level", "INFO"))

    if not config.get("paper_trading", True):
        config["api_key"] = config.get("api_key") or os.getenv("API_KEY")
        config["api_secret"] = config.get("api_secret") or os.getenv("API_SECRET")

    position_manager = PositionManager(config.get("state_file", "state.json"), logger, config.get("daily_loss_limit_pct", 0.05))
    exchange = ExchangeManager(config, logger, position_manager)

    if args.backtest:
        run_backtest(config, exchange, logger)
    else:
        run_live(config, exchange, position_manager, logger)


if __name__ == "__main__":
    main()
