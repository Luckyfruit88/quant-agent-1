import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from logger import log_event


class PositionManager:
    def __init__(self, state_file: str, logger, daily_loss_limit_pct: float = 0.05) -> None:
        self.state_file = state_file
        self.logger = logger
        self.daily_loss_limit_pct = daily_loss_limit_pct
        self.positions: Dict[str, Dict[str, Any]] = {}
        self.fvgs: Dict[str, Any] = {}
        self.paper_balance: float = 0.0
        self.daily: Dict[str, Any] = {"date": "", "start_balance": 0.0}
        self.load_state()

    def load_state(self) -> None:
        if not os.path.exists(self.state_file):
            return
        try:
            with open(self.state_file, "r", encoding="utf-8") as handle:
                data = json.load(handle)
                self.positions = data.get("positions", {})
                self.fvgs = data.get("fvgs", {})
                self.paper_balance = float(data.get("paper_balance", 0.0))
                self.daily = data.get("daily", {"date": "", "start_balance": 0.0})
        except Exception as exc:
            log_event(self.logger, "ERROR", "Failed to load state", {"error": str(exc)})

    def save_state(self) -> None:
        payload = {
            "positions": self.positions,
            "fvgs": self.fvgs,
            "paper_balance": self.paper_balance,
            "daily": self.daily,
        }
        try:
            with open(self.state_file, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
        except Exception as exc:
            log_event(self.logger, "ERROR", "Failed to persist state", {"error": str(exc)})

    def update_fvgs(self, symbol: str, fvgs) -> None:
        self.fvgs[symbol] = fvgs

    def get_fvgs(self, symbol: str):
        return self.fvgs.get(symbol, [])

    def has_open_position(self, symbol: str) -> bool:
        position = self.positions.get(symbol)
        return bool(position and position.get("status") == "open")

    def total_open_positions(self) -> int:
        return sum(1 for pos in self.positions.values() if pos.get("status") == "open")

    def open_position(self, symbol: str, data: Dict[str, Any]) -> None:
        self.positions[symbol] = data
        log_event(self.logger, "INFO", "Position opened", {"symbol": symbol, **data})

    def close_position(self, symbol: str, exit_price: float, reason: str) -> Optional[Dict[str, Any]]:
        position = self.positions.get(symbol)
        if not position or position.get("status") != "open":
            return None

        side = position.get("side")
        amount = position.get("amount", 0.0)
        entry_price = position.get("entry_price", 0.0)
        pnl_per_unit = exit_price - entry_price if side == "buy" else entry_price - exit_price
        pnl = pnl_per_unit * amount
        position.update(
            {
                "status": "closed",
                "exit_price": exit_price,
                "exit_reason": reason,
                "exit_time": datetime.now(timezone.utc).isoformat(),
                "pnl": pnl,
            }
        )
        if self.paper_balance is not None:
            self.paper_balance += pnl
        log_event(self.logger, "INFO", "Position closed", position)
        return position

    def enforce_daily_reset(self, balance: float) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        if self.daily.get("date") != today:
            self.daily = {"date": today, "start_balance": balance}

    def hit_daily_loss_limit(self, balance: float) -> bool:
        self.enforce_daily_reset(balance)
        start_balance = self.daily.get("start_balance", 0.0)
        if start_balance <= 0:
            return False
        max_loss = start_balance * self.daily_loss_limit_pct
        return balance <= start_balance - max_loss
