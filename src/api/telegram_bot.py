"""
Telegram Bot с обработкой inline кнопок.

Исправления:
- Мгновенный ответ на callback (answerCallbackQuery)
- Тяжёлая работа в asyncio.create_task()
- Единая aiohttp сессия
- Защита от двойного нажатия
"""
import asyncio
from typing import Optional, Dict, Any, Set

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
    """Обновляет кэш акций."""
    global SHARES_CACHE
    SHARES_CACHE.clear()
    for share in shares:
        SHARES_CACHE[share["ticker"]] = share
    logger.info("shares_cache_updated", count=len(SHARES_CACHE), tickers=list(SHARES_CACHE.keys()))


def get_share_from_cache(ticker: str) -> Optional[Dict[str, Any]]:
    """Получает данные акции из кэша."""
    return SHARES_CACHE.get(ticker)


class TelegramBot:
    """Telegram бот с обработкой callback."""

    def __init__(self, config: Config):
        self.config = config
        self.bot_token = config.telegram.bot_token
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}"
        self.notifier = TelegramNotifier(config.telegram)
        self._running = False
        self._offset = 0
        self._session: Optional[aiohttp.ClientSession] = None
        self._processing_tickers: Set[str] = set()  # Защита от двойного нажатия

    async def start_polling(self):
        """Запускает polling для получения updates."""
        self._running = True
        
        # Создаём единую сессию
        timeout = aiohttp.ClientTimeout(total=15)
        self._session = aiohttp.ClientSession(timeout=timeout)
        
        # Загружаем сохранённый offset или flush старых updates
        await self._init_offset()
        
        logger.info("telegram_bot_polling_started", offset=self._offset)
        
        poll_count = 0
        while self._running:
            poll_count += 1
            
            if poll_count == 1 or poll_count % 30 == 0:
                logger.info("polling_active", iteration=poll_count, offset=self._offset)
            
            try:
                updates = await self._get_updates()
                
                if updates:
                    logger.info("updates_received", count=len(updates), 
                              first_id=updates[0]["update_id"], 
                              last_id=updates[-1]["update_id"])
                    for update in updates:
                        await self._process_update(update)
                        self._offset = update["update_id"] + 1
                        self._save_offset()  # Сохраняем после каждого update
                        
            except asyncio.CancelledError:
                logger.info("polling_cancelled")
                break
            except Exception as e:
                logger.error("polling_error", error=str(e), error_type=type(e).__name__)
                await asyncio.sleep(3)
            
            await asyncio.sleep(0.3)
        
        logger.info("polling_loop_ended")

    async def _init_offset(self):
        """Инициализирует offset: загружает из файла или flush старых updates."""
        offset_file = "/tmp/tbot_offset.txt"
        
        # Пробуем загрузить из файла
        try:
            with open(offset_file, "r") as f:
                saved_offset = int(f.read().strip())
                if saved_offset > 0:
                    self._offset = saved_offset
                    logger.info("offset_loaded_from_file", offset=self._offset)
                    return
        except (FileNotFoundError, ValueError):
            pass
        
        # Файла нет — flush все старые updates
        logger.info("flushing_old_updates")
        url = f"{self.base_url}/getUpdates"
        params = {"offset": 0, "timeout": 0, "limit": 100}
        
        try:
            async with self._session.get(url, params=params) as response:
                if response.status == 200:
                    data = await response.json()
                    results = data.get("result", [])
                    if results:
                        # Ставим offset на последний+1
                        last_id = results[-1]["update_id"]
                        self._offset = last_id + 1
                        self._save_offset()
                        logger.info("offset_set_after_flush", 
                                   flushed_count=len(results), 
                                   new_offset=self._offset)
                    else:
                        logger.info("no_old_updates_to_flush")
        except Exception as e:
            logger.error("flush_error", error=str(e))

    def _save_offset(self):
        """Сохраняет offset в файл."""
        offset_file = "/tmp/tbot_offset.txt"
        try:
            with open(offset_file, "w") as f:
                f.write(str(self._offset))
        except Exception as e:
            logger.error("save_offset_error", error=str(e))

    async def stop(self):
        """Останавливает polling и закрывает сессию."""
        self._running = False
        if self._session:
            await self._session.close()
            self._session = None
        logger.info("telegram_bot_stopped")

    async def _get_updates(self) -> list:
        """Получает updates от Telegram."""
        if not self._session:
            return []
        
        url = f"{self.base_url}/getUpdates"
        params = {
            "offset": self._offset,
            "timeout": 10,
            "allowed_updates": ["callback_query", "message"]
        }
        
        try:
            async with self._session.get(url, params=params) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get("result", [])
                else:
                    error_text = await response.text()
                    logger.error("get_updates_failed", status=response.status, error=error_text[:200])
        except asyncio.TimeoutError:
            pass  # Нормально для long polling
        except Exception as e:
            logger.error("get_updates_error", error=str(e))
        
        return []

    async def _process_update(self, update: Dict[str, Any]):
        """Обрабатывает update."""
        logger.info("processing_update", update_id=update.get("update_id"), keys=list(update.keys()))
        
        if "callback_query" in update:
            await self._handle_callback(update["callback_query"])
        elif "message" in update:
            msg = update["message"]
            text = msg.get("text", "")
            chat_id = msg["chat"]["id"]
            logger.info("got_message", text=text[:50], chat_id=chat_id)
            
            if text == "/test":
                await self.notifier.send_message("✅ Бот работает! Нажми кнопку на карточке акции.")
            elif text == "/status":
                cache_info = f"Кэш: {len(SHARES_CACHE)} акций"
                tickers = ", ".join(list(SHARES_CACHE.keys())[:10])
                await self.notifier.send_message(f"📊 {cache_info}\n📌 {tickers}...")
            elif text == "/button":
                # Тестовая кнопка
                await self._send_test_button(chat_id)
    
    async def _send_test_button(self, chat_id: int):
        """Отправляет тестовое сообщение с кнопкой."""
        url = f"{self.base_url}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": "🧪 Тестовая кнопка. Нажми её!",
            "reply_markup": {
                "inline_keyboard": [[
                    {"text": "🔘 Нажми меня", "callback_data": "test:ping"}
                ]]
            }
        }
        
        try:
            async with self._session.post(url, json=payload) as response:
                if response.status == 200:
                    logger.info("test_button_sent")
                else:
                    error = await response.text()
                    logger.error("test_button_failed", error=error[:100])
        except Exception as e:
            logger.error("test_button_error", error=str(e))

    async def _handle_callback(self, callback: Dict[str, Any]):
        """Обрабатывает нажатие inline кнопки."""
        callback_id = callback["id"]
        data = callback.get("data", "")
        chat_id = callback["message"]["chat"]["id"]
        
        logger.info("callback_received", data=data, chat_id=chat_id, callback_id=callback_id)
        
        if data.startswith("buy:"):
            ticker = data.split(":")[1]
            
            # Защита от двойного нажатия
            if ticker in self._processing_tickers:
                await self._answer_callback(callback_id, f"⏳ {ticker} уже обрабатывается...")
                return
            
            # 1) СРАЗУ отвечаем Telegram (убираем "часики")
            await self._answer_callback(callback_id, f"✅ Принял {ticker}. Обрабатываю...")
            logger.info("callback_answered", ticker=ticker)
            
            # 2) Запускаем тяжёлую работу в фоне
            asyncio.create_task(self._place_order_background(ticker, chat_id))
            return
        
        elif data == "test:ping":
            # Тестовый callback
            logger.info("test_callback_received!")
            await self._answer_callback(callback_id, "🎉 Callback работает!")
            await self.notifier.send_message("✅ Кнопка нажата! Callback получен.")
            return
        
        await self._answer_callback(callback_id, "❓ Неизвестная команда")

    async def _place_order_background(self, ticker: str, chat_id: int):
        """Выставляет заявку в фоновом режиме."""
        logger.info("place_order_background_started", ticker=ticker)
        
        # Добавляем в "обрабатываемые"
        self._processing_tickers.add(ticker)
        
        try:
            await self._place_order(ticker, chat_id)
        except Exception as e:
            logger.exception("place_order_background_error", ticker=ticker)
            await self.notifier.send_message(f"❌ Ошибка {ticker}: {str(e)}")
        finally:
            # Убираем из "обрабатываемых"
            self._processing_tickers.discard(ticker)
            logger.info("place_order_background_finished", ticker=ticker)

    async def _place_order(self, ticker: str, chat_id: int):
        """Выставляет заявку по тикеру."""
        logger.info("place_order_started", ticker=ticker)
        
        # Получаем данные из кэша
        share_data = get_share_from_cache(ticker)
        
        if not share_data:
            logger.warning("share_not_in_cache", ticker=ticker, available=list(SHARES_CACHE.keys()))
            await self.notifier.send_message(
                f"❌ Данные по {ticker} не найдены в кэше.\n"
                f"Запустите расчёт: <code>python main.py --now</code>"
            )
            return
        
        logger.info("share_data_found", ticker=ticker, figi=share_data.get("figi"), 
                   entry_price=share_data.get("entry_price"), position_size=share_data.get("position_size"))
        
        # Конвертируем position_size в лоты
        lot_size = share_data.get("lot_size", 1)
        quantity_lots = share_data["position_size"] // lot_size if lot_size > 0 else share_data["position_size"]
        
        if quantity_lots <= 0:
            logger.error("invalid_quantity", position_size=share_data["position_size"], lot_size=lot_size)
            await self.notifier.send_message(
                f"❌ <b>Ошибка: {ticker}</b>\n\n"
                f"Размер позиции ({share_data['position_size']} шт) меньше 1 лота ({lot_size} шт)"
            )
            return
        
        try:
            async with TinkoffClient(self.config.tinkoff) as client:
                order_manager = OrderManager(client, self.config)
                
                logger.info("placing_take_profit_buy", 
                           figi=share_data["figi"],
                           quantity_lots=quantity_lots,
                           price=share_data["entry_price"],
                           dry_run=self.config.dry_run)
                
                # Выставляем заявку
                result = await order_manager.place_take_profit_buy(
                    figi=share_data["figi"],
                    quantity=quantity_lots,
                    price=share_data["entry_price"],
                )
                
                logger.info("order_result", result=result)
                
                if result.get("success"):
                    if result.get("dry_run"):
                        msg = (
                            f"🔸 <b>DRY RUN: {ticker}</b>\n\n"
                            f"Заявка НЕ выставлена (dry_run=True)\n\n"
                            f"📋 Тейк-профит покупка\n"
                            f"📥 Цена: {share_data['entry_price']} ₽\n"
                            f"📦 Кол-во: {quantity_lots} лот ({share_data['position_size']} шт)\n"
                            f"🎯 Тейк: {share_data.get('take_price', 'N/A')} ₽\n"
                            f"🛑 Стоп: {share_data.get('stop_price', 'N/A')} ₽"
                        )
                        logger.info("dry_run_order", ticker=ticker)
                    else:
                        order_id = result.get("order_id", "N/A")
                        msg = (
                            f"✅ <b>Заявка: {ticker}</b>\n\n"
                            f"📋 Тейк-профит покупка\n"
                            f"📥 Цена: {share_data['entry_price']} ₽\n"
                            f"📦 Кол-во: {quantity_lots} лот ({share_data['position_size']} шт)\n"
                            f"🆔 ID: <code>{order_id}</code>\n\n"
                            f"⏳ Сработает при цене {share_data['entry_price']} ₽"
                        )
                        logger.info("order_placed", ticker=ticker, order_id=order_id)
                    
                    await self.notifier.send_message(msg)
                else:
                    error_msg = result.get("error", "Неизвестная ошибка")
                    await self.notifier.send_message(
                        f"❌ <b>Ошибка: {ticker}</b>\n\n⚠️ {error_msg}"
                    )
                    logger.error("order_failed", ticker=ticker, error=error_msg)
                    
        except Exception as e:
            logger.exception("order_exception", ticker=ticker)
            await self.notifier.send_message(
                f"❌ <b>Исключение: {ticker}</b>\n\n⚠️ {str(e)}"
            )

    async def _answer_callback(self, callback_id: str, text: str, show_alert: bool = False):
        """Отвечает на callback query (убирает 'часики')."""
        if not self._session:
            logger.error("answer_callback_no_session")
            return
        
        url = f"{self.base_url}/answerCallbackQuery"
        payload = {
            "callback_query_id": callback_id,
            "text": text,
            "show_alert": show_alert
        }
        
        try:
            async with self._session.post(url, json=payload) as response:
                if response.status == 200:
                    logger.debug("callback_answered_ok")
                else:
                    error = await response.text()
                    logger.error("answer_callback_failed", status=response.status, error=error[:100])
        except Exception as e:
            logger.error("answer_callback_error", error=str(e))