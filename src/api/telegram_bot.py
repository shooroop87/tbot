"""
Telegram Bot на aiogram с управлением kill switch.

Команды управления:
- /status - текущий статус бота
- /pause - приостановить бота
- /resume - возобновить работу
- /auto - включить автоматический режим (SL/TP автоматически)
- /manual - ручной режим (только уведомления)
- /kill - экстренное отключение ВСЕГО

Команды торговли:
- /list - список тикеров
- /buy TICKER - выставить заявку
- /orders - активные заявки
- /cancel ORDER_ID - отменить заявку
"""
import asyncio
from typing import Dict, Any, Optional, List, TYPE_CHECKING

import structlog
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command

if TYPE_CHECKING:
    from config import Config
    from executor.position_watcher import PositionWatcher
    from db.repository import Repository

logger = structlog.get_logger()

# Глобальные ссылки
SHARES_CACHE: Dict[str, Dict[str, Any]] = {}
_position_watcher: Optional["PositionWatcher"] = None
_repository: Optional["Repository"] = None
_config: Optional["Config"] = None
_scheduler = None
_watcher_task = None


def set_globals(
    watcher: "PositionWatcher" = None,
    repo: "Repository" = None,
    config: "Config" = None,
    scheduler = None,
    watcher_task = None
):
    """Устанавливает глобальные зависимости."""
    global _position_watcher, _repository, _config, _scheduler, _watcher_task
    if watcher:
        _position_watcher = watcher
    if repo:
        _repository = repo
    if config:
        _config = config
    if scheduler:
        _scheduler = scheduler
    if watcher_task:
        _watcher_task = watcher_task


def update_shares_cache(shares: list):
    """Обновляет кэш акций."""
    global SHARES_CACHE
    SHARES_CACHE.clear()
    for share in shares:
        SHARES_CACHE[share["ticker"]] = share
    logger.info("shares_cache_updated", count=len(SHARES_CACHE))


def get_share_from_cache(ticker: str) -> Optional[Dict[str, Any]]:
    """Получает данные акции из кэша."""
    return SHARES_CACHE.get(ticker)


def escape_html(text: str) -> str:
    """Экранирует HTML-символы."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class TelegramBotAiogram:
    """Telegram бот с управлением kill switch."""

    def __init__(self, config: "Config"):
        self.config = config
        self.bot = Bot(token=config.telegram.bot_token)
        self.dp = Dispatcher()
        self._processing_tickers = set()
        
        # Авторизованные пользователи для опасных команд
        self.authorized_users = set(config.telegram.authorized_users)
        
        self._register_handlers()

    def _is_authorized(self, user_id: int) -> bool:
        """Проверяет авторизацию пользователя."""
        if not self.authorized_users:
            return True  # Если список пуст — все авторизованы
        return user_id in self.authorized_users

    def _register_handlers(self):
        """Регистрирует обработчики."""
        
        # ═══════════════════════════════════════════════════════════════
        # КОМАНДЫ СПРАВКИ
        # ═══════════════════════════════════════════════════════════════
        
        @self.dp.message(Command("start", "help"))
        async def cmd_help(message: Message):
            await message.answer(
                "🤖 <b>Trading Bot</b>\n\n"
                "<b>📊 Управление:</b>\n"
                "/status - текущий статус\n"
                "/pause - приостановить бота\n"
                "/resume - возобновить работу\n"
                "/auto - авто-режим (SL/TP автоматически)\n"
                "/manual - ручной режим (только уведомления)\n"
                "/kill - ⚠️ ЭКСТРЕННОЕ отключение\n\n"
                "<b>📈 Торговля:</b>\n"
                "/list - список тикеров с ценами\n"
                "/buy SBER - выставить заявку\n"
                "/orders - активные заявки\n"
                "/stats - статистика\n\n"
                "⚠️ <i>Торговля несёт риск потери капитала</i>",
                parse_mode="HTML"
            )

        # ═══════════════════════════════════════════════════════════════
        # КОМАНДЫ УПРАВЛЕНИЯ (требуют авторизации)
        # ═══════════════════════════════════════════════════════════════
        
        @self.dp.message(Command("status"))
        async def cmd_status(message: Message):
            """Показывает полный статус бота."""
            if not _repository:
                await message.answer("❌ Репозиторий не инициализирован")
                return
            
            try:
                settings = await _repository.get_bot_settings()
                
                # Статус активности
                if settings.paused_until:
                    from datetime import datetime
                    if datetime.utcnow() < settings.paused_until:
                        active_status = f"⏸ Пауза до {settings.paused_until.strftime('%H:%M')}"
                    else:
                        active_status = "🟢 Активен" if settings.is_active else "🔴 Выключен"
                else:
                    active_status = "🟢 Активен" if settings.is_active else "🔴 Выключен"
                
                # Режим
                mode_emoji = {"auto": "🤖", "manual": "👤", "monitor_only": "👁"}
                mode_status = f"{mode_emoji.get(settings.mode, '❓')} {settings.mode.upper()}"
                
                # Watcher
                watcher_status = "🟢 Работает" if _position_watcher and _position_watcher.is_running else "🔴 Остановлен"
                tracked = _position_watcher.tracked_count if _position_watcher else 0
                
                # Scheduler
                scheduler_status = "🟢 Работает" if _scheduler and _scheduler.running else "🔴 Остановлен"
                
                # Статистика
                stats = await _repository.get_order_stats()
                
                await message.answer(
                    f"📊 <b>Статус бота</b>\n\n"
                    f"<b>Управление:</b>\n"
                    f"• Бот: {active_status}\n"
                    f"• Режим: {mode_status}\n"
                    f"• Watcher: {watcher_status}\n"
                    f"• Scheduler: {scheduler_status}\n\n"
                    f"<b>Заявки:</b>\n"
                    f"• Отслеживается: {tracked}\n"
                    f"• Pending в БД: {stats['by_status'].get('pending', 0)}\n"
                    f"• Исполнено: {stats['by_status'].get('executed', 0)}\n\n"
                    f"<b>Статистика:</b>\n"
                    f"• Всего заявок: {settings.total_orders_placed}\n"
                    f"• SL сработало: {settings.total_sl_triggered}\n"
                    f"• TP сработало: {settings.total_tp_triggered}\n"
                    f"• Общий PnL: {stats['total_pnl_rub']:+,.0f} ₽\n\n"
                    f"<b>Последнее изменение:</b>\n"
                    f"• {settings.last_change_reason or 'N/A'}\n"
                    f"• {settings.last_change_at.strftime('%Y-%m-%d %H:%M') if settings.last_change_at else 'N/A'}",
                    parse_mode="HTML"
                )
                
            except Exception as e:
                logger.exception("cmd_status_error")
                await message.answer(f"❌ Ошибка: {escape_html(str(e))}", parse_mode="HTML")

        @self.dp.message(Command("pause"))
        async def cmd_pause(message: Message):
            """Приостанавливает бота."""
            if not self._is_authorized(message.from_user.id):
                await message.answer("🚫 Нет доступа к этой команде")
                return
            
            if not _repository:
                await message.answer("❌ Репозиторий не инициализирован")
                return
            
            try:
                await _repository.set_bot_active(
                    is_active=False,
                    reason="Paused via /pause command",
                    changed_by=str(message.from_user.id)
                )
                
                await message.answer(
                    "⏸ <b>Бот приостановлен</b>\n\n"
                    "• Новые заявки НЕ принимаются\n"
                    "• Существующие заявки на бирже ОСТАЮТСЯ\n"
                    "• Watcher НЕ следит за позициями\n\n"
                    "Для возобновления: /resume",
                    parse_mode="HTML"
                )
                
            except Exception as e:
                await message.answer(f"❌ Ошибка: {escape_html(str(e))}", parse_mode="HTML")

        @self.dp.message(Command("resume"))
        async def cmd_resume(message: Message):
            """Возобновляет работу бота."""
            if not self._is_authorized(message.from_user.id):
                await message.answer("🚫 Нет доступа к этой команде")
                return
            
            if not _repository:
                await message.answer("❌ Репозиторий не инициализирован")
                return
            
            try:
                settings = await _repository.set_bot_active(
                    is_active=True,
                    reason="Resumed via /resume command",
                    changed_by=str(message.from_user.id)
                )
                
                await message.answer(
                    f"▶️ <b>Бот возобновлён</b>\n\n"
                    f"• Режим: {settings.mode.upper()}\n"
                    f"• Заявки принимаются\n"
                    f"• Watcher активен\n\n"
                    f"Текущий режим: /status",
                    parse_mode="HTML"
                )
                
            except Exception as e:
                await message.answer(f"❌ Ошибка: {escape_html(str(e))}", parse_mode="HTML")

        @self.dp.message(Command("auto"))
        async def cmd_auto(message: Message):
            """Переключает в автоматический режим."""
            if not self._is_authorized(message.from_user.id):
                await message.answer("🚫 Нет доступа к этой команде")
                return
            
            if not _repository:
                await message.answer("❌ Репозиторий не инициализирован")
                return
            
            try:
                # Включаем бота + режим auto
                await _repository.set_bot_active(
                    is_active=True,
                    reason="Switched to AUTO mode",
                    changed_by=str(message.from_user.id)
                )
                await _repository.set_bot_mode(
                    mode="auto",
                    reason="Switched to AUTO mode",
                    changed_by=str(message.from_user.id)
                )
                
                await message.answer(
                    "🤖 <b>АВТОМАТИЧЕСКИЙ РЕЖИМ</b>\n\n"
                    "• При исполнении entry → SL и TP выставляются АВТОМАТИЧЕСКИ\n"
                    "• Watcher следит за позициями\n"
                    "• Полная автоматизация\n\n"
                    "⚠️ Убедитесь что параметры риска верны!\n\n"
                    "Для ручного режима: /manual",
                    parse_mode="HTML"
                )
                
            except Exception as e:
                await message.answer(f"❌ Ошибка: {escape_html(str(e))}", parse_mode="HTML")

        @self.dp.message(Command("manual"))
        async def cmd_manual(message: Message):
            """Переключает в ручной режим."""
            if not self._is_authorized(message.from_user.id):
                await message.answer("🚫 Нет доступа к этой команде")
                return
            
            if not _repository:
                await message.answer("❌ Репозиторий не инициализирован")
                return
            
            try:
                await _repository.set_bot_active(
                    is_active=True,
                    reason="Switched to MANUAL mode",
                    changed_by=str(message.from_user.id)
                )
                await _repository.set_bot_mode(
                    mode="manual",
                    reason="Switched to MANUAL mode",
                    changed_by=str(message.from_user.id)
                )
                
                await message.answer(
                    "👤 <b>РУЧНОЙ РЕЖИМ</b>\n\n"
                    "• При исполнении entry → только УВЕДОМЛЕНИЕ\n"
                    "• SL и TP НЕ выставляются автоматически\n"
                    "• Вы должны выставить их ВРУЧНУЮ в терминале\n\n"
                    "Для авто-режима: /auto",
                    parse_mode="HTML"
                )
                
            except Exception as e:
                await message.answer(f"❌ Ошибка: {escape_html(str(e))}", parse_mode="HTML")

        @self.dp.message(Command("kill"))
        async def cmd_kill(message: Message):
            """Экстренное отключение."""
            if not self._is_authorized(message.from_user.id):
                await message.answer("🚫 Нет доступа к этой команде")
                return
            
            if not _repository:
                await message.answer("❌ Репозиторий не инициализирован")
                return
            
            try:
                await _repository.set_bot_active(
                    is_active=False,
                    reason="KILL SWITCH activated",
                    changed_by=str(message.from_user.id)
                )
                
                # Очищаем кэш
                SHARES_CACHE.clear()
                
                await message.answer(
                    "🔴 <b>KILL SWITCH АКТИВИРОВАН</b>\n\n"
                    "• Бот ПОЛНОСТЬЮ отключен\n"
                    "• Новые заявки НЕ принимаются\n"
                    "• Watcher НЕ работает\n"
                    "• Кэш очищен\n\n"
                    "⚠️ <b>ВАЖНО:</b> Заявки на бирже НЕ отменены!\n"
                    "Отмените их вручную в терминале если нужно.\n\n"
                    "Для возобновления: /resume",
                    parse_mode="HTML"
                )
                
            except Exception as e:
                await message.answer(f"❌ Ошибка: {escape_html(str(e))}", parse_mode="HTML")

        # ═══════════════════════════════════════════════════════════════
        # КОМАНДЫ ТОРГОВЛИ
        # ═══════════════════════════════════════════════════════════════
        
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

        @self.dp.message(Command("orders"))
        async def cmd_orders(message: Message):
            """Показывает активные заявки."""
            if not _position_watcher:
                await message.answer("❌ Watcher не инициализирован")
                return
            
            orders = _position_watcher.get_tracked_orders()
            if not orders:
                await message.answer("📋 Нет активных отслеживаемых заявок")
                return
            
            lines = ["📋 <b>Отслеживаемые заявки:</b>", ""]
            for order_id, order in orders.items():
                emoji = {
                    "entry_buy": "📥",
                    "stop_loss": "🛑", 
                    "take_profit": "🎯"
                }.get(order.order_type.value, "⚪")
                
                lines.append(
                    f"{emoji} <b>{order.ticker}</b> — {order.order_type.value}\n"
                    f"   Вход: {order.entry_price:,.2f} | "
                    f"SL: {order.stop_price:,.2f} | "
                    f"TP: {order.target_price:,.2f}\n"
                    f"   ID: <code>{order_id[:20]}...</code>"
                )
            
            await message.answer("\n".join(lines), parse_mode="HTML")

        @self.dp.message(Command("stats"))
        async def cmd_stats(message: Message):
            """Показывает статистику."""
            if not _repository:
                await message.answer("❌ Репозиторий не инициализирован")
                return
            
            try:
                settings = await _repository.get_bot_settings()
                stats = await _repository.get_order_stats()
                
                win_rate = 0
                total_closed = settings.total_sl_triggered + settings.total_tp_triggered
                if total_closed > 0:
                    win_rate = settings.total_tp_triggered / total_closed * 100
                
                await message.answer(
                    f"📊 <b>Статистика</b>\n\n"
                    f"<b>Заявки:</b>\n"
                    f"• Всего выставлено: {settings.total_orders_placed}\n"
                    f"• SL сработало: {settings.total_sl_triggered}\n"
                    f"• TP сработало: {settings.total_tp_triggered}\n"
                    f"• Win Rate: {win_rate:.1f}%\n\n"
                    f"<b>Результат:</b>\n"
                    f"• Общий PnL: {stats['total_pnl_rub']:+,.0f} ₽",
                    parse_mode="HTML"
                )
                
            except Exception as e:
                await message.answer(f"❌ Ошибка: {escape_html(str(e))}", parse_mode="HTML")

        @self.dp.message(Command("buy"))
        async def cmd_buy(message: Message):
            """Команда /buy TICKER."""
            # Проверяем авторизацию
            if not self._is_authorized(message.from_user.id):
                await message.answer("🚫 Нет доступа к торговым командам")
                return
            
            # Проверяем что бот активен
            if _repository:
                is_active = await _repository.is_bot_active()
                if not is_active:
                    await message.answer(
                        "🔴 Бот выключен. Заявки не принимаются.\n"
                        "Включите: /resume"
                    )
                    return
            
            parts = message.text.split()
            if len(parts) < 2:
                await message.answer("❌ Использование: /buy SBER")
                return
            
            ticker_input = parts[1].upper()
            
            # Ищем в кэше
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
            
            logger.info("buy_command", ticker=ticker, user_id=message.from_user.id)
            await message.answer(f"⏳ Обрабатываю заявку {ticker}...")
            
            # Запускаем в фоне
            asyncio.create_task(self._place_order(ticker, message))

        @self.dp.callback_query()
        async def callback_any(callback: CallbackQuery):
            """Игнорируем старые callback."""
            await callback.answer("Используйте команду /buy TICKER")

    async def _place_order(self, ticker: str, message: Message):
        """Выставляет заявку."""
        from api.tinkoff_client import TinkoffClient
        from executor.order_manager import OrderManager
        from executor.position_watcher import OrderType
        
        self._processing_tickers.add(ticker)
        
        try:
            share_data = get_share_from_cache(ticker)
            
            if not share_data:
                await message.answer(f"❌ Данные по {ticker} не найдены в кэше")
                return
            
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
                
                if result.get("success"):
                    if result.get("dry_run"):
                        msg = (
                            f"🔸 <b>DRY RUN: {ticker}</b>\n\n"
                            f"📋 Тейк-профит покупка\n"
                            f"📥 Цена: {share_data['entry_price']:,.2f} ₽\n"
                            f"📦 Кол-во: {quantity_lots} лот"
                        )
                    else:
                        order_id = result.get("order_id", "N/A")
                        
                        # Добавляем в отслеживание
                        if _position_watcher:
                            await _position_watcher.track_order(
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
                                created_by=str(message.from_user.id),
                            )
                        
                        # Увеличиваем счётчик
                        if _repository:
                            await _repository.increment_stats(orders_placed=1)
                        
                        # Получаем текущий режим
                        mode = "manual"
                        if _repository:
                            mode = await _repository.get_bot_mode()
                        
                        mode_warning = ""
                        if mode == "manual":
                            mode_warning = "\n\n⚠️ Режим MANUAL: SL/TP нужно выставить вручную!"
                        
                        msg = (
                            f"✅ <b>Заявка: {ticker}</b>\n\n"
                            f"📥 Цена входа: {share_data['entry_price']:,.2f} ₽\n"
                            f"🛑 Стоп-лосс: {share_data['stop_price']:,.2f} ₽\n"
                            f"🎯 Тейк-профит: {share_data['take_price']:,.2f} ₽\n"
                            f"📦 Кол-во: {quantity_lots} лот\n"
                            f"🆔 ID: <code>{order_id}</code>\n\n"
                            f"🔍 Отслеживание активно"
                            f"{mode_warning}"
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
        logger.info("telegram_bot_starting")
        await self.dp.start_polling(self.bot)

    async def stop(self):
        """Останавливает бота."""
        logger.info("telegram_bot_stopping")
        await self.bot.session.close()