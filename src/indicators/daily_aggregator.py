"""
Агрегация часовых свечей в дневные.

Берём только торговые часы 10:00-19:00 МСК.
"""
from typing import List, Dict, Any, Optional

import pandas as pd
import pytz
import structlog

logger = structlog.get_logger()
MSK = pytz.timezone("Europe/Moscow")


def aggregate_hourly_to_daily(
    candles: List[Dict[str, Any]],
    start_hour: int = 10,
    end_hour: int = 19
) -> pd.DataFrame:
    """Агрегирует часовые свечи в дневные (10-19 МСК)."""
    if not candles:
        return pd.DataFrame()
    
    df = pd.DataFrame(candles)
    df["time"] = pd.to_datetime(df["time"])
    
    if df["time"].dt.tz is None:
        df["time"] = df["time"].dt.tz_localize("UTC")
    
    df["time_msk"] = df["time"].dt.tz_convert(MSK)
    df["hour"] = df["time_msk"].dt.hour
    df["weekday"] = df["time_msk"].dt.weekday
    df["date"] = df["time_msk"].dt.date
    
    # Фильтр: 10-19 МСК, пн-пт
    mask = (df["hour"] >= start_hour) & (df["hour"] < end_hour) & (df["weekday"] < 5)
    df_filtered = df[mask]
    
    if df_filtered.empty:
        return pd.DataFrame()
    
    # Агрегация
    daily = df_filtered.groupby("date").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum"
    }).reset_index()
    
    return daily.sort_values("date").reset_index(drop=True)


def calculate_indicators(df: pd.DataFrame) -> Optional[Dict[str, float]]:
    """Рассчитывает ATR(14) и BB(20,2) по дневным свечам."""
    if len(df) < 20:
        return None
    
    df = df.copy()
    
    # ATR(14)
    df["prev_close"] = df["close"].shift(1)
    df["tr"] = pd.concat([
        df["high"] - df["low"],
        abs(df["high"] - df["prev_close"]),
        abs(df["low"] - df["prev_close"])
    ], axis=1).max(axis=1)
    df["atr"] = df["tr"].ewm(span=14, adjust=False).mean()
    
    # BB(20, 2)
    df["bb_middle"] = df["close"].rolling(20).mean()
    df["bb_std"] = df["close"].rolling(20).std()
    df["bb_upper"] = df["bb_middle"] + 2 * df["bb_std"]
    df["bb_lower"] = df["bb_middle"] - 2 * df["bb_std"]
    
    last = df.iloc[-1]
    return {
        "close": last["close"],
        "atr": round(last["atr"], 2),
        "bb_lower": round(last["bb_lower"], 2),
        "bb_middle": round(last["bb_middle"], 2),
        "bb_upper": round(last["bb_upper"], 2),
    }