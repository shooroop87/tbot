"""
Трекер позиций.

TODO: Реализовать в следующей итерации.

Будет уметь:
- Отслеживать открытые позиции
- Следить за ценой и P&L
- Автоматически закрывать по стопу/тейку
- Trailing stop
"""
from typing import Optional, Dict, Any, List
from dataclasses import dataclass
from datetime import datetime

import structlog

logger = structlog.get_logger()


@dataclass
class Position:
    """Открытая позиция."""
    figi: str
    ticker: str
    direction: str  # long / short
    quantity: int
    entry_price: float
    entry_time: datetime
    
    # Цели
    stop_price: Optional[float] = None
    target_price: Optional[float] = None
    
    # Текущее состояние
    current_price: Optional[float] = None
    unrealized_pnl: Optional[float] = None


class PositionTracker:
    """
    Трекер позиций (заглушка).
    
    Пример использования (будет реализовано):
        >>> tracker = PositionTracker(client, order_manager, config)
        >>> await tracker.start()  # Начать отслеживание
        >>> positions = await tracker.get_open_positions()
        >>> await tracker.check_stop_loss(position)
    """

    def __init__(self, tinkoff_client, order_manager, config):
        self.client = tinkoff_client
        self.order_manager = order_manager
        self.config = config
        self.positions: Dict[str, Position] = {}
        self._running = False
        self.logger = logger.bind(component="position_tracker")

    async def start(self):
        """
        Запускает отслеживание позиций.
        
        TODO: Реализовать.
        """
        self.logger.warning("start: NOT IMPLEMENTED")
        self._running = True

    async def stop(self):
        """
        Останавливает отслеживание.
        
        TODO: Реализовать.
        """
        self.logger.warning("stop: NOT IMPLEMENTED")
        self._running = False

    async def get_open_positions(self) -> List[Position]:
        """
        Получает открытые позиции из API.
        
        TODO: Реализовать.
        """
        self.logger.warning("get_open_positions: NOT IMPLEMENTED")
        return []

    async def add_position(self, position: Position):
        """
        Добавляет позицию для отслеживания.
        
        TODO: Реализовать.
        """
        self.logger.warning("add_position: NOT IMPLEMENTED")
        self.positions[position.figi] = position

    async def update_prices(self):
        """
        Обновляет текущие цены позиций.
        
        TODO: Реализовать.
        """
        self.logger.warning("update_prices: NOT IMPLEMENTED")

    async def check_stop_loss(self, position: Position) -> bool:
        """
        Проверяет достижение стоп-лосса.
        
        TODO: Реализовать.
        """
        self.logger.warning("check_stop_loss: NOT IMPLEMENTED")
        return False

    async def check_take_profit(self, position: Position) -> bool:
        """
        Проверяет достижение тейк-профита.
        
        TODO: Реализовать.
        """
        self.logger.warning("check_take_profit: NOT IMPLEMENTED")
        return False

    async def close_position(self, figi: str, reason: str = "manual"):
        """
        Закрывает позицию.
        
        TODO: Реализовать.
        """
        self.logger.warning("close_position: NOT IMPLEMENTED")
