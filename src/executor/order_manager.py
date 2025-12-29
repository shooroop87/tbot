"""
Менеджер заявок.

TODO: Реализовать в следующей итерации.

Будет уметь:
- Выставлять лимитные заявки
- Выставлять рыночные заявки
- Ставить стоп-заявки (невидимые в стакане)
- Отменять заявки
- Проверять статус заявок
"""
from typing import Optional, Dict, Any
from enum import Enum

import structlog

logger = structlog.get_logger()


class OrderType(Enum):
    """Тип заявки."""
    LIMIT = "limit"
    MARKET = "market"
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"


class OrderStatus(Enum):
    """Статус заявки."""
    PENDING = "pending"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class OrderManager:
    """
    Менеджер заявок (заглушка).
    
    Пример использования (будет реализовано):
        >>> manager = OrderManager(client, config)
        >>> order = await manager.place_limit_order(
        ...     figi="BBG004730N88",
        ...     direction="buy",
        ...     quantity=10,
        ...     price=280.50
        ... )
        >>> status = await manager.get_order_status(order.order_id)
    """

    def __init__(self, tinkoff_client, config):
        self.client = tinkoff_client
        self.config = config
        self.logger = logger.bind(component="order_manager")

    async def place_limit_order(
        self,
        figi: str,
        direction: str,
        quantity: int,
        price: float,
    ) -> Optional[Dict[str, Any]]:
        """
        Выставляет лимитную заявку.
        
        TODO: Реализовать.
        """
        self.logger.warning("place_limit_order: NOT IMPLEMENTED")
        return None

    async def place_market_order(
        self,
        figi: str,
        direction: str,
        quantity: int,
    ) -> Optional[Dict[str, Any]]:
        """
        Выставляет рыночную заявку.
        
        TODO: Реализовать.
        """
        self.logger.warning("place_market_order: NOT IMPLEMENTED")
        return None

    async def place_stop_order(
        self,
        figi: str,
        direction: str,
        quantity: int,
        stop_price: float,
        order_type: str = "stop_loss",
    ) -> Optional[Dict[str, Any]]:
        """
        Выставляет стоп-заявку (невидимую в стакане).
        
        TODO: Реализовать.
        """
        self.logger.warning("place_stop_order: NOT IMPLEMENTED")
        return None

    async def cancel_order(self, order_id: str) -> bool:
        """
        Отменяет заявку.
        
        TODO: Реализовать.
        """
        self.logger.warning("cancel_order: NOT IMPLEMENTED")
        return False

    async def get_order_status(self, order_id: str) -> Optional[OrderStatus]:
        """
        Получает статус заявки.
        
        TODO: Реализовать.
        """
        self.logger.warning("get_order_status: NOT IMPLEMENTED")
        return None
