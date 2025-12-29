"""
Расчёт размера позиции с учётом риска.

Принципы:
1. Размер позиции определяется РИСКОМ, а не "сколько хочу купить"
2. Два ограничителя: риск на сделку И макс. размер позиции
3. Берём МИНИМУМ из двух расчётов

Формула:
    Size by Risk = Max Loss ₽ / Stop Loss ₽ per share
    Size by Capital = (Deposit × Max Position %) / Price
    Final Size = min(Size by Risk, Size by Capital)
"""
from dataclasses import dataclass
from typing import Dict, Any

import structlog

from config import RiskConfig, TradingConfig

logger = structlog.get_logger()


@dataclass
class PositionSize:
    """Результат расчёта размера позиции."""
    size_shares: int          # Кол-во акций/контрактов
    size_lots: int            # Кол-во лотов
    stop_rub: float           # Стоп-лосс в рублях на 1 шт
    stop_pct: float           # Стоп-лосс в %
    position_value: float     # Стоимость позиции
    position_pct: float       # Позиция в % от депозита
    max_loss_rub: float       # Максимальный убыток
    actual_risk_pct: float    # Реальный риск в %
    limited_by: str           # "risk" или "capital"


class PositionSizer:
    """
    Калькулятор размера позиции.
    
    Example:
        >>> sizer = PositionSizer(risk_config, trading_config)
        >>> pos = sizer.calculate(
        ...     price=4400,
        ...     atr=60,
        ...     lot_size=1
        ... )
        >>> print(f"Купить {pos.size_shares} акций, макс убыток {pos.max_loss_rub}₽")
    """

    def __init__(self, risk_config: RiskConfig, trading_config: TradingConfig):
        self.risk = risk_config
        self.trading = trading_config

    def calculate(
        self,
        price: float,
        atr: float,
        lot_size: int = 1,
        stop_loss_atr: float = None
    ) -> PositionSize:
        """
        Рассчитывает размер позиции.
        
        Args:
            price: Цена инструмента
            atr: ATR инструмента
            lot_size: Размер лота
            stop_loss_atr: Множитель ATR для стопа (по умолчанию из конфига)
        
        Returns:
            PositionSize с параметрами
        """
        stop_atr = stop_loss_atr or self.risk.stop_loss_atr
        deposit = self.trading.deposit_rub
        risk_pct = self.trading.risk_per_trade_pct
        max_pos_pct = self.risk.max_position_pct

        # Стоп в рублях на 1 акцию
        stop_rub = atr * stop_atr

        # Допустимый убыток
        max_loss = deposit * risk_pct

        # Размер по риску
        size_by_risk = max_loss / stop_rub if stop_rub > 0 else 0

        # Размер по капиталу (макс % депозита)
        max_position_value = deposit * max_pos_pct
        size_by_capital = max_position_value / price if price > 0 else 0

        # Берём минимум
        raw_size = min(size_by_risk, size_by_capital)

        # Округляем до лотов
        lots = int(raw_size // lot_size)
        final_size = lots * lot_size

        # Реальные параметры
        position_value = final_size * price
        position_pct = position_value / deposit * 100 if deposit > 0 else 0
        actual_loss = final_size * stop_rub
        actual_risk_pct = actual_loss / deposit * 100 if deposit > 0 else 0

        # Чем ограничен размер
        limited_by = "capital" if size_by_capital < size_by_risk else "risk"

        logger.debug(
            "position_calculated",
            price=price,
            atr=atr,
            size=final_size,
            limited_by=limited_by,
            risk_pct=round(actual_risk_pct, 2)
        )

        return PositionSize(
            size_shares=final_size,
            size_lots=lots,
            stop_rub=round(stop_rub, 2),
            stop_pct=round(stop_rub / price * 100, 2) if price > 0 else 0,
            position_value=round(position_value, 0),
            position_pct=round(position_pct, 2),
            max_loss_rub=round(actual_loss, 0),
            actual_risk_pct=round(actual_risk_pct, 2),
            limited_by=limited_by,
        )


def calculate_position_size(
    price: float,
    atr: float,
    stop_loss_atr: float,
    deposit: float,
    risk_pct: float,
    max_position_pct: float,
    lot_size: int = 1
) -> Dict[str, Any]:
    """
    Функциональный интерфейс для расчёта размера позиции.
    
    Args:
        price: Цена инструмента
        atr: ATR
        stop_loss_atr: Множитель ATR для стопа (например 0.3)
        deposit: Депозит в рублях
        risk_pct: Риск на сделку (0.01 = 1%)
        max_position_pct: Макс размер позиции (0.25 = 25%)
        lot_size: Размер лота
    
    Returns:
        Dict с параметрами позиции
    """
    # Стоп в рублях
    stop_rub = atr * stop_loss_atr

    # Допустимый убыток
    max_loss = deposit * risk_pct

    # Размер по риску
    size_by_risk = max_loss / stop_rub if stop_rub > 0 else 0

    # Размер по капиталу
    max_position_value = deposit * max_position_pct
    size_by_capital = max_position_value / price if price > 0 else 0

    # Берём минимум и округляем до лотов
    raw_size = min(size_by_risk, size_by_capital)
    lots = int(raw_size // lot_size)
    final_size = lots * lot_size

    # Реальный риск
    actual_risk = final_size * stop_rub
    actual_risk_pct = actual_risk / deposit * 100 if deposit > 0 else 0

    return {
        "size_shares": final_size,
        "size_lots": lots,
        "stop_rub": round(stop_rub, 2),
        "stop_pct": round(stop_rub / price * 100, 2) if price > 0 else 0,
        "position_value": round(final_size * price, 0),
        "position_pct": round(final_size * price / deposit * 100, 2) if deposit > 0 else 0,
        "max_loss_rub": round(actual_risk, 0),
        "actual_risk_pct": round(actual_risk_pct, 2),
        "limited_by": "capital" if size_by_capital < size_by_risk else "risk",
    }


def calculate_take_profit(
    entry_price: float,
    atr: float,
    take_profit_atr: float = 0.5,
    direction: str = "long"
) -> float:
    """
    Рассчитывает цену тейк-профита.
    
    Args:
        entry_price: Цена входа
        atr: ATR
        take_profit_atr: Множитель ATR (по умолчанию 0.5)
        direction: "long" или "short"
    
    Returns:
        Цена тейк-профита
    """
    tp_offset = atr * take_profit_atr
    
    if direction == "long":
        return round(entry_price + tp_offset, 2)
    else:
        return round(entry_price - tp_offset, 2)


def calculate_stop_loss(
    entry_price: float,
    atr: float,
    stop_loss_atr: float = 0.3,
    direction: str = "long"
) -> float:
    """
    Рассчитывает цену стоп-лосса.
    
    Args:
        entry_price: Цена входа
        atr: ATR
        stop_loss_atr: Множитель ATR (по умолчанию 0.3)
        direction: "long" или "short"
    
    Returns:
        Цена стоп-лосса
    """
    sl_offset = atr * stop_loss_atr
    
    if direction == "long":
        return round(entry_price - sl_offset, 2)
    else:
        return round(entry_price + sl_offset, 2)
