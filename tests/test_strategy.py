import pandas as pd

from strategy import calculate_macd, calculate_position_size, check_entry_trigger, detect_fvg


def test_macd_shapes():
    data = {
        "timestamp": pd.date_range("2023-01-01", periods=60, freq="4H"),
        "open": [1.0] * 60,
        "high": [1.1] * 60,
        "low": [0.9] * 60,
        "close": [1 + i * 0.01 for i in range(60)],
        "volume": [100] * 60,
    }
    df = pd.DataFrame(data)
    macd_df = calculate_macd(df)
    assert "macd" in macd_df.columns
    assert "signal" in macd_df.columns
    assert len(macd_df) == 60


def test_fvg_detection():
    # Construct candles to create a bullish FVG on candle 3
    records = [
        [1.0, 1.1, 0.9, 1.05],
        [1.05, 1.15, 1.0, 1.1],
        [1.2, 1.25, 1.15, 1.2],  # gap up, low > prior high
    ]
    df = pd.DataFrame(records * 3, columns=["open", "high", "low", "close"])
    df.insert(0, "timestamp", pd.date_range("2023-01-01", periods=len(df), freq="4H"))
    df["volume"] = 100
    fvgs = detect_fvg(df)
    assert fvgs, "Expected at least one FVG"
    assert fvgs[0]["type"] == "bullish"


def test_position_size():
    size = calculate_position_size(balance=10000, risk_pct=0.01, entry=100, sl=95)
    expected = (10000 * 0.01) / 5
    assert abs(size - expected) < 1e-6


def test_entry_trigger_mid_touch():
    rows = []
    for i in range(25):
        open_price = 100 + i
        close_price = open_price + 1
        high_price = close_price + 0.5
        low_price = open_price - 0.5
        rows.append([open_price, high_price, low_price, close_price])
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    df.insert(0, "timestamp", pd.date_range("2023-01-01", periods=len(df), freq="4H"))
    df["volume"] = 100
    df = calculate_macd(df)
    fvgs = detect_fvg(df)
    signals = check_entry_trigger(df, fvgs)
    assert isinstance(signals, list)
