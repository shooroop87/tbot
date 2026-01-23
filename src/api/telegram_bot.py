"""
Telegram Bot на aiogram с управлением kill switch и свободным трейдингом.

Команды управления:
- /status - текущий статус бота
- /pause - приостановить бота
- /resume - возобновить работу
- /auto - включить автоматический режим (SL/TP автоматически)
- /manual - ручной режим (только уведомления)
- /kill - экстренное отключение ВСЕГО

Команды торговли:
- /list - список тикеров из кэша (Bollinger)
- /buy TICKER - выставить заявку по цене из кэша
- /buy TICKER PRICE - выставить заявку по своей цене (лоты авто)
- /buy TICKER PRICE LOTS - выставить заявку полностью вручную
- /orders - активные заявки
- /cancel ORDER_ID - отменить заявку
- /stats - статистика
"""
import asyncio
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Any, Optional, List, TYPE_CHECKING

import structlog
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command

if TYPE_CHECKING:
    from config import Config
    from executor.position_watcher import PositionWatcher
    from db.repository import Repository

logger = structlog.get_logger()


# ═══════════════════════════════════════════════════════════════════════════════
# ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ
# ═══════════════════════════════════════════════════════════════════════════════

# Кэш акций с расчётами (заполняется из main.py)
SHARES_CACHE: Dict[str, Dict[str, Any]] = {}

# Зависимости (устанавливаются через set_globals)
_position_watcher: Optional["PositionWatcher"] = None
_repository: Optional["Repository"] = None
_config: Optional["Config"] = None
_scheduler = None
_watcher_task = None


def set_globals(
    watcher: "PositionWatcher" = None,
    repo: "Repository" = None,
    config: "Config" = None,
    scheduler=None,
    watcher_task=None
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
    return SHARES_CACHE.get(ticker.upper())


def escape_html(text: str) -> str:
    """Экранирует HTML-символы."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ═══════════════════════════════════════════════════════════════════════════════
# PENDING ORDERS (для подтверждения)
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class PendingOrder:
    """Ожидающая подтверждения заявка."""
    ticker: str
    figi: str
    entry_price: float
    quantity_lots: int
    lot_size: int
    atr: float
    sl_price: float
    tp_price: float
    risk_rub: float
    risk_pct: float
    reward_rub: float
    position_value: float
    created_at: datetime
    user_id: int


# Словарь pending заявок: callback_id -> PendingOrder
_pending_orders: Dict[str, PendingOrder] = {}


# ═══════════════════════════════════════════════════════════════════════════════
# TELEGRAM BOT
# ═══════════════════════════════════════════════════════════════════════════════

class TelegramBotAiogram:
    """Telegram бот с управлением kill switch и свободным трейдингом."""

    # Таймаут подтверждения заявки (секунды)
    CONFIRMATION_TIMEOUT = 60

    def __init__(self, config: "Config"):
        self.config = config
        self.bot = Bot(token=config.telegram.bot_token)
        self.dp = Dispatcher()
        self._processing_tickers = set()
        
        # Авторизованные пользователи для опасных команд
        self.authorized_users = set(config.telegram.authorized_users)
        
        # Валидатор для свободного трейдинга
        self._validator = None
        if config.free_trading.enabled:
            self._init_validator()
        
        self._register_handlers()

    def _init_validator(self):
        """Инициализирует валидатор для свободного трейдинга."""
        from executor.order_validator import OrderValidator, FreeTradeConfig
        
        ft_config = FreeTradeConfig(
            enabled=self.config.free_trading.enabled,
            max_price_deviation_pct=self.config.free_trading.max_price_deviation_pct,
            max_concurrent_positions=self.config.free_trading.max_concurrent_positions,
            max_daily_trades=self.config.free_trading.max_daily_trades,
            max_daily_loss_rub=self.config.free_trading.max_daily_loss_rub,
            sl_placement_timeout_sec=self.config.free_trading.sl_placement_timeout_sec,
            confirmation_timeout_sec=self.config.free_trading.confirmation_timeout_sec,
            trading_start=self.config.free_trading.trading_start,
            trading_end=self.config.free_trading.trading_end,
            sl_atr_multiplier=self.config.free_trading.sl_atr_multiplier,
            tp_atr_multiplier=self.config.free_trading.tp_atr_multiplier,
        )
        
        self._validator = OrderValidator(self.config, ft_config)
        logger.info("free_trading_validator_initialized")

    def _is_authorized(self, user_id: int) -> bool:
        """Проверяет авторизацию пользователя."""
        if not self.authorized_users:
            return True
        return user_id in self.authorized_users

    def _register_handlers(self):
        """Регистрирует обработчики."""
        
        # ═══════════════════════════════════════════════════════════════════════
        # БАЗОВЫЕ КОМАНДЫ
        # ═══════════════════════════════════════════════════════════════════════
        
        @self.dp.message(Command("start", "help"))
        async def cmd_start(message: Message):
            ft_status = "✅" if self.config.free_trading.enabled else "❌"
            await message.answer(
                f"🤖 <b>Trading Bot</b>\n\n"
                f"<b>Управление:</b>\n"
                f"/status - статус бота\n"
                f"/pause - приостановить\n"
                f"/resume - возобновить\n"
                f"/auto - авто режим (SL/TP)\n"
                f"/manual - ручной режим\n"
                f"/kill - экстренное отключение\n\n"
                f"<b>Торговля:</b>\n"
                f"/list - тикеры из кэша\n"
                f"/buy SBER - по цене из кэша\n"
                f"/buy SBER 250 - своя цена\n"
                f"/buy SBER 250 10 - цена + лоты\n"
                f"/orders - активные заявки\n"
                f"/stats - статистика\n\n"
                f"<b>Free Trading:</b> {ft_status}\n"
                f"<b>Dry Run:</b> {'✅' if self.config.dry_run else '❌'}",
                parse_mode="HTML"
            )

        @self.dp.message(Command("status"))
        async def cmd_status(message: Message):
            """Показывает статус бота."""
            if not _repository:
                await message.answer("❌ Репозиторий не инициализирован")
                return
            
            try:
                is_active = await _repository.is_bot_active()
                mode = await _repository.get_bot_mode()
                
                watcher_status = "🟢 работает" if _position_watcher and _position_watcher.is_running else "🔴 остановлен"
                tracked = _position_watcher.tracked_count if _position_watcher else 0
                cache_count = len(SHARES_CACHE)
                
                await message.answer(
                    f"📊 <b>Статус бота</b>\n\n"
                    f"<b>Состояние:</b> {'🟢 Активен' if is_active else '🔴 Остановлен'}\n"
                    f"<b>Режим:</b> {mode.upper()}\n"
                    f"<b>Watcher:</b> {watcher_status}\n"
                    f"<b>Отслеживается:</b> {tracked} заявок\n"
                    f"<b>В кэше:</b> {cache_count} тикеров\n"
                    f"<b>Dry Run:</b> {'✅ Да' if self.config.dry_run else '❌ Нет'}\n"
                    f"<b>Free Trading:</b> {'✅' if self.config.free_trading.enabled else '❌'}",
                    parse_mode="HTML"
                )
            except Exception as e:
                await message.answer(f"❌ Ошибка: {escape_html(str(e))}", parse_mode="HTML")

        # ═══════════════════════════════════════════════════════════════════════
        # КОМАНДЫ УПРАВЛЕНИЯ
        # ═══════════════════════════════════════════════════════════════════════

        @self.dp.message(Command("pause"))
        async def cmd_pause(message: Message):
            """Приостанавливает бота."""
            if not self._is_authorized(message.from_user.id):
                await message.answer("🚫 Нет доступа")
                return
            
            if _repository:
                await _repository.set_bot_active(False, "paused", str(message.from_user.id))
            
            await message.answer(
                "⏸ <b>Бот приостановлен</b>\n\n"
                "• Новые заявки не выставляются\n"
                "• Существующие заявки на бирже активны\n\n"
                "Для возобновления: /resume",
                parse_mode="HTML"
            )

        @self.dp.message(Command("resume"))
        async def cmd_resume(message: Message):
            """Возобновляет работу бота."""
            if not self._is_authorized(message.from_user.id):
                await message.answer("🚫 Нет доступа")
                return
            
            if _repository:
                await _repository.set_bot_active(True, "resumed", str(message.from_user.id))
            
            await message.answer(
                "▶️ <b>Бот возобновлён</b>\n\n"
                "Заявки принимаются.",
                parse_mode="HTML"
            )

        @self.dp.message(Command("auto"))
        async def cmd_auto(message: Message):
            """Включает авто режим."""
            if not self._is_authorized(message.from_user.id):
                await message.answer("🚫 Нет доступа")
                return
            
            if _repository:
                await _repository.set_bot_mode("auto")
            
            await message.answer(
                "🤖 <b>Режим: AUTO</b>\n\n"
                "SL и TP выставляются автоматически.",
                parse_mode="HTML"
            )

        @self.dp.message(Command("manual"))
        async def cmd_manual(message: Message):
            """Включает ручной режим."""
            if not self._is_authorized(message.from_user.id):
                await message.answer("🚫 Нет доступа")
                return
            
            if _repository:
                await _repository.set_bot_mode("manual")
            
            await message.answer(
                "👤 <b>Режим: MANUAL</b>\n\n"
                "SL и TP НЕ выставляются автоматически.\n"
                "Только уведомления о событиях.",
                parse_mode="HTML"
            )

        @self.dp.message(Command("kill"))
        async def cmd_kill(message: Message):
            """Экстренное отключение."""
            if not self._is_authorized(message.from_user.id):
                await message.answer("🚫 Нет доступа")
                return
            
            if _repository:
                await _repository.set_bot_active(False, "KILL SWITCH", str(message.from_user.id))
            
            SHARES_CACHE.clear()
            
            await message.answer(
                "🔴 <b>KILL SWITCH АКТИВИРОВАН</b>\n\n"
                "• Бот ПОЛНОСТЬЮ отключен\n"
                "• Кэш очищен\n\n"
                "⚠️ Заявки на бирже НЕ отменены!\n"
                "Отмените их вручную.\n\n"
                "Для возобновления: /resume",
                parse_mode="HTML"
            )

        # ═══════════════════════════════════════════════════════════════════════
        # КОМАНДЫ ТОРГОВЛИ
        # ═══════════════════════════════════════════════════════════════════════

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
            for ticker, data in list(SHARES_CACHE.items())[:20]:  # Лимит 20
                entry = data.get("entry_price", 0)
                signal = "🟢" if data.get("signal") == "BUY" else "⚪"
                lines.append(f"{signal} <code>/buy {ticker}</code> — вход {entry:,.2f}₽")
            
            if len(SHARES_CACHE) > 20:
                lines.append(f"\n... и ещё {len(SHARES_CACHE) - 20}")
            
            await message.answer("\n".join(lines), parse_mode="HTML")

        @self.dp.message(Command("orders"))
        async def cmd_orders(message: Message):
            """Показывает активные заявки."""
            if not _position_watcher:
                await message.answer("❌ Watcher не инициализирован")
                return
            
            orders = _position_watcher.get_tracked_orders()
            if not orders:
                await message.answer("📋 Нет активных заявок")
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
                
                sl_count = settings.total_sl_triggered or 0
                tp_count = settings.total_tp_triggered or 0
                orders_count = settings.total_orders_placed or 0
                total_pnl = settings.total_pnl_rub or 0
                
                total_closed = sl_count + tp_count
                win_rate = (tp_count / total_closed * 100) if total_closed > 0 else 0
                
                await message.answer(
                    f"📊 <b>Статистика</b>\n\n"
                    f"<b>Заявки:</b>\n"
                    f"• Всего выставлено: {orders_count}\n"
                    f"• SL сработало: {sl_count}\n"
                    f"• TP сработало: {tp_count}\n"
                    f"• Win Rate: {win_rate:.1f}%\n\n"
                    f"<b>Результат:</b>\n"
                    f"• Общий PnL: {total_pnl:+,.0f} ₽",
                    parse_mode="HTML"
                )
            except Exception as e:
                await message.answer(f"❌ Ошибка: {escape_html(str(e))}", parse_mode="HTML")

        # ═══════════════════════════════════════════════════════════════════════
        # КОМАНДА /buy С ПОДДЕРЖКОЙ FREE TRADING
        # ═══════════════════════════════════════════════════════════════════════

        @self.dp.message(Command("buy"))
        async def cmd_buy(message: Message):
            """
            Команда /buy с поддержкой разных форматов:
            - /buy SBER — цена из кэша, лоты авто
            - /buy SBER 250 — своя цена, лоты авто
            - /buy SBER 250 10 — своя цена, свои лоты
            """
            if not self._is_authorized(message.from_user.id):
                await message.answer("🚫 Нет доступа")
                return
            
            # Парсим аргументы
            ticker, price, lots, error = self._parse_buy_command(message.text)
            
            if error:
                await message.answer(
                    f"❌ {error}\n\n"
                    f"<b>Формат:</b>\n"
                    f"<code>/buy SBER</code> — по цене из кэша\n"
                    f"<code>/buy SBER 250</code> — своя цена\n"
                    f"<code>/buy SBER 250 10</code> — цена + лоты",
                    parse_mode="HTML"
                )
                return
            
            # Получаем данные из кэша
            share_data = get_share_from_cache(ticker)
            if not share_data:
                await message.answer(
                    f"❌ Тикер {ticker} не найден в кэше.\n"
                    f"Дождитесь расчёта или проверьте /list"
                )
                return
            
            # Определяем цену
            if price is None:
                # Старое поведение: цена из кэша
                entry_price = share_data.get("entry_price")
                if not entry_price:
                    await message.answer(f"❌ Нет цены входа для {ticker}")
                    return
            else:
                entry_price = price
            
            # Если free_trading включён и указана своя цена — валидация + подтверждение
            if self.config.free_trading.enabled and price is not None:
                await self._handle_free_trading_buy(
                    message, ticker, entry_price, lots, share_data
                )
            else:
                # Старое поведение без подтверждения
                await self._place_order_legacy(message, ticker, share_data)

        # ═══════════════════════════════════════════════════════════════════════
        # CALLBACK HANDLERS (подтверждение заявок)
        # ═══════════════════════════════════════════════════════════════════════

        @self.dp.callback_query(F.data.startswith("confirm:"))
        async def callback_confirm(callback: CallbackQuery):
            """Подтверждение заявки."""
            callback_id = callback.data.replace("confirm:", "")
            
            pending = _pending_orders.pop(callback_id, None)
            if not pending:
                await callback.answer("⏰ Время подтверждения истекло", show_alert=True)
                return
            
            if callback.from_user.id != pending.user_id:
                await callback.answer("🚫 Это не ваша заявка", show_alert=True)
                _pending_orders[callback_id] = pending
                return
            
            await callback.answer("⏳ Выставляю заявку...")
            
            # Выставляем заявку
            result = await self._place_order_with_params(pending)
            
            if result["success"]:
                dry_run_note = " (DRY RUN)" if result.get("dry_run") else ""
                await callback.message.edit_text(
                    f"✅ <b>Заявка выставлена!{dry_run_note}</b>\n\n"
                    f"📌 {pending.ticker}\n"
                    f"📥 Цена: {pending.entry_price:,.2f} ₽\n"
                    f"📦 Кол-во: {pending.quantity_lots} лот\n"
                    f"🛑 SL: {pending.sl_price:,.2f} ₽\n"
                    f"🎯 TP: {pending.tp_price:,.2f} ₽\n\n"
                    f"🔍 ID: <code>{result.get('order_id', 'N/A')[:20]}...</code>\n\n"
                    f"⏳ Отслеживание запущено",
                    parse_mode="HTML"
                )
            else:
                await callback.message.edit_text(
                    f"❌ <b>Ошибка выставления заявки</b>\n\n"
                    f"📌 {pending.ticker}\n"
                    f"💥 {result.get('error', 'Неизвестная ошибка')}",
                    parse_mode="HTML"
                )

        @self.dp.callback_query(F.data.startswith("cancel:"))
        async def callback_cancel(callback: CallbackQuery):
            """Отмена заявки."""
            callback_id = callback.data.replace("cancel:", "")
            
            pending = _pending_orders.pop(callback_id, None)
            if not pending:
                await callback.answer("Заявка уже обработана")
                return
            
            await callback.answer("Отменено")
            await callback.message.edit_text(
                f"⚪ <b>Заявка отменена</b>\n\n"
                f"📌 {pending.ticker} @ {pending.entry_price:,.2f}",
                parse_mode="HTML"
            )

    # ═══════════════════════════════════════════════════════════════════════════
    # HELPER METHODS
    # ═══════════════════════════════════════════════════════════════════════════

    def _parse_buy_command(self, text: str):
        """
        Парсит команду /buy.
        
        Returns:
            (ticker, price, lots, error_message)
        """
        text = text.strip()
        if text.lower().startswith("/buy"):
            text = text[4:].strip()
        
        if not text:
            return None, None, None, "Укажите тикер"
        
        parts = text.split()
        ticker = parts[0].upper()
        
        if not re.match(r'^[A-Z]{1,10}$', ticker):
            return None, None, None, f"Некорректный тикер: {ticker}"
        
        price = None
        lots = None
        
        if len(parts) >= 2:
            try:
                price = float(parts[1].replace(",", "."))
                if price <= 0:
                    return None, None, None, "Цена должна быть > 0"
            except ValueError:
                return None, None, None, f"Некорректная цена: {parts[1]}"
        
        if len(parts) >= 3:
            try:
                lots = int(parts[2])
                if lots <= 0:
                    return None, None, None, "Количество лотов должно быть > 0"
            except ValueError:
                return None, None, None, f"Некорректное количество: {parts[2]}"
        
        return ticker, price, lots, ""

    def _generate_callback_id(self, ticker: str, user_id: int) -> str:
        """Генерирует уникальный ID для callback."""
        ts = int(datetime.now().timestamp() * 1000) % 1_000_000
        return f"ft:{ticker}:{user_id}:{ts}"

    def _create_confirmation_keyboard(self, callback_id: str) -> InlineKeyboardMarkup:
        """Создаёт клавиатуру подтверждения."""
        return InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"confirm:{callback_id}"),
                InlineKeyboardButton(text="❌ Отмена", callback_data=f"cancel:{callback_id}"),
            ]
        ])

    async def _get_current_price(self, figi: str) -> Optional[float]:
        """Получает текущую цену инструмента."""
        from api.tinkoff_client import TinkoffClient
        
        try:
            async with TinkoffClient(self.config.tinkoff) as client:
                from t_tech.invest.utils import quotation_to_decimal
                response = await client._services.market_data.get_last_prices(figi=[figi])
                if response.last_prices:
                    return float(quotation_to_decimal(response.last_prices[0].price))
        except Exception as e:
            logger.error("get_price_error", figi=figi, error=str(e))
        return None

    async def _count_current_positions(self) -> int:
        """Считает текущие открытые позиции."""
        if not _position_watcher:
            return 0
        orders = _position_watcher.get_tracked_orders()
        return sum(1 for o in orders.values() 
                   if o.order_type.value == "entry_buy" and not o.is_executed)

    def _calculate_auto_lots(self, entry_price: float, atr: float, lot_size: int) -> int:
        """Рассчитывает количество лотов по риску."""
        deposit = self.config.trading.deposit_rub
        risk_pct = self.config.trading.risk_per_trade_pct
        max_risk_rub = deposit * risk_pct
        
        sl_offset = atr * self.config.free_trading.sl_atr_multiplier
        risk_per_lot = sl_offset * lot_size
        
        if risk_per_lot <= 0:
            return 1
        
        return max(1, int(max_risk_rub / risk_per_lot))

    async def _handle_free_trading_buy(
        self,
        message: Message,
        ticker: str,
        entry_price: float,
        lots: Optional[int],
        share_data: Dict
    ):
        """Обрабатывает /buy с free trading (валидация + подтверждение)."""
        if not self._validator:
            await message.answer("❌ Free trading не инициализирован")
            return
        
        figi = share_data["figi"]
        lot_size = share_data.get("lot_size", 1)
        atr = share_data.get("atr", 0)
        
        if atr <= 0:
            await message.answer(
                f"❌ ATR для {ticker} не рассчитан.\n"
                f"Запустите: <code>python main.py --now</code>",
                parse_mode="HTML"
            )
            return
        
        # Получаем текущую цену
        current_price = await self._get_current_price(figi)
        if not current_price:
            await message.answer(f"❌ Не удалось получить цену {ticker}")
            return
        
        # Авто-расчёт лотов если не указано
        if lots is None:
            lots = self._calculate_auto_lots(entry_price, atr, lot_size)
        
        # Валидация
        current_positions = await self._count_current_positions()
        validation = await self._validator.validate_buy_order(
            ticker=ticker,
            entry_price=entry_price,
            quantity_lots=lots,
            current_price=current_price,
            atr=atr,
            lot_size=lot_size,
            current_positions=current_positions
        )
        
        if not validation.is_valid:
            lines = [f"❌ <b>Заявка отклонена: {ticker}</b>", ""]
            lines.extend(validation.errors)
            await message.answer("\n".join(lines), parse_mode="HTML")
            return
        
        # Создаём pending order
        callback_id = self._generate_callback_id(ticker, message.from_user.id)
        
        pending = PendingOrder(
            ticker=ticker,
            figi=figi,
            entry_price=entry_price,
            quantity_lots=lots,
            lot_size=lot_size,
            atr=atr,
            sl_price=validation.sl_price,
            tp_price=validation.tp_price,
            risk_rub=validation.risk_rub,
            risk_pct=validation.risk_pct,
            reward_rub=validation.reward_rub,
            position_value=validation.position_value,
            created_at=datetime.now(),
            user_id=message.from_user.id,
        )
        
        _pending_orders[callback_id] = pending
        
        # Формируем сообщение подтверждения
        quantity_shares = lots * lot_size
        lines = [
            f"📋 <b>Подтвердите заявку</b>",
            "",
            f"📌 <b>{ticker}</b>",
            f"📥 Цена входа: <b>{entry_price:,.2f} ₽</b>",
            f"📦 Количество: {lots} лот ({quantity_shares} шт)",
            "",
            f"🛑 Stop-Loss: <b>{validation.sl_price:,.2f} ₽</b>",
            f"🎯 Take-Profit: <b>{validation.tp_price:,.2f} ₽</b>",
            "",
            f"💸 Риск: <b>{validation.risk_rub:,.0f} ₽</b> ({validation.risk_pct:.2f}%)",
            f"💰 Потенц. прибыль: {validation.reward_rub:,.0f} ₽",
            f"📊 R:R = 1:{validation.risk_reward_ratio:.1f}",
            f"💼 Размер позиции: {validation.position_value:,.0f} ₽",
        ]
        
        if validation.warnings:
            lines.append("")
            lines.extend(validation.warnings)
        
        keyboard = self._create_confirmation_keyboard(callback_id)
        
        await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=keyboard)
        
        # Таймаут
        asyncio.create_task(self._confirmation_timeout(callback_id, message.chat.id))

    async def _confirmation_timeout(self, callback_id: str, chat_id: int):
        """Таймаут ожидания подтверждения."""
        await asyncio.sleep(self.CONFIRMATION_TIMEOUT)
        
        pending = _pending_orders.pop(callback_id, None)
        if pending:
            try:
                await self.bot.send_message(
                    chat_id=chat_id,
                    text=f"⏰ <b>Время подтверждения истекло</b>\n\n"
                         f"📌 {pending.ticker} @ {pending.entry_price:,.2f}",
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.error("timeout_notification_error", error=str(e))

    async def _place_order_with_params(self, pending: PendingOrder) -> Dict[str, Any]:
        """Выставляет заявку с указанными параметрами."""
        from api.tinkoff_client import TinkoffClient
        from executor.order_manager import OrderManager
        from executor.position_watcher import OrderType
        
        try:
            async with TinkoffClient(self.config.tinkoff) as client:
                order_manager = OrderManager(client, self.config)
                
                result = await order_manager.place_take_profit_buy(
                    figi=pending.figi,
                    quantity=pending.quantity_lots,
                    price=pending.entry_price,
                )
                
                if not result.get("success"):
                    return result
                
                order_id = result.get("order_id") or result.get("stop_order_id")
                
                if result.get("dry_run"):
                    return {"success": True, "dry_run": True, "order_id": "DRY_RUN"}
                
                # Добавляем в отслеживание
                if _position_watcher:
                    await _position_watcher.track_order(
                        order_id=order_id,
                        ticker=pending.ticker,
                        figi=pending.figi,
                        order_type=OrderType.ENTRY_BUY,
                        quantity=pending.quantity_lots,
                        entry_price=pending.entry_price,
                        stop_price=pending.sl_price,
                        target_price=pending.tp_price,
                        stop_offset=pending.entry_price - pending.sl_price,
                        take_offset=pending.tp_price - pending.entry_price,
                        lot_size=pending.lot_size,
                        atr=pending.atr,
                        created_by=str(pending.user_id),
                    )
                
                # Увеличиваем счётчик
                if self._validator:
                    self._validator.increment_daily_trades()
                
                return {"success": True, "order_id": order_id}
                
        except Exception as e:
            logger.exception("place_order_error", ticker=pending.ticker)
            return {"success": False, "error": str(e)}

    async def _place_order_legacy(self, message: Message, ticker: str, share_data: Dict):
        """Старое поведение /buy (без подтверждения)."""
        from api.tinkoff_client import TinkoffClient
        from executor.order_manager import OrderManager
        from executor.position_watcher import OrderType
        
        if ticker in self._processing_tickers:
            await message.answer(f"⏳ Заявка по {ticker} уже обрабатывается...")
            return
        
        self._processing_tickers.add(ticker)
        
        try:
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
                        order_id = result.get("order_id") or result.get("stop_order_id")
                        
                        if _position_watcher:
                            await _position_watcher.track_order(
                                order_id=order_id,
                                ticker=ticker,
                                figi=share_data["figi"],
                                order_type=OrderType.ENTRY_BUY,
                                quantity=quantity_lots,
                                entry_price=share_data["entry_price"],
                                stop_price=share_data.get("stop_price", 0),
                                target_price=share_data.get("take_price", 0),
                                stop_offset=share_data.get("stop_offset", 0),
                                take_offset=share_data.get("take_offset", 0),
                                lot_size=lot_size,
                                atr=share_data.get("atr", 0),
                            )
                        
                        msg = (
                            f"✅ <b>Заявка выставлена: {ticker}</b>\n\n"
                            f"📥 Цена: {share_data['entry_price']:,.2f} ₽\n"
                            f"📦 Кол-во: {quantity_lots} лот\n"
                            f"🔍 ID: <code>{order_id[:20]}...</code>"
                        )
                else:
                    msg = f"❌ Ошибка: {result.get('error', 'Неизвестная ошибка')}"
                
                await message.answer(msg, parse_mode="HTML")
                
        except Exception as e:
            logger.exception("place_order_legacy_error", ticker=ticker)
            await message.answer(f"❌ Ошибка: {escape_html(str(e))}", parse_mode="HTML")
        finally:
            self._processing_tickers.discard(ticker)

    # ═══════════════════════════════════════════════════════════════════════════
    # BOT LIFECYCLE
    # ═══════════════════════════════════════════════════════════════════════════

    async def start_polling(self):
        """Запускает бота."""
        logger.info("telegram_bot_starting")
        await self.dp.start_polling(self.bot)

    async def stop(self):
        """Останавливает бота."""
        logger.info("telegram_bot_stopping")
        await self.bot.session.close()
