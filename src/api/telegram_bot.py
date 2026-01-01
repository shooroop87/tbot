"""
Telegram Bot на aiogram.

Преимущества:
- Автоматически управляет offset
- Надёжный polling
- Простые handlers для callback
"""
import asyncio
from typing import Dict, Any, Optional

import structlog
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command

from config import Config
from api.tinkoff_client import TinkoffClient
from executor.order_manager import OrderManager

logger = structlog.get_logger()

# Глобальный кэш данных по акциям
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
                "/status - статус кэша\n"
                "/test - проверить работу\n"
                "/help - эта справка",
                parse_mode="HTML"
            )

        @self.dp.message(Command("test"))
        async def cmd_test(message: Message):
            logger.info("cmd_test_received", chat_id=message.chat.id)
            await message.answer("✅ Бот работает!")

        @self.dp.message(Command("status"))
        async def cmd_status(message: Message):
            cache_info = f"Кэш: {len(SHARES_CACHE)} акций"
            tickers = ", ".join(list(SHARES_CACHE.keys())[:15])
            await message.answer(f"📊 {cache_info}\n📌 {tickers}...")

        @self.dp.message(Command("list"))
        async def cmd_list(message: Message):
            """Список тикеров с ценами входа."""
            if not SHARES_CACHE:
                await message.answer(
                    "❌ Кэш пуст. Дождитесь расчёта в 06:30\n"
                    "или запустите: <code>--now</code>",
                    parse_mode="HTML"
                )
                return
            
            lines = ["📋 <b>Доступные тикеры:</b>", ""]
            for ticker, data in SHARES_CACHE.items():
                entry = data.get("entry_price", 0)
                signal = "🟢" if data.get("signal") == "BUY" else "⚪"
                lines.append(f"{signal} <code>/buy {ticker}</code> — вход {entry:.2f}₽")
            
            await message.answer("\n".join(lines), parse_mode="HTML")

        @self.dp.message(Command("buy"))
        async def cmd_buy(message: Message):
            """Команда /buy TICKER."""
            parts = message.text.split()
            if len(parts) < 2:
                await message.answer("❌ Использование: /buy SBER")
                return
            
            ticker_input = parts[1]
            
            # Ищем в кэше без учёта регистра
            ticker = None
            for key in SHARES_CACHE.keys():
                if key.upper() == ticker_input.upper():
                    ticker = key
                    break
            
            if not ticker:
                available = ", ".join(SHARES_CACHE.keys()) if SHARES_CACHE else "пусто"
                await message.answer(
                    f"❌ Тикер {ticker_input} не найден.\n"
                    f"Доступные: {available}\n"
                    f"Используй /list",
                    parse_mode="HTML"
                )
                return
            
            logger.info("buy_command_received", ticker=ticker)
            await message.answer(f"⏳ Обрабатываю заявку {ticker}...")
            
            # Запускаем в фоне
            asyncio.create_task(self._place_order(ticker, message))

        @self.dp.message(Command("button"))
        async def cmd_button(message: Message):
            """Тестовая кнопка."""
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔘 Нажми меня", callback_data="test:ping")
            ]])
            await message.answer("🧪 Тестовая кнопка:", reply_markup=keyboard)

        @self.dp.callback_query(F.data == "test:ping")
        async def callback_test(callback: CallbackQuery):
            """Обработка тестовой кнопки."""
            logger.info("test_callback_received!", callback_id=callback.id)
            await callback.answer("🎉 Callback работает!")
            await callback.message.answer("✅ Кнопка нажата! Всё работает.")

        @self.dp.callback_query(F.data.startswith("buy:"))
        async def callback_buy(callback: CallbackQuery):
            """Обработка кнопки покупки."""
            ticker = callback.data.split(":")[1]
            logger.info("buy_callback_received", ticker=ticker, callback_id=callback.id)
            
            # Защита от двойного нажатия
            if ticker in self._processing_tickers:
                await callback.answer(f"⏳ {ticker} уже обрабатывается...")
                return
            
            # Сразу отвечаем (убираем часики)
            await callback.answer(f"✅ Принял {ticker}. Обрабатываю...")
            
            # Запускаем в фоне
            asyncio.create_task(self._place_order(ticker, callback.message))

        @self.dp.callback_query()
        async def callback_unknown(callback: CallbackQuery):
            """Неизвестный callback."""
            logger.warning("unknown_callback", data=callback.data)
            await callback.answer("❓ Неизвестная команда")

    async def _place_order(self, ticker: str, message: Message):
        """Выставляет заявку."""
        logger.info("place_order_started", ticker=ticker)
        self._processing_tickers.add(ticker)
        
        try:
            share_data = get_share_from_cache(ticker)
            
            if not share_data:
                logger.warning("share_not_in_cache", ticker=ticker)
                await message.answer(
                    f"❌ Данные по {ticker} не найдены.\n"
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
                await message.answer(f"❌ Размер позиции меньше 1 лота", parse_mode="HTML")
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
                            f"📥 Цена: {share_data['entry_price']} ₽\n"
                            f"📦 Кол-во: {quantity_lots} лот ({share_data['position_size']} шт)"
                        )
                    else:
                        order_id = result.get("order_id", "N/A")
                        msg = (
                            f"✅ <b>Заявка: {ticker}</b>\n\n"
                            f"📥 Цена: {share_data['entry_price']} ₽\n"
                            f"📦 Кол-во: {quantity_lots} лот\n"
                            f"🆔 ID: <code>{order_id}</code>"
                        )
                    await message.answer(msg, parse_mode="HTML")
                else:
                    error = result.get("error", "Неизвестная ошибка")
                    await message.answer(f"❌ Ошибка: {error}", parse_mode="HTML")
                    
        except Exception as e:
            logger.exception("place_order_error", ticker=ticker)
            await message.answer(f"❌ Исключение: {str(e)}", parse_mode="HTML")
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