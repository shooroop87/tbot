"""
Bollinger Bands - индикатор волатильности и уровней.

Состоит из:
- Средняя линия (SMA)
- Верхняя линия = SMA + (σ × multiplier)
- Нижняя линия = SMA - (σ × multiplier)

Используется для:
- Определения уровней входа (отскок от нижней линии)
- Оценки перекупленности/перепроданности
- Определения сужения волатильности (squeeze)
"""
from typing import Optional, Dict, Any

import pandas as pd
import structlog

from indicators.utils import filter_trading_hours, validate_candles_df

logger = structlog.get_logger()


def calculate_bollinger_bands(
    df: pd.DataFrame,
    period: int = 20,
    std_multiplier: float = 2.0
) -> Optional[Dict[str, float]]:
    """
    Рассчитывает линии Боллинджера.
    
    Args:
        df: DataFrame с колонкой close
        period: Период SMA (по умолчанию 20)
        std_multiplier: Множитель σ (по умолчанию 2.0)
    
    Returns:
        Dict с upper, middle, lower, std, bandwidth
        или None если недостаточно данных
    
    Example:
        >>> bb = calculate_bollinger_bands(df, period=20, std_multiplier=2.0)
        >>> print(f"Lower BB: {bb['lower']:.2f}")
    """
    if not validate_candles_df(df, required_rows=period):
        return None

    close = df["close"]

    # Средняя линия = SMA
    middle = close.rolling(window=period).mean().iloc[-1]

    # Стандартное отклонение
    std = close.rolling(window=period).std().iloc[-1]

    # Верхняя и нижняя линии
    upper = middle + (std * std_multiplier)
    lower = middle - (std * std_multiplier)

    # Ширина полос в % (Bandwidth)
    bandwidth = (upper - lower) / middle * 100 if middle > 0 else 0

    return {
        "upper": round(upper, 2),
        "middle": round(middle, 2),
        "lower": round(lower, 2),
        "std": round(std, 4),
        "bandwidth": round(bandwidth, 2),
    }


def calculate_bollinger_series(
    df: pd.DataFrame,
    period: int = 20,
    std_multiplier: float = 2.0
) -> pd.DataFrame:
    """
    Рассчитывает Bollinger Bands для всего DataFrame.
    
    Args:
        df: DataFrame с close
        period: Период SMA
        std_multiplier: Множитель σ
    
    Returns:
        DataFrame с колонками bb_upper, bb_middle, bb_lower, bb_bandwidth
    """
    close = df["close"]

    bb_middle = close.rolling(window=period).mean()
    bb_std = close.rolling(window=period).std()
    
    bb_upper = bb_middle + (bb_std * std_multiplier)
    bb_lower = bb_middle - (bb_std * std_multiplier)
    bb_bandwidth = (bb_upper - bb_lower) / bb_middle * 100

    result = pd.DataFrame({
        "bb_upper": bb_upper,
        "bb_middle": bb_middle,
        "bb_lower": bb_lower,
        "bb_bandwidth": bb_bandwidth,
    })

    return result


def calculate_bb_position(price: float, bb: Dict[str, float]) -> Dict[str, Any]:
    """
    Определяет позицию цены относительно полос Боллинджера.
    
    Args:
        price: Текущая цена
        bb: Dict с upper, middle, lower
    
    Returns:
        Dict с:
        - position: "above_upper" | "upper_half" | "lower_half" | "below_lower"
        - distance_to_lower_pct: расстояние до нижней линии в %
        - distance_to_upper_pct: расстояние до верхней линии в %
        - percent_b: %B индикатор (0 = на нижней, 1 = на верхней)
    """
    lower = bb["lower"]
    upper = bb["upper"]
    middle = bb["middle"]

    # %B = (Price - Lower) / (Upper - Lower)
    band_width = upper - lower
    percent_b = (price - lower) / band_width if band_width > 0 else 0.5

    # Расстояние до линий в %
    distance_to_lower_pct = (price - lower) / price * 100 if price > 0 else 0
    distance_to_upper_pct = (upper - price) / price * 100 if price > 0 else 0

    # Определяем позицию
    if price > upper:
        position = "above_upper"
    elif price > middle:
        position = "upper_half"
    elif price > lower:
        position = "lower_half"
    else:
        position = "below_lower"

    return {
        "position": position,
        "percent_b": round(percent_b, 3),
        "distance_to_lower_pct": round(distance_to_lower_pct, 2),
        "distance_to_upper_pct": round(distance_to_upper_pct, 2),
    }


def calculate_bb_from_candles(
    candles: list,
    period: int = 20,
    std_multiplier: float = 2.0,
    start_hour: int = 10,
    end_hour: int = 19
) -> Optional[Dict[str, Any]]:
    """
    Рассчитывает Bollinger Bands из списка свечей.
    
    Это основная функция для вызова из бота.
    
    Args:
        candles: Список свечей из API
        period: Период SMA
        std_multiplier: Множитель σ
        start_hour: Начало торговых часов
        end_hour: Конец торговых часов
    
    Returns:
        Dict с параметрами BB или None
    """
    # Фильтруем по торговым часам
    df = filter_trading_hours(candles, start_hour, end_hour)

    if df.empty or len(df) < period:
        logger.warning(
            "bollinger_insufficient_data",
            candles=len(candles),
            filtered=len(df),
            required=period
        )
        return None

    bb = calculate_bollinger_bands(df, period, std_multiplier)

    if bb is None:
        return None

    last_close = df["close"].iloc[-1]
    bb_position = calculate_bb_position(last_close, bb)

    return {
        **bb,
        **bb_position,
        "last_close": last_close,
        "candles_used": len(df),
    }
