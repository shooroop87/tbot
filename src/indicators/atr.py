"""
Average True Range (ATR) - индикатор волатильности.

ATR показывает средний диапазон движения цены за период.
Используется для:
- Расчёта стоп-лосса (например, 0.3 × ATR)
- Определения размера позиции
- Оценки волатильности инструмента
"""
from typing import Optional, Dict, Any

import pandas as pd
import structlog

from indicators.utils import filter_trading_hours, validate_candles_df

logger = structlog.get_logger()


def calculate_true_range(df: pd.DataFrame) -> pd.Series:
    """
    Рассчитывает True Range для каждой свечи.
    
    True Range = max(
        High - Low,
        |High - Previous Close|,
        |Low - Previous Close|
    )
    
    Args:
        df: DataFrame с колонками high, low, close
    
    Returns:
        Series с True Range
    """
    df = df.copy()
    df["prev_close"] = df["close"].shift(1)
    
    tr1 = df["high"] - df["low"]
    tr2 = abs(df["high"] - df["prev_close"])
    tr3 = abs(df["low"] - df["prev_close"])
    
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    
    return true_range


def calculate_atr(
    df: pd.DataFrame,
    period: int = 14,
    method: str = "ema"
) -> Optional[float]:
    """
    Рассчитывает ATR (Average True Range).
    
    Args:
        df: DataFrame с колонками high, low, close
        period: Период ATR (по умолчанию 14)
        method: Метод усреднения:
            - "ema": Exponential Moving Average (более отзывчивый)
            - "sma": Simple Moving Average (классический)
    
    Returns:
        Значение ATR или None если недостаточно данных
    
    Example:
        >>> df = pd.DataFrame({"high": [...], "low": [...], "close": [...]})
        >>> atr = calculate_atr(df, period=14)
        >>> print(f"ATR14 = {atr:.2f}")
    """
    if not validate_candles_df(df, required_rows=period + 1):
        return None
    
    true_range = calculate_true_range(df)
    
    if method == "ema":
        # EMA более отзывчив к последним изменениям
        atr = true_range.ewm(span=period, adjust=False).mean().iloc[-1]
    else:
        # SMA - классический метод
        atr = true_range.rolling(window=period).mean().iloc[-1]
    
    return round(atr, 4)


def calculate_atr_series(
    df: pd.DataFrame,
    period: int = 14,
    method: str = "ema"
) -> pd.Series:
    """
    Рассчитывает ATR для всего DataFrame (для бэктестинга).
    
    Args:
        df: DataFrame с OHLC
        period: Период ATR
        method: ema/sma
    
    Returns:
        Series с ATR значениями
    """
    true_range = calculate_true_range(df)
    
    if method == "ema":
        atr_series = true_range.ewm(span=period, adjust=False).mean()
    else:
        atr_series = true_range.rolling(window=period).mean()
    
    return atr_series


def calculate_atr_from_candles(
    candles: list,
    period: int = 14,
    start_hour: int = 10,
    end_hour: int = 19
) -> Optional[Dict[str, Any]]:
    """
    Рассчитывает ATR из списка свечей с фильтрацией по часам.
    
    Это основная функция для вызова из бота.
    
    Args:
        candles: Список свечей из API
        period: Период ATR
        start_hour: Начало торговых часов
        end_hour: Конец торговых часов
    
    Returns:
        Dict с atr и atr_pct или None
    """
    # Фильтруем по торговым часам
    df = filter_trading_hours(candles, start_hour, end_hour)
    
    if df.empty or len(df) < period + 1:
        logger.warning(
            "atr_insufficient_data",
            candles=len(candles),
            filtered=len(df),
            required=period + 1
        )
        return None
    
    atr = calculate_atr(df, period)
    
    if atr is None:
        return None
    
    last_close = df["close"].iloc[-1]
    atr_pct = round(atr / last_close * 100, 2) if last_close > 0 else 0
    
    return {
        "atr": atr,
        "atr_pct": atr_pct,
        "last_close": last_close,
        "candles_used": len(df),
    }
