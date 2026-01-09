## Quant Agent 1

Swing-trading bot for 4H charts using Fair Value Gap (FVG) logic with MACD confirmation. Exchange connectivity uses CCXT; analysis uses pandas.

### Features
- FVG detection (3-candle pattern) with lifecycle limits and storage of up to 3 active gaps per symbol
- Entry on mid-point touch with MACD confirmation; structural SL and 1:2 RR TP
- Position sizing by risk %, daily loss guard, and per-symbol/portfolio position caps
- Paper trading and live modes (sandbox toggle), plus simple backtest runner
- JSON state persistence for active FVGs and positions
- Structured JSON logging for signals, rejections, executions, and PnL updates

### Project Structure
- main.py — entry point with live loop and backtest driver
- strategy.py — signal logic (MACD, FVG, sizing, triggers, execution helpers)
- exchange_manager.py — CCXT wrapper with retries, ticker/ohlcv access, and paper-order simulation
- position_manager.py — state persistence, position tracking, daily loss checks
- logger.py — structured logging helper
- config.json — editable settings (symbols, risk, exchange keys, toggles)
- state.json — persisted runtime state (auto-updated)
- tests/ — unit tests for core calculations
- requirements.txt — dependencies

### Setup
1) Install deps
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2) Configure
- Edit config.json (symbols, risk, limits). Set paper_trading to false for live; provide API keys (or via env vars API_KEY and API_SECRET). Sandbox mode can be left on for testnet.

3) Run live loop (paper by default)
```bash
python main.py --config config.json
```
The loop wakes every 4H close (plus 30s buffer), manages existing positions, scans symbols sequentially, and persists state.

4) Run backtest (quick equity estimate over recent history)
```bash
python main.py --config config.json --backtest
```

5) Tests
```bash
pytest
```

### Notes
- Daily loss guard stops new entries after 5% drawdown from the current day start balance in paper/live modes.
- Paper trading simulates fills at current ticker prices and tracks virtual balance in state.json.
- Min order size checks use exchange market metadata when available; signals below minimum are logged and skipped.

