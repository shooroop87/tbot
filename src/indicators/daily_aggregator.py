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


def calculate_ema_trend(df: pd.DataFrame) -> Dict[str, Any]:
    """
    Рассчитывает тренд по двум EMA (13/26) — классика Элдера.
    
    Логика:
    - EMA(13) > EMA(26) и обе растут → UP
    - EMA(13) < EMA(26) → DOWN
    - Иначе → FLAT
    
    Returns:
        Dict с ema_13, ema_26, ema_trend, ema_diff_pct, ema_13_slope, ema_26_slope
    """
    if len(df) < 26:
        return {
            "ema_13": None,
            "ema_26": None,
            "ema_trend": "FLAT",
            "ema_diff_pct": 0,
            "ema_13_slope": 0,
            "ema_26_slope": 0,
        }
    
    close = df["close"]
    
    # Рассчитываем EMA
    ema_13 = close.ewm(span=13, adjust=False).mean()
    ema_26 = close.ewm(span=26, adjust=False).mean()
    
    # Текущие значения
    ema_13_now = ema_13.iloc[-1]
    ema_26_now = ema_26.iloc[-1]
    
    # Значения 3 дня назад (для расчёта slope)
    ema_13_3d = ema_13.iloc[-4] if len(ema_13) >= 4 else ema_13.iloc[0]
    ema_26_3d = ema_26.iloc[-4] if len(ema_26) >= 4 else ema_26.iloc[0]
    
    # Наклон (slope) — изменение за 3 дня в %
    ema_13_slope = (ema_13_now - ema_13_3d) / ema_13_3d * 100 if ema_13_3d > 0 else 0
    ema_26_slope = (ema_26_now - ema_26_3d) / ema_26_3d * 100 if ema_26_3d > 0 else 0
    
    # Разница между EMA в %
    ema_diff_pct = (ema_13_now - ema_26_now) / ema_26_now * 100 if ema_26_now > 0 else 0
    
    # Определяем тренд
    # UP: быстрая выше медленной И обе растут (slope > 0)
    # DOWN: быстрая ниже медленной
    # FLAT: переходное состояние
    
    if ema_13_now > ema_26_now and ema_13_slope > 0 and ema_26_slope > 0:
        trend = "UP"
    elif ema_13_now < ema_26_now:
        trend = "DOWN"
    elif ema_13_now > ema_26_now and (ema_13_slope <= 0 or ema_26_slope <= 0):
        # Быстрая выше, но одна из EMA падает — ослабление тренда
        trend = "WEAK_UP"
    else:
        trend = "FLAT"
    
    return {
        "ema_13": round(ema_13_now, 2),
        "ema_26": round(ema_26_now, 2),
        "ema_trend": trend,
        "ema_diff_pct": round(ema_diff_pct, 2),
        "ema_13_slope": round(ema_13_slope, 2),
        "ema_26_slope": round(ema_26_slope, 2),
    }


def calculate_indicators(df: pd.DataFrame) -> Optional[Dict[str, Any]]:
    """Рассчитывает ATR(14), BB(20,2) и EMA(13/26) по дневным свечам."""
    if len(df) < 26:  # Нужно минимум 26 дней для EMA26
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
    close = last["close"]
    
    # EMA тренд (13/26)
    ema_data = calculate_ema_trend(df)
    
    # Расстояние цены от EMA
    ema_13 = ema_data["ema_13"]
    ema_26 = ema_data["ema_26"]
    
    distance_to_ema_13_pct = (close - ema_13) / ema_13 * 100 if ema_13 and ema_13 > 0 else 0
    distance_to_ema_26_pct = (close - ema_26) / ema_26 * 100 if ema_26 and ema_26 > 0 else 0
    
    return {
        "close": close,
        "atr": round(last["atr"], 2),
        "bb_lower": round(last["bb_lower"], 2),
        "bb_middle": round(last["bb_middle"], 2),
        "bb_upper": round(last["bb_upper"], 2),
        # EMA (13/26)
        "ema_13": ema_data["ema_13"],
        "ema_26": ema_data["ema_26"],
        "ema_trend": ema_data["ema_trend"],
        "ema_diff_pct": ema_data["ema_diff_pct"],
        "ema_13_slope": ema_data["ema_13_slope"],
        "ema_26_slope": ema_data["ema_26_slope"],
        "distance_to_ema_13_pct": round(distance_to_ema_13_pct, 2),
        "distance_to_ema_26_pct": round(distance_to_ema_26_pct, 2),
    }
