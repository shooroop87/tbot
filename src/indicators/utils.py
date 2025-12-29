"""
Утилиты для работы с индикаторами.

Функции:
- filter_trading_hours: фильтрация свечей по торговым часам
"""
from typing import List, Dict, Any

import pandas as pd
import pytz
import structlog

logger = structlog.get_logger()

MSK = pytz.timezone("Europe/Moscow")


def filter_trading_hours(
    candles: List[Dict[str, Any]],
    start_hour: int = 10,
    end_hour: int = 19,
    exclude_weekends: bool = True,
    timezone: str = "Europe/Moscow"
) -> pd.DataFrame:
    """
    Фильтрует свечи по торговым часам.
    
    Для расчёта ATR и Bollinger используем только часы
    основной торговой сессии MOEX (10:00-19:00 МСК).
    Это исключает:
    - Утреннюю сессию (до 10:00) с низкой ликвидностью
    - Вечернюю сессию (после 19:00)
    - Выходные дни
    
    Args:
        candles: Список свечей с полями time, open, high, low, close, volume
        start_hour: Начало торгового дня (по умолчанию 10)
        end_hour: Конец торгового дня (по умолчанию 19)
        exclude_weekends: Исключать субботу/воскресенье
        timezone: Часовой пояс
    
    Returns:
        DataFrame с отфильтрованными свечами
    """
    if not candles:
        logger.warning("filter_trading_hours: empty candles list")
        return pd.DataFrame()

    df = pd.DataFrame(candles)

    # Конвертируем время
    df["time"] = pd.to_datetime(df["time"])
    
    # Если время без timezone, считаем что UTC
    if df["time"].dt.tz is None:
        df["time"] = df["time"].dt.tz_localize("UTC")
    
    # Конвертируем в нужный часовой пояс
    tz = pytz.timezone(timezone)
    df["time_local"] = df["time"].dt.tz_convert(tz)

    # Фильтр по часам
    df["hour"] = df["time_local"].dt.hour
    mask_hours = (df["hour"] >= start_hour) & (df["hour"] < end_hour)

    # Фильтр по дням недели (0=пн, 6=вс)
    if exclude_weekends:
        df["weekday"] = df["time_local"].dt.weekday
        mask_weekday = df["weekday"] < 5
        df = df[mask_hours & mask_weekday].copy()
    else:
        df = df[mask_hours].copy()

    # Убираем вспомогательные колонки
    df = df.drop(columns=["hour", "weekday", "time_local"], errors="ignore")
    df = df.sort_values("time").reset_index(drop=True)

    logger.debug(
        "candles_filtered",
        original=len(candles),
        filtered=len(df),
        hours=f"{start_hour}-{end_hour}",
        exclude_weekends=exclude_weekends
    )
    
    return df


def validate_candles_df(df: pd.DataFrame, required_rows: int = 14) -> bool:
    """
    Проверяет DataFrame на достаточность данных.
    
    Args:
        df: DataFrame со свечами
        required_rows: Минимальное количество строк
    
    Returns:
        True если данных достаточно
    """
    if df.empty:
        return False
    
    required_columns = ["open", "high", "low", "close"]
    for col in required_columns:
        if col not in df.columns:
            logger.warning(f"validate_candles: missing column {col}")
            return False
    
    if len(df) < required_rows:
        logger.warning(
            "validate_candles: insufficient data",
            required=required_rows,
            got=len(df)
        )
        return False
    
    return True
