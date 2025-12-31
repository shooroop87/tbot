"""
Telegram Bot с обработкой inline кнопок.

Запускает polling для получения callback'ов при нажатии кнопок.
"""
import asyncio
from typing import Optional, Dict, Any

import aiohttp
import structlog

from config import Config
from api.tinkoff_client import TinkoffClient
from api.telegram_notifier import TelegramNotifier
from executor.order_manager import OrderManager

logger = structlog.get_logger()

# Глобальный кэш данных по акциям (заполняется при ежедневном расчёте)
SHARES_CACHE: Dict[str, Dict[str, Any]] = {}


def update_shares_cache(shares: list):
    """
    Обновляет кэш акций.
    
    Вызывается из jobs.py после расчёта индикаторов.
    """
    global SHARES_CACHE
    SHARES_CACHE.clear()
    for share in shares:
        SHARES_CACHE[share["ticker"]] = share
    logger.info("shares_cache_updated", count=len(SHARES_CACHE))


def get_share_from_cache(ticker: str) -> Optional[Dict[str, Any]]:
    """Получает данные акции из кэша."""
    return SHARES_CACHE.get(ticker)


class TelegramBot:
    """
    Telegram бот с обработкой callback.
    
    Пример использования:
        >>> bot = TelegramBot(config)
        >>> await bot.start_polling()
    """

    def __init__(self, config: Config):
        self.config = config
        self.bot_token = config.telegram.bot_token
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}"
        self.notifier = TelegramNotifier(config.telegram)
        self._running = False
        self._offset = 0

    async def start_polling(self):
        """Запускает polling для получения updates."""
        self._running = True
        logger.info("telegram_bot_polling_started")
        
        while self._running:
            try:
                updates = await self._get_updates()
                for update in updates:
                    await self._process_update(update)
                    self._offset = update["update_id"] + 1
            except asyncio.CancelledError:
                logger.info("polling_cancelled")
                break
            except Exception as e:
                logger.error("polling_error", error=str(e))
                await asyncio.sleep(5)
            
            await asyncio.sleep(1)

    async def stop(self):
        """Останавливает polling."""
        self._running = False
        logger.info("telegram_bot_stopped")

    async def _get_updates(self) -> list:
        """Получает updates от Telegram."""
        url = f"{self.base_url}/getUpdates"
        params = {
            "offset": self._offset,
            "timeout": 30,
            "allowed_updates": ["callback_query"]
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=35) as response:
                    if response.status == 200:
                        data = await response.json()
                        return data.get("result", [])
        except asyncio.TimeoutError:
            pass  # Нормально для long polling
        except Exception as e:
            logger.error("get_updates_error", error=str(e))
        
        return []

    async def _process_update(self, update: Dict[str, Any]):
        """Обрабатывает update."""
        if "callback_query" in update:
            await self._handle_callback(update["callback_query"])

    async def _handle_callback(self, callback: Dict[str, Any]):
        """Обрабатывает нажатие inline кнопки."""
        callback_id = callback["id"]
        data = callback.get("data", "")
        chat_id = callback["message"]["chat"]["id"]
        
        logger.info("callback_received", data=data, chat_id=chat_id)
        
        # Парсим callback_data: "buy:TICKER"
        if data.startswith("buy:"):
            ticker = data.split(":")[1]
            await self._place_order(ticker, chat_id, callback_id)
        else:
            await self._answer_callback(callback_id, "❓ Неизвестная команда")

    async def _place_order(self, ticker: str, chat_id: int, callback_id: str):
        """Выставляет заявку по тикеру."""
        # Получаем данные из кэша
        share_data = get_share_from_cache(ticker)
        
        if not share_data:
            await self._answer_callback(callback_id, f"❌ Данные по {ticker} не найдены")
            await self.notifier.send_message(
                f"❌ Данные по {ticker} устарели или не найдены.\n"
                f"Запустите расчёт заново: <code>python main.py --now</code>"
            )
            return
        
        # Показываем что обрабатываем
        await self._answer_callback(callback_id, f"⏳ Выставляю заявку {ticker}...")
        
        try:
            async with TinkoffClient(self.config.tinkoff) as client:
                order_manager = OrderManager(client, self.config)
                
                # Выставляем отложенную заявку тейк-профит на покупку
                result = await order_manager.place_take_profit_buy(
                    figi=share_data["figi"],
                    quantity=share_data["position_size"],
                    activation_price=share_data["entry_price"],
                )
                
                if result.get("success"):
                    if result.get("dry_run"):
                        await self.notifier.send_message(
                            f"🔸 <b>DRY RUN: {ticker}</b>\n\n"
                            f"Заявка НЕ выставлена (режим dry_run=True)\n\n"
                            f"Параметры:\n"
                            f"📥 Цена входа: {share_data['entry_price']} ₽\n"
                            f"📦 Количество: {share_data['position_size']} шт\n"
                            f"🎯 Тейк: {share_data['take_price']} ₽\n"
                            f"🛑 Стоп: {share_data['stop_price']} ₽"
                        )
                    else:
                        await self.notifier.send_order_confirmation(
                            ticker=ticker,
                            order_type="Отложенная Тейк-профит (покупка)",
                            price=share_data["entry_price"],
                            quantity=share_data["position_size"],
                            order_id=result.get("stop_order_id", "N/A")
                        )
                else:
                    await self.notifier.send_order_error(
                        ticker=ticker,
                        error=result.get("error", "Неизвестная ошибка")
                    )
                    
        except Exception as e:
            logger.exception("order_error", ticker=ticker)
            await self.notifier.send_order_error(ticker=ticker, error=str(e))

    async def _answer_callback(self, callback_id: str, text: str, show_alert: bool = False):
        """Отвечает на callback query (всплывающее уведомление)."""
        url = f"{self.base_url}/answerCallbackQuery"
        payload = {
            "callback_query_id": callback_id,
            "text": text,
            "show_alert": show_alert
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                await session.post(url, json=payload, timeout=10)
        except Exception as e:
            logger.error("answer_callback_error", error=str(e))