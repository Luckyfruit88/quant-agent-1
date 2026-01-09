from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from logger import log_event


def fetch_ohlcv_data(exchange, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
    raw = exchange.fetch_ohlcv(symbol, timeframe, limit)
    df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    return df


def calculate_macd(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    df = df.copy()
    df["ema_fast"] = df["close"].ewm(span=fast, adjust=False).mean()
    df["ema_slow"] = df["close"].ewm(span=slow, adjust=False).mean()
    df["macd"] = df["ema_fast"] - df["ema_slow"]
    df["signal"] = df["macd"].ewm(span=signal, adjust=False).mean()
    df["hist"] = df["macd"] - df["signal"]
    return df


def _fvg_filled(df: pd.DataFrame, fvg: Dict[str, Any]) -> bool:
    last_close = df["close"].iloc[-1]
    if fvg["type"] == "bullish":
        return last_close <= fvg["bottom"]
    return last_close >= fvg["top"]


def detect_fvg(df: pd.DataFrame, existing_fvgs: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    active: List[Dict[str, Any]] = []
    last_idx = len(df) - 1

    for fvg in existing_fvgs or []:
        if fvg.get("expiry_index", 0) >= last_idx and not _fvg_filled(df, fvg):
            active.append(fvg)

    for idx in range(2, len(df)):
        c1 = df.iloc[idx - 2]
        c3 = df.iloc[idx]
        fvg_type: Optional[str] = None
        top: Optional[float] = None
        bottom: Optional[float] = None

        if c3["low"] > c1["high"]:
            fvg_type = "bullish"
            top = float(c3["low"])
            bottom = float(c1["high"])
        elif c3["high"] < c1["low"]:
            fvg_type = "bearish"
            top = float(c1["high"])
            bottom = float(c3["low"])

        if fvg_type is None:
            continue

        fvg = {
            "type": fvg_type,
            "top": top,
            "bottom": bottom,
            "mid": (top + bottom) / 2,
            "candle1_idx": idx - 2,
            "detected_idx": idx,
            "expiry_index": idx + 20,
            "detected_at": df["timestamp"].iloc[idx].isoformat(),
        }
        active.append(fvg)
        active = sorted(active, key=lambda x: x.get("detected_idx", 0), reverse=True)[:3]

    return active


def _recent_crossover(df: pd.DataFrame, lookback: int, direction: str) -> bool:
    if len(df) < lookback + 1:
        return True
    macd = df["macd"].iloc[-lookback - 1 :]
    signal = df["signal"].iloc[-lookback - 1 :]
    diff = macd - signal
    signs = np.sign(diff)
    for i in range(1, len(signs)):
        if signs.iloc[i - 1] < 0 < signs.iloc[i] and direction == "bullish":
            return True
        if signs.iloc[i - 1] > 0 > signs.iloc[i] and direction == "bearish":
            return True
    return False


def check_entry_trigger(
    df: pd.DataFrame,
    fvg_list: List[Dict[str, Any]],
    macd_recent_crossover: bool = True,
    crossover_lookback: int = 6,
) -> List[Dict[str, Any]]:
    signals: List[Dict[str, Any]] = []
    if len(df) < 3:
        return signals

    trigger_idx = len(df) - 1
    trigger_row = df.iloc[trigger_idx]

    for fvg in fvg_list:
        if fvg.get("expiry_index", 0) < trigger_idx:
            continue

        if fvg["type"] == "bullish":
            touched = trigger_row["low"] <= fvg["mid"] <= trigger_row["high"]
            macd_ok = trigger_row["macd"] > trigger_row["signal"]
            direction = "bullish"
            sl = fvg["bottom"]
        else:
            touched = trigger_row["high"] >= fvg["mid"] >= trigger_row["low"]
            macd_ok = trigger_row["macd"] < trigger_row["signal"]
            direction = "bearish"
            sl = fvg["top"]

        if not touched or not macd_ok:
            continue

        if macd_recent_crossover and not _recent_crossover(df, crossover_lookback, direction):
            continue

        entry_price = float(trigger_row["close"])
        if direction == "bullish" and sl >= entry_price:
            continue
        if direction == "bearish" and sl <= entry_price:
            continue

        risk = abs(entry_price - sl)
        tp = entry_price + 2 * risk if direction == "bullish" else entry_price - 2 * risk
        side = "buy" if direction == "bullish" else "sell"

        signals.append(
            {
                "symbol": df.attrs.get("symbol"),
                "direction": direction,
                "side": side,
                "entry_price": entry_price,
                "sl": sl,
                "tp": tp,
                "fvg": fvg,
                "trigger_time": trigger_row["timestamp"].isoformat(),
                "macd": float(trigger_row["macd"]),
                "signal_line": float(trigger_row["signal"]),
            }
        )

    return signals


def calculate_position_size(balance: float, risk_pct: float, entry: float, sl: float) -> float:
    if entry <= 0 or sl <= 0:
        return 0.0
    risk_amount = balance * risk_pct
    unit_risk = abs(entry - sl)
    if unit_risk == 0:
        return 0.0
    return risk_amount / unit_risk


def execute_trade(
    exchange,
    symbol: str,
    side: str,
    amount: float,
    sl: float,
    tp: float,
    paper: bool,
    position_manager=None,
):
    entry_order = exchange.create_market_order(symbol, side, amount)
    opp_side = "sell" if side == "buy" else "buy"
    sl_order = exchange.create_stop_order(symbol, opp_side, amount, sl)
    tp_order = exchange.create_take_profit_order(symbol, opp_side, amount, tp)

    position_payload = {
        "symbol": symbol,
        "side": side,
        "amount": amount,
        "entry_price": entry_order.get("price"),
        "stop_loss": sl,
        "take_profit": tp,
        "status": "open",
        "entry_time": pd.Timestamp.utcnow().isoformat(),
        "order_ids": {
            "entry": entry_order.get("id"),
            "sl": sl_order.get("id"),
            "tp": tp_order.get("id"),
        },
    }

    if position_manager is not None:
        position_manager.open_position(symbol, position_payload)

    return position_payload


def manage_positions(exchange, position_manager, symbols: List[str]) -> None:
    for symbol in symbols:
        if not position_manager.has_open_position(symbol):
            continue
        price = exchange.fetch_price(symbol)
        if price is None:
            continue
        position = position_manager.positions.get(symbol, {})
        side = position.get("side")
        sl = position.get("stop_loss")
        tp = position.get("take_profit")

        if side == "buy":
            if price <= sl:
                position_manager.close_position(symbol, price, "stop_loss")
            elif price >= tp:
                position_manager.close_position(symbol, price, "take_profit")
        else:
            if price >= sl:
                position_manager.close_position(symbol, price, "stop_loss")
            elif price <= tp:
                position_manager.close_position(symbol, price, "take_profit")
