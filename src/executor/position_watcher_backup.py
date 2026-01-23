"""
Мониторинг заявок и позиций с персистентностью.

Логика:
1. При старте загружает pending заявки из БД
2. Перед любым действием проверяет is_active (kill switch)
3. Следит за стоп-заявками каждые 5 сек
4. Когда заявка исполнена → выставляем SL и TP (если mode=auto)
5. Сохраняет все изменения в БД

Режимы работы:
- auto: полный автомат (SL/TP выставляются автоматически)
- manual: только уведомления, заявки НЕ выставляются
- monitor_only: только мониторинг, без уведомлений о действиях
"""
import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Any, Optional, Set, TYPE_CHECKING
from enum import Enum

import structlog

from db.repository import Repository

if TYPE_CHECKING:
    from config import Config
    from api.tinkoff_client import TinkoffClient
    from api.telegram_notifier import TelegramNotifier

logger = structlog.get_logger()


class OrderType(Enum):
    """Типы отслеживаемых заявок."""
    ENTRY_BUY = "entry_buy"       # Вход в позицию (take-profit buy)
    STOP_LOSS = "stop_loss"       # Стоп-лосс на продажу
    TAKE_PROFIT = "take_profit"   # Тейк-профит на продажу


@dataclass
class TrackedOrder:
    """Отслеживаемая заявка (in-memory представление)."""
    order_id: str
    ticker: str
    figi: str
    order_type: OrderType
    quantity: int  # в лотах
    
    # Цены
    entry_price: float
    stop_price: float
    target_price: float
    
    # Расчётные параметры
    stop_offset: float = 0
    take_offset: float = 0
    lot_size: int = 1
    atr: float = 0
    
    # Статус
    is_executed: bool = False
    executed_price: Optional[float] = None
    executed_at: Optional[datetime] = None
    
    # Связанные заявки
    parent_order_id: Optional[str] = None
    sl_order_id: Optional[str] = None
    tp_order_id: Optional[str] = None
    
    # Кто создал
    created_by: Optional[str] = None


class PositionWatcher:
    """
    Мониторинг заявок с персистентностью и kill switch.
    
    ⚠️ БЕЗОПАСНОСТЬ:
    - Перед ЛЮБЫМ действием проверяет is_active в БД
    - При mode=manual только уведомляет, не выставляет заявки
    - Все заявки сохраняются в БД и переживают рестарт
    
    Использование:
        watcher = PositionWatcher(config, repo, notifier)
        
        # При выставлении заявки
        await watcher.track_order(order_id, ticker, ...)
        
        # Запуск мониторинга
        await watcher.start()
    """

    def __init__(
        self, 
        config: "Config", 
        repository: Repository,
        notifier: "TelegramNotifier",
        poll_interval: float = 5.0
    ):
        self.config = config
        self.repo = repository
        self.notifier = notifier
        self.poll_interval = poll_interval
        
        self._running = False
        self._tracked_orders: Dict[str, TrackedOrder] = {}
        self._executed_orders: Set[str] = set()
        
        self.logger = logger.bind(component="position_watcher")

    async def _check_bot_active(self) -> bool:
        """
        Проверяет активен ли бот.
        
        ⚠️ Вызывается перед ЛЮБЫМ действием!
        """
        try:
            return await self.repo.is_bot_active()
        except Exception as e:
            self.logger.error("check_bot_active_error", error=str(e))
            # При ошибке БД — считаем что бот ВЫКЛЮЧЕН (безопасность)
            return False

    async def _get_bot_mode(self) -> str:
        """Получает текущий режим работы."""
        try:
            return await self.repo.get_bot_mode()
        except Exception:
            return "manual"  # При ошибке — безопасный режим

    async def load_pending_orders(self):
        """
        Загружает pending заявки из БД при старте.
        
        Вызывается автоматически в start().
        """
        try:
            pending = await self.repo.get_pending_orders()
            
            for order_db in pending:
                order = TrackedOrder(
                    order_id=order_db.order_id,
                    ticker=order_db.ticker,
                    figi=order_db.figi,
                    order_type=OrderType(order_db.order_type),
                    quantity=order_db.quantity,
                    entry_price=order_db.entry_price,
                    stop_price=order_db.stop_price,
                    target_price=order_db.target_price,
                    stop_offset=order_db.stop_offset or 0,
                    take_offset=order_db.take_offset or 0,
                    lot_size=order_db.lot_size or 1,
                    atr=order_db.atr or 0,
                    parent_order_id=order_db.parent_order_id,
                    sl_order_id=order_db.sl_order_id,
                    tp_order_id=order_db.tp_order_id,
                    created_by=order_db.created_by,
                )
                self._tracked_orders[order.order_id] = order
            
            self.logger.info("pending_orders_loaded", count=len(pending))
            
            if pending:
                await self.notifier.send_message(
                    f"🔄 <b>Восстановлено {len(pending)} заявок</b>\n"
                    f"Заявки загружены из БД после рестарта."
                )
                
        except Exception as e:
            self.logger.exception("load_pending_orders_error", error=str(e))

    async def track_order(
        self,
        order_id: str,
        ticker: str,
        figi: str,
        order_type: OrderType,
        quantity: int,
        entry_price: float,
        stop_price: float,
        target_price: float,
        stop_offset: float = 0,
        take_offset: float = 0,
        lot_size: int = 1,
        atr: float = 0,
        parent_order_id: Optional[str] = None,
        created_by: Optional[str] = None,
    ):
        """
        Добавляет заявку в отслеживание.
        
        Сохраняет в in-memory кэш И в БД.
        """
        # Проверяем kill switch
        if not await self._check_bot_active():
            self.logger.warning("track_order_blocked_inactive", order_id=order_id)
            return
        
        order = TrackedOrder(
            order_id=order_id,
            ticker=ticker,
            figi=figi,
            order_type=order_type,
            quantity=quantity,
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            stop_offset=stop_offset,
            take_offset=take_offset,
            lot_size=lot_size,
            atr=atr,
            parent_order_id=parent_order_id,
            created_by=created_by,
        )
        
        # Сохраняем в память
        self._tracked_orders[order_id] = order
        
        # Сохраняем в БД
        try:
            await self.repo.save_tracked_order({
                "order_id": order_id,
                "ticker": ticker,
                "figi": figi,
                "order_type": order_type.value,
                "quantity": quantity,
                "lot_size": lot_size,
                "entry_price": entry_price,
                "stop_price": stop_price,
                "target_price": target_price,
                "stop_offset": stop_offset,
                "take_offset": take_offset,
                "atr": atr,
                "status": "pending",
                "parent_order_id": parent_order_id,
                "created_by": created_by,
            })
        except Exception as e:
            self.logger.error("save_tracked_order_error", order_id=order_id, error=str(e))
        
        self.logger.info("order_tracked", 
                        order_id=order_id, 
                        ticker=ticker, 
                        type=order_type.value)

    async def untrack_order(self, order_id: str, reason: str = "manual"):
        """Удаляет заявку из отслеживания."""
        if order_id in self._tracked_orders:
            del self._tracked_orders[order_id]
        
        # Обновляем статус в БД
        try:
            await self.repo.mark_order_cancelled(order_id, reason)
        except Exception as e:
            self.logger.error("untrack_order_db_error", order_id=order_id, error=str(e))
        
        self.logger.info("order_untracked", order_id=order_id, reason=reason)

    async def start(self):
        """Запускает мониторинг."""
        self._running = True
        self.logger.info("position_watcher_starting")
        
        # Загружаем pending заявки из БД
        await self.load_pending_orders()
        
        consecutive_errors = 0
        max_consecutive_errors = 5
        
        while self._running:
            # Проверяем kill switch ПЕРЕД каждой итерацией
            if not await self._check_bot_active():
                self.logger.debug("watcher_paused_inactive")
                await asyncio.sleep(self.poll_interval * 2)  # Реже проверяем когда выключен
                continue
            
            try:
                await self._check_orders()
                consecutive_errors = 0
                
            except Exception as e:
                consecutive_errors += 1
                self.logger.exception("watcher_error", 
                                     error=str(e), 
                                     consecutive=consecutive_errors)
                
                if consecutive_errors == 1:
                    await self.notifier.send_message(
                        f"⚠️ <b>Watcher: ошибка</b>\n"
                        f"📛 {str(e)[:200]}\n"
                        f"🔄 Продолжаю работу..."
                    )
                elif consecutive_errors >= max_consecutive_errors:
                    await self.notifier.send_message(
                        f"🔴 <b>Watcher: {consecutive_errors} ошибок подряд!</b>\n"
                        f"⏳ Пауза 60 сек..."
                    )
                    await asyncio.sleep(60)
                    consecutive_errors = 0
                    continue
            
            await asyncio.sleep(self.poll_interval)
        
        self.logger.info("position_watcher_stopped")

    async def stop(self):
        """Останавливает мониторинг."""
        self._running = False
        self.logger.info("position_watcher_stop_requested")

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def tracked_count(self) -> int:
        return len(self._tracked_orders)

    def get_tracked_orders(self) -> Dict[str, TrackedOrder]:
        return self._tracked_orders.copy()

    async def _check_orders(self):
        """Проверяет статус всех отслеживаемых заявок."""
        if not self._tracked_orders:
            return
        
        self.logger.debug("checking_orders", count=len(self._tracked_orders))
        
        # Импорт здесь чтобы избежать circular import
        from api.tinkoff_client import TinkoffClient
        
        try:
            async with TinkoffClient(self.config.tinkoff) as client:
                services = client._services
                response = await services.stop_orders.get_stop_orders(
                    account_id=self.config.tinkoff.account_id
                )
                
                current_orders = {
                    order.stop_order_id: order 
                    for order in response.stop_orders
                }
                
                for order_id, tracked in list(self._tracked_orders.items()):
                    # Проверяем kill switch перед каждой заявкой
                    if not await self._check_bot_active():
                        self.logger.info("check_orders_interrupted_inactive")
                        return
                    
                    try:
                        await self._process_order(client, order_id, tracked, current_orders)
                    except Exception as e:
                        self.logger.exception("process_order_error", 
                                            order_id=order_id, 
                                            error=str(e))
                        
        except Exception as e:
            self.logger.error("check_orders_api_error", error=str(e))

    async def _process_order(
        self, 
        client,
        order_id: str, 
        tracked: TrackedOrder, 
        current_orders: Dict
    ):
        """Обрабатывает одну заявку."""
        if order_id in self._executed_orders:
            return
        
        api_order = current_orders.get(order_id)
        
        if api_order is None:
            await self._handle_missing_order(client, tracked)
            return
        
        status = api_order.status.name
        
        if status == "STOP_ORDER_STATUS_EXECUTED":
            await self._handle_executed_order(client, tracked, api_order)
        elif status == "STOP_ORDER_STATUS_CANCELLED":
            await self._handle_cancelled_order(tracked)

    async def _handle_missing_order(self, client, tracked: TrackedOrder):
        """Обрабатывает исчезнувшую заявку."""
        self.logger.info("order_missing", order_id=tracked.order_id, ticker=tracked.ticker)
        
        # Проверяем позицию
        services = client._services
        portfolio = await services.operations.get_portfolio(
            account_id=self.config.tinkoff.account_id
        )
        
        has_position = False
        executed_price = 0
        
        for pos in portfolio.positions:
            if pos.figi == tracked.figi:
                from t_tech.invest.utils import quotation_to_decimal
                qty = float(quotation_to_decimal(pos.quantity))
                if qty > 0:
                    has_position = True
                    executed_price = float(quotation_to_decimal(pos.average_position_price))
                    break
        
        if has_position and tracked.order_type == OrderType.ENTRY_BUY:
            tracked.is_executed = True
            tracked.executed_price = executed_price
            tracked.executed_at = datetime.utcnow()
            self._executed_orders.add(tracked.order_id)
            
            # Обновляем в БД
            await self.repo.mark_order_executed(
                tracked.order_id,
                executed_price=executed_price,
                execution_reason="filled"
            )
            
            await self._on_entry_executed(client, tracked, executed_price)
        else:
            await self._handle_cancelled_order(tracked)

    async def _handle_executed_order(self, client, tracked: TrackedOrder, api_order):
        """Обрабатывает исполненную заявку."""
        from t_tech.invest.utils import quotation_to_decimal
        
        executed_price = float(quotation_to_decimal(api_order.stop_price))
        
        tracked.is_executed = True
        tracked.executed_price = executed_price
        tracked.executed_at = datetime.utcnow()
        self._executed_orders.add(tracked.order_id)
        
        self.logger.info("order_executed",
                        order_id=tracked.order_id,
                        ticker=tracked.ticker,
                        type=tracked.order_type.value,
                        price=executed_price)
        
        if tracked.order_type == OrderType.ENTRY_BUY:
            # Обновляем в БД
            await self.repo.mark_order_executed(
                tracked.order_id,
                executed_price=executed_price,
                execution_reason="filled"
            )
            await self._on_entry_executed(client, tracked, executed_price)
            
        elif tracked.order_type == OrderType.STOP_LOSS:
            pnl = self._calculate_pnl(tracked, executed_price)
            await self.repo.mark_order_executed(
                tracked.order_id,
                executed_price=executed_price,
                execution_reason="sl_triggered",
                pnl_rub=pnl["pnl_rub"],
                pnl_pct=pnl["pnl_pct"]
            )
            await self.repo.increment_stats(sl_triggered=1)
            await self._on_stop_loss_executed(tracked, executed_price)
            
        elif tracked.order_type == OrderType.TAKE_PROFIT:
            pnl = self._calculate_pnl(tracked, executed_price)
            await self.repo.mark_order_executed(
                tracked.order_id,
                executed_price=executed_price,
                execution_reason="tp_triggered",
                pnl_rub=pnl["pnl_rub"],
                pnl_pct=pnl["pnl_pct"]
            )
            await self.repo.increment_stats(tp_triggered=1)
            await self._on_take_profit_executed(tracked, executed_price)

    def _calculate_pnl(self, tracked: TrackedOrder, exit_price: float) -> Dict[str, float]:
        """Рассчитывает PnL."""
        pnl_per_share = exit_price - tracked.entry_price
        pnl_rub = pnl_per_share * tracked.quantity * tracked.lot_size
        pnl_pct = (pnl_per_share / tracked.entry_price * 100) if tracked.entry_price > 0 else 0
        return {"pnl_rub": pnl_rub, "pnl_pct": pnl_pct}

    async def _handle_cancelled_order(self, tracked: TrackedOrder):
        """Обрабатывает отменённую заявку."""
        self.logger.info("order_cancelled", order_id=tracked.order_id, ticker=tracked.ticker)
        
        await self.notifier.send_message(
            f"⚪ <b>Заявка отменена</b>\n"
            f"📌 {tracked.ticker}\n"
            f"📋 Тип: {tracked.order_type.value}"
        )
        
        await self.untrack_order(tracked.order_id, "cancelled_on_exchange")

    async def _on_entry_executed(self, client, tracked: TrackedOrder, executed_price: float):
        """
        Заявка на ВХОД исполнена.
        
        В режиме auto → выставляем SL и TP
        В режиме manual → только уведомляем
        """
        mode = await self._get_bot_mode()
        
        self.logger.info("entry_executed",
                        ticker=tracked.ticker,
                        price=executed_price,
                        mode=mode)
        
        # Рассчитываем SL и TP от реальной цены входа
        sl_price = executed_price - tracked.stop_offset
        tp_price = executed_price + tracked.take_offset
        
        sl_pct = (tracked.stop_offset / executed_price * 100) if executed_price > 0 else 0
        tp_pct = (tracked.take_offset / executed_price * 100) if executed_price > 0 else 0
        
        potential_loss = tracked.stop_offset * tracked.quantity * tracked.lot_size
        potential_profit = tracked.take_offset * tracked.quantity * tracked.lot_size
        
        # Уведомляем о входе
        await self.notifier.send_message(
            f"✅ <b>Позиция открыта!</b>\n"
            f"📌 {tracked.ticker}\n"
            f"💰 Цена входа: {executed_price:,.2f} ₽\n"
            f"📦 Кол-во: {tracked.quantity} лот(ов)\n\n"
            f"🛑 SL: {sl_price:,.2f} ₽ ({sl_pct:.2f}%)\n"
            f"🎯 TP: {tp_price:,.2f} ₽ ({tp_pct:.2f}%)\n\n"
            f"💸 Макс. убыток: {potential_loss:,.0f} ₽\n"
            f"💰 Потенц. прибыль: {potential_profit:,.0f} ₽"
        )
        
        # Если режим manual — не выставляем заявки
        if mode != "auto":
            await self.notifier.send_message(
                f"⚠️ <b>Режим: {mode.upper()}</b>\n"
                f"SL и TP НЕ выставлены автоматически.\n"
                f"Выставите вручную или переключите режим: /auto"
            )
            # Удаляем из отслеживания (позиция открыта, но без автоматики)
            if tracked.order_id in self._tracked_orders:
                del self._tracked_orders[tracked.order_id]
            return
        
        # Режим auto — выставляем SL и TP
        await self._place_sl_tp(client, tracked, executed_price, sl_price, tp_price)

    async def _place_sl_tp(
        self, 
        client, 
        tracked: TrackedOrder, 
        executed_price: float,
        sl_price: float,
        tp_price: float
    ):
        """Выставляет SL и TP заявки."""
        from decimal import Decimal
        from t_tech.invest.utils import decimal_to_quotation
        from t_tech.invest import (
            StopOrderDirection,
            StopOrderType,
            StopOrderExpirationType,
        )
        
        services = client._services
        sl_success = False
        tp_success = False
        
        # === STOP-LOSS ===
        try:
            sl_response = await services.stop_orders.post_stop_order(
                figi=tracked.figi,
                quantity=tracked.quantity,
                stop_price=decimal_to_quotation(Decimal(str(sl_price))),
                direction=StopOrderDirection.STOP_ORDER_DIRECTION_SELL,
                account_id=self.config.tinkoff.account_id,
                stop_order_type=StopOrderType.STOP_ORDER_TYPE_STOP_LOSS,
                expiration_type=StopOrderExpirationType.STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL,
            )
            
            tracked.sl_order_id = sl_response.stop_order_id
            sl_success = True
            
            # Сохраняем SL в отслеживание
            await self.track_order(
                order_id=sl_response.stop_order_id,
                ticker=tracked.ticker,
                figi=tracked.figi,
                order_type=OrderType.STOP_LOSS,
                quantity=tracked.quantity,
                entry_price=executed_price,
                stop_price=sl_price,
                target_price=tp_price,
                stop_offset=tracked.stop_offset,
                take_offset=tracked.take_offset,
                lot_size=tracked.lot_size,
                atr=tracked.atr,
                parent_order_id=tracked.order_id,
                created_by="auto",
            )
            
            await self.repo.increment_stats(orders_placed=1)
            
        except Exception as e:
            self.logger.exception("stop_loss_error", error=str(e))
            await self.notifier.send_error(f"SL не выставлен: {str(e)}", tracked.ticker)
        
        # === TAKE-PROFIT ===
        try:
            tp_response = await services.stop_orders.post_stop_order(
                figi=tracked.figi,
                quantity=tracked.quantity,
                stop_price=decimal_to_quotation(Decimal(str(tp_price))),
                direction=StopOrderDirection.STOP_ORDER_DIRECTION_SELL,
                account_id=self.config.tinkoff.account_id,
                stop_order_type=StopOrderType.STOP_ORDER_TYPE_TAKE_PROFIT,
                expiration_type=StopOrderExpirationType.STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL,
            )
            
            tracked.tp_order_id = tp_response.stop_order_id
            tp_success = True
            
            # Сохраняем TP в отслеживание
            await self.track_order(
                order_id=tp_response.stop_order_id,
                ticker=tracked.ticker,
                figi=tracked.figi,
                order_type=OrderType.TAKE_PROFIT,
                quantity=tracked.quantity,
                entry_price=executed_price,
                stop_price=sl_price,
                target_price=tp_price,
                stop_offset=tracked.stop_offset,
                take_offset=tracked.take_offset,
                lot_size=tracked.lot_size,
                atr=tracked.atr,
                parent_order_id=tracked.order_id,
                created_by="auto",
            )
            
            await self.repo.increment_stats(orders_placed=1)
            
        except Exception as e:
            self.logger.exception("take_profit_error", error=str(e))
            await self.notifier.send_error(f"TP не выставлен: {str(e)}", tracked.ticker)
        
        # Связываем заявки в БД
        if tracked.sl_order_id or tracked.tp_order_id:
            await self.repo.link_sl_tp_orders(
                tracked.order_id,
                sl_order_id=tracked.sl_order_id,
                tp_order_id=tracked.tp_order_id
            )
        
        # Итоговое уведомление
        if sl_success and tp_success:
            await self.notifier.send_message(
                f"🎯 <b>SL и TP выставлены!</b>\n"
                f"📌 {tracked.ticker}\n"
                f"🛑 SL: {sl_price:,.2f} ₽\n"
                f"🎯 TP: {tp_price:,.2f} ₽"
            )
        elif sl_success:
            await self.notifier.send_message(
                f"⚠️ <b>Только SL выставлен!</b>\n"
                f"📌 {tracked.ticker}\n"
                f"❌ TP НЕ выставлен!"
            )
        elif tp_success:
            await self.notifier.send_message(
                f"⚠️ <b>Только TP выставлен!</b>\n"
                f"📌 {tracked.ticker}\n"
                f"❌ SL НЕ выставлен! ОПАСНО!"
            )
        else:
            await self.notifier.send_message(
                f"❌ <b>SL и TP НЕ выставлены!</b>\n"
                f"📌 {tracked.ticker}\n"
                f"⚠️ Позиция БЕЗ ЗАЩИТЫ!"
            )
        
        # Удаляем entry из отслеживания
        if tracked.order_id in self._tracked_orders:
            del self._tracked_orders[tracked.order_id]

    async def _on_stop_loss_executed(self, tracked: TrackedOrder, executed_price: float):
        """Стоп-лосс сработал."""
        pnl = self._calculate_pnl(tracked, executed_price)
        
        await self.notifier.send_message(
            f"🛑 <b>СТОП-ЛОСС сработал!</b>\n"
            f"📌 {tracked.ticker}\n"
            f"💰 Вход: {tracked.entry_price:,.2f} ₽\n"
            f"📤 Выход: {executed_price:,.2f} ₽\n"
            f"📦 Кол-во: {tracked.quantity} лот(ов)\n"
            f"💸 P&L: {pnl['pnl_rub']:+,.0f} ₽ ({pnl['pnl_pct']:+.2f}%)"
        )
        
        await self._cancel_related_order(tracked, "tp")
        
        if tracked.order_id in self._tracked_orders:
            del self._tracked_orders[tracked.order_id]

    async def _on_take_profit_executed(self, tracked: TrackedOrder, executed_price: float):
        """Тейк-профит сработал."""
        pnl = self._calculate_pnl(tracked, executed_price)
        
        await self.notifier.send_message(
            f"🎯 <b>ТЕЙК-ПРОФИТ сработал!</b>\n"
            f"📌 {tracked.ticker}\n"
            f"💰 Вход: {tracked.entry_price:,.2f} ₽\n"
            f"📤 Выход: {executed_price:,.2f} ₽\n"
            f"📦 Кол-во: {tracked.quantity} лот(ов)\n"
            f"💰 P&L: {pnl['pnl_rub']:+,.0f} ₽ ({pnl['pnl_pct']:+.2f}%)"
        )
        
        await self._cancel_related_order(tracked, "sl")
        
        if tracked.order_id in self._tracked_orders:
            del self._tracked_orders[tracked.order_id]

    async def _cancel_related_order(self, tracked: TrackedOrder, order_type: str):
        """Отменяет связанную заявку (SL или TP)."""
        from api.tinkoff_client import TinkoffClient
        
        target_type = OrderType.TAKE_PROFIT if order_type == "tp" else OrderType.STOP_LOSS
        related_order_id = None
        
        for oid, order in list(self._tracked_orders.items()):
            if (order.ticker == tracked.ticker and 
                order.order_type == target_type and 
                not order.is_executed):
                related_order_id = oid
                break
        
        if not related_order_id:
            return
        
        try:
            async with TinkoffClient(self.config.tinkoff) as client:
                services = client._services
                await services.stop_orders.cancel_stop_order(
                    account_id=self.config.tinkoff.account_id,
                    stop_order_id=related_order_id
                )
                
                self.logger.info("related_order_cancelled", 
                               order_id=related_order_id, 
                               type=order_type)
                
                await self.untrack_order(related_order_id, "opposite_triggered")
                
                await self.notifier.send_message(
                    f"🗑 Связанная {order_type.upper()} заявка отменена"
                )
                
        except Exception as e:
            self.logger.exception("cancel_related_order_error", error=str(e))