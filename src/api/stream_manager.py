"""
Менеджер стримов Tinkoff API с автоматическим переподключением.

TODO: Реализовать в следующей итерации для:
- Подписка на цены в реальном времени
- Отслеживание позиций
- Исполнение ордеров

Пока используем polling через TinkoffClient.
"""
import asyncio
from typing import Optional, Callable, Any

import structlog

logger = structlog.get_logger()


class StreamManager:
    """
    Менеджер потоковых данных с reconnect.
    
    Будет использоваться для:
    - MarketDataStream (цены, стаканы)
    - OrdersStream (статусы заявок)
    - PositionsStream (изменения позиций)
    """

    def __init__(self, token: str, max_reconnect_attempts: int = 5):
        self.token = token
        self.max_reconnect_attempts = max_reconnect_attempts
        self._running = False
        self._reconnect_count = 0

    async def start(self):
        """Запускает стрим."""
        self._running = True
        logger.info("stream_manager_started")
        # TODO: Реализовать подключение к стриму

    async def stop(self):
        """Останавливает стрим."""
        self._running = False
        logger.info("stream_manager_stopped")

    async def subscribe_candles(
        self,
        figi: str,
        callback: Callable[[Any], None]
    ):
        """
        Подписывается на свечи инструмента.
        
        Args:
            figi: FIGI инструмента
            callback: Функция обработки новых свечей
        """
        # TODO: Реализовать подписку
        logger.info("candles_subscription_stub", figi=figi)

    async def subscribe_orderbook(
        self,
        figi: str,
        depth: int,
        callback: Callable[[Any], None]
    ):
        """
        Подписывается на стакан инструмента.
        
        Args:
            figi: FIGI инструмента  
            depth: Глубина стакана
            callback: Функция обработки
        """
        # TODO: Реализовать подписку
        logger.info("orderbook_subscription_stub", figi=figi, depth=depth)

    async def _reconnect(self):
        """Переподключение при обрыве."""
        while self._reconnect_count < self.max_reconnect_attempts and self._running:
            self._reconnect_count += 1
            wait_time = min(2 ** self._reconnect_count, 60)  # Exponential backoff
            
            logger.warning(
                "stream_reconnecting",
                attempt=self._reconnect_count,
                wait_seconds=wait_time
            )
            
            await asyncio.sleep(wait_time)
            
            try:
                await self.start()
                self._reconnect_count = 0
                return
            except Exception as e:
                logger.error("stream_reconnect_failed", error=str(e))

        logger.error("stream_max_reconnects_reached")
