"""
Telegram Bot на aiogram.

Команды:
- /help - справка
- /list - список тикеров
- /buy TICKER - выставить заявку
- /status - статус кэша
- /orders - активные заявки
"""
import asyncio
from typing import Dict, Any, Optional, TYPE_CHECKING

import structlog
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command

from config import Config
from api.tinkoff_client import TinkoffClient
from executor.order_manager import OrderManager

if TYPE_CHECKING:
    from executor.position_watcher import PositionWatcher

logger = structlog.get_logger()

# Глобальный кэш данных по акциям
SHARES_CACHE: Dict[str, Dict[str, Any]] = {}

# Глобальная ссылка на PositionWatcher (устанавливается из main.py)
_position_watcher: Optional["PositionWatcher"] = None

# Глобальные ссылки для управления (устанавливаются из main.py)
_scheduler = None
_watcher_task = None
_bot_active = True  # Флаг активности бота


def set_position_watcher(watcher: "PositionWatcher"):
    """Устанавливает глобальный PositionWatcher."""
    global _position_watcher
    _position_watcher = watcher


def get_position_watcher() -> Optional["PositionWatcher"]:
    """Возвращает глобальный PositionWatcher."""
    return _position_watcher


def set_scheduler(scheduler):
    """Устанавливает глобальный scheduler."""
    global _scheduler
    _scheduler = scheduler


def set_watcher_task(task):
    """Устанавливает глобальную задачу watcher."""
    global _watcher_task
    _watcher_task = task


def is_bot_active() -> bool:
    """Проверяет активен ли бот."""
    return _bot_active


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


def escape_html(text: str) -> str:
    """Экранирует HTML-символы."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class TelegramBotAiogram:
    """Telegram бот на aiogram."""

    def __init__(self, config: Config):
        self.config = config
        self.bot = Bot(token=config.telegram.bot_token)
        self.dp = Dispatcher()
        self._processing_tickers = set()
        
        # Регистрируем handlers
        self._register_handlers()

    def _register_handlers(self):
        """Регистрирует обработчики."""
        
        @self.dp.message(Command("start", "help"))
        async def cmd_start(message: Message):
            await message.answer(
                "🤖 <b>Trading Bot</b>\n\n"
                "Команды:\n"
                "/list - список тикеров с ценами входа\n"
                "/buy SBER - выставить заявку\n"
                "/orders - активные отслеживаемые заявки\n"
                "/status - статус бота\n"
                "/stop_bot - остановить бота\n"
                "/start_bot - запустить бота\n"
                "/help - эта справка",
                parse_mode="HTML"
            )

        @self.dp.message(Command("stop_bot"))
        async def cmd_stop_bot(message: Message):
            """Останавливает бота (watcher, scheduler, очищает кэш)."""
            global _bot_active, _watcher_task
            
            if not _bot_active:
                await message.answer("⚪ Бот уже остановлен")
                return
            
            logger.info("stop_bot_command")
            
            # Останавливаем watcher
            watcher = get_position_watcher()
            tracked_count = 0
            if watcher:
                await watcher.stop()
                tracked_count = watcher.tracked_count
            
            # Останавливаем scheduler
            if _scheduler:
                _scheduler.pause()
            
            # Отменяем задачу watcher
            if _watcher_task and not _watcher_task.done():
                _watcher_task.cancel()
            
            # Очищаем кэш
            cache_count = len(SHARES_CACHE)
            SHARES_CACHE.clear()
            
            _bot_active = False
            
            await message.answer(
                f"🔴 <b>Бот остановлен</b>\n\n"
                f"📋 Очищено из кэша: {cache_count} тикеров\n"
                f"🔍 Отслеживалось: {tracked_count} заявок\n\n"
                f"⚠️ Новые заявки не будут приниматься\n"
                f"⚠️ Активные заявки НЕ отменены на бирже!\n\n"
                f"Для запуска: /start_bot",
                parse_mode="HTML"
            )

        @self.dp.message(Command("start_bot"))
        async def cmd_start_bot(message: Message):
            """Запускает бота."""
            global _bot_active, _watcher_task
            
            if _bot_active:
                await message.answer("🟢 Бот уже работает")
                return
            
            logger.info("start_bot_command")
            
            # Запускаем watcher
            watcher = get_position_watcher()
            if watcher:
                _watcher_task = asyncio.create_task(watcher.start())
            
            # Запускаем scheduler
            if _scheduler:
                _scheduler.resume()
            
            _bot_active = True
            
            await message.answer(
                f"🟢 <b>Бот запущен</b>\n\n"
                f"📋 Кэш пуст — запустите расчёт:\n"
                f"<code>python main.py --now</code>\n"
                f"или дождитесь 06:30 МСК\n\n"
                f"🔍 Watcher активен",
                parse_mode="HTML"
            )

        @self.dp.message(Command("test"))
        async def cmd_test(message: Message):
            logger.info("cmd_test_received", chat_id=message.chat.id)
            await message.answer("✅ Бот работает!")

        @self.dp.message(Command("status"))
        async def cmd_status(message: Message):
            """Показывает полный статус бота."""
            # Статус бота
            bot_status = "🟢 Активен" if _bot_active else "🔴 Остановлен"
            
            # Кэш
            cache_count = len(SHARES_CACHE)
            tickers = ", ".join(list(SHARES_CACHE.keys())[:10])
            if len(SHARES_CACHE) > 10:
                tickers += "..."
            
            # Watcher
            watcher = get_position_watcher()
            if watcher:
                watcher_status = "🟢 Работает" if watcher.is_running else "🔴 Остановлен"
                tracked_count = watcher.tracked_count
            else:
                watcher_status = "⚪ Не инициализирован"
                tracked_count = 0
            
            # Scheduler
            scheduler_status = "🟢 Работает" if _scheduler and _scheduler.running else "🔴 Остановлен"
            
            await message.answer(
                f"📊 <b>Статус бота</b>\n\n"
                f"🤖 Бот: {bot_status}\n"
                f"🔍 Watcher: {watcher_status}\n"
                f"⏰ Scheduler: {scheduler_status}\n\n"
                f"📋 Кэш: {cache_count} тикеров\n"
                f"📌 {tickers}\n\n"
                f"🎯 Отслеживается: {tracked_count} заявок",
                parse_mode="HTML"
            )

        @self.dp.message(Command("orders"))
        async def cmd_orders(message: Message):
            """Показывает активные отслеживаемые заявки."""
            watcher = get_position_watcher()
            if not watcher:
                await message.answer("❌ Watcher не запущен")
                return
            
            orders = watcher.get_tracked_orders()
            if not orders:
                await message.answer("📋 Нет активных отслеживаемых заявок")
                return
            
            lines = ["📋 <b>Отслеживаемые заявки:</b>", ""]
            for order_id, order in orders.items():
                emoji = {"entry_buy": "📥", "stop_loss": "🛑", "take_profit": "🎯"}.get(order.order_type.value, "⚪")
                lines.append(
                    f"{emoji} {order.ticker} — {order.order_type.value}\n"
                    f"   Вход: {order.entry_price:,.2f} | SL: {order.stop_price:,.2f} | TP: {order.target_price:,.2f}"
                )
            
            await message.answer("\n".join(lines), parse_mode="HTML")

        @self.dp.message(Command("list"))
        async def cmd_list(message: Message):
            """Список тикеров с ценами входа."""
            if not SHARES_CACHE:
                await message.answer(
                    "❌ Кэш пуст. Дождитесь расчёта в 06:30\n"
                    "или запустите: <code>python main.py --now</code>",
                    parse_mode="HTML"
                )
                return
            
            lines = ["📋 <b>Доступные тикеры:</b>", ""]
            for ticker, data in SHARES_CACHE.items():
                entry = data.get("entry_price", 0)
                signal = "🟢" if data.get("signal") == "BUY" else "⚪"
                lines.append(f"{signal} <code>/buy {ticker}</code> — вход {entry:,.2f}₽")
            
            await message.answer("\n".join(lines), parse_mode="HTML")

        @self.dp.message(Command("buy"))
        async def cmd_buy(message: Message):
            """Команда /buy TICKER."""
            parts = message.text.split()
            if len(parts) < 2:
                await message.answer("❌ Использование: /buy SBER")
                return
            
            ticker_input = parts[1].upper()
            
            # Ищем в кэше без учёта регистра
            ticker = None
            for key in SHARES_CACHE.keys():
                if key.upper() == ticker_input:
                    ticker = key
                    break
            
            if not ticker:
                available = ", ".join(SHARES_CACHE.keys()) if SHARES_CACHE else "пусто"
                await message.answer(
                    f"❌ Тикер {ticker_input} не найден.\n"
                    f"Доступные: {available}\n"
                    f"Используй /list"
                )
                return
            
            # Защита от двойного нажатия
            if ticker in self._processing_tickers:
                await message.answer(f"⏳ {ticker} уже обрабатывается...")
                return
            
            logger.info("buy_command_received", ticker=ticker)
            await message.answer(f"⏳ Обрабатываю заявку {ticker}...")
            
            # Запускаем в фоне
            asyncio.create_task(self._place_order(ticker, message))

        @self.dp.callback_query()
        async def callback_any(callback: CallbackQuery):
            """Любой callback — игнорируем старые кнопки."""
            logger.debug("callback_ignored", data=callback.data)
            await callback.answer("Используйте команду /buy TICKER")

    async def _place_order(self, ticker: str, message: Message):
        """Выставляет заявку и добавляет в отслеживание."""
        # Проверка активности бота
        if not _bot_active:
            await message.answer(
                "🔴 Бот остановлен. Заявки не принимаются.\n"
                "Запустите: /start_bot"
            )
            return
        
        logger.info("place_order_started", ticker=ticker)
        self._processing_tickers.add(ticker)
        
        try:
            share_data = get_share_from_cache(ticker)
            
            if not share_data:
                logger.warning("share_not_in_cache", ticker=ticker)
                await message.answer(
                    f"❌ Данные по {ticker} не найдены в кэше.\n"
                    f"Запустите расчёт: <code>python main.py --now</code>",
                    parse_mode="HTML"
                )
                return
            
            logger.info("share_data_found", ticker=ticker, 
                       entry_price=share_data.get("entry_price"),
                       position_size=share_data.get("position_size"))
            
            # Количество лотов
            lot_size = share_data.get("lot_size", 1)
            quantity_lots = share_data["position_size"] // lot_size
            
            if quantity_lots <= 0:
                await message.answer(f"❌ Размер позиции {ticker} меньше 1 лота")
                return
            
            async with TinkoffClient(self.config.tinkoff) as client:
                order_manager = OrderManager(client, self.config)
                
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
                            f"📋 Тейк-профит покупка\n"
                            f"📥 Цена: {share_data['entry_price']:,.2f} ₽\n"
                            f"📦 Кол-во: {quantity_lots} лот ({share_data['position_size']} шт)"
                        )
                    else:
                        order_id = result.get("order_id", "N/A")
                        
                        # Добавляем в отслеживание
                        watcher = get_position_watcher()
                        if watcher:
                            from executor.position_watcher import OrderType
                            watcher.track_order(
                                order_id=order_id,
                                ticker=ticker,
                                figi=share_data["figi"],
                                order_type=OrderType.ENTRY_BUY,
                                quantity=quantity_lots,
                                entry_price=share_data["entry_price"],
                                stop_price=share_data["stop_price"],
                                target_price=share_data["take_price"],
                                stop_offset=share_data.get("stop_offset", 0),
                                take_offset=share_data.get("take_offset", 0),
                                lot_size=lot_size,
                                atr=share_data.get("atr", 0),
                            )
                            logger.info("order_added_to_watcher", order_id=order_id, ticker=ticker)
                        
                        msg = (
                            f"✅ <b>Заявка: {ticker}</b>\n\n"
                            f"📥 Цена входа: {share_data['entry_price']:,.2f} ₽\n"
                            f"🛑 Стоп-лосс: {share_data['stop_price']:,.2f} ₽\n"
                            f"🎯 Тейк-профит: {share_data['take_price']:,.2f} ₽\n"
                            f"📦 Кол-во: {quantity_lots} лот\n"
                            f"🆔 ID: <code>{order_id}</code>\n\n"
                            f"⏰ Действует до конца дня\n"
                            f"🔍 Отслеживание активно"
                        )
                    await message.answer(msg, parse_mode="HTML")
                else:
                    error = escape_html(str(result.get("error", "Неизвестная ошибка")))
                    await message.answer(f"❌ Ошибка: {error}", parse_mode="HTML")
                    
        except Exception as e:
            logger.exception("place_order_error", ticker=ticker)
            error = escape_html(str(e))
            await message.answer(f"❌ Исключение: {error}", parse_mode="HTML")
        finally:
            self._processing_tickers.discard(ticker)

    async def start(self):
        """Запускает polling."""
        logger.info("aiogram_bot_starting")
        await self.dp.start_polling(self.bot)

    async def stop(self):
        """Останавливает бота."""
        logger.info("aiogram_bot_stopping")
        await self.bot.session.close()


# Для обратной совместимости
TelegramBot = TelegramBotAiogram