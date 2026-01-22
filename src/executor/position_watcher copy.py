"""
Мониторинг заявок и позиций.

Логика:
1. Следим за стоп-заявками (take-profit buy) каждые 5 сек
2. Когда заявка исполнена → выставляем SL и TP на продажу
3. Уведомляем в Telegram
4. Следим за SL/TP → уведомляем при срабатывании
"""
import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Any, Optional, Set
from enum import Enum

import structlog

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
    """Отслеживаемая заявка."""
    order_id: str
    ticker: str
    figi: str
    order_type: OrderType
    quantity: int  # в лотах
    
    # Цены
    entry_price: float      # Цена входа
    stop_price: float       # Цена стоп-лосса
    target_price: float     # Цена тейк-профита
    
    # Расчётные параметры (для SL/TP)
    stop_offset: float = 0
    take_offset: float = 0
    lot_size: int = 1
    atr: float = 0          # ATR для расчёта % 
    
    # Статус
    is_executed: bool = False
    executed_price: Optional[float] = None
    executed_at: Optional[datetime] = None
    
    # Связанные заявки (для entry → SL/TP)
    sl_order_id: Optional[str] = None
    tp_order_id: Optional[str] = None


class PositionWatcher:
    """
    Мониторинг заявок с автовыставлением SL/TP.
    
    Использование:
        watcher = PositionWatcher(config, notifier)
        
        # При выставлении заявки
        watcher.track_order(order_id, ticker, figi, ...)
        
        # Запуск мониторинга
        await watcher.start()
    """

    def __init__(
        self, 
        config: Config, 
        notifier: TelegramNotifier,
        poll_interval: float = 5.0
    ):
        self.config = config
        self.notifier = notifier
        self.poll_interval = poll_interval
        
        self._running = False
        self._tracked_orders: Dict[str, TrackedOrder] = {}
        self._executed_orders: Set[str] = set()  # Чтобы не обрабатывать повторно
        
        self.logger = logger.bind(component="position_watcher")

    def track_order(
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
    ):
        """Добавляет заявку в отслеживание."""
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
        )
        self._tracked_orders[order_id] = order
        self.logger.info("order_tracked", 
                        order_id=order_id, 
                        ticker=ticker, 
                        type=order_type.value,
                        entry=entry_price,
                        stop=stop_price,
                        target=target_price,
                        atr=atr)

    def untrack_order(self, order_id: str):
        """Удаляет заявку из отслеживания."""
        if order_id in self._tracked_orders:
            del self._tracked_orders[order_id]
            self.logger.info("order_untracked", order_id=order_id)

    async def start(self):
        """Запускает мониторинг с авто-рестартом."""
        self._running = True
        self.logger.info("position_watcher_started", interval=self.poll_interval)
        
        consecutive_errors = 0
        max_consecutive_errors = 5
        
        while self._running:
            try:
                await self._check_orders()
                consecutive_errors = 0  # Сброс при успехе
                
            except Exception as e:
                consecutive_errors += 1
                self.logger.exception("watcher_error", 
                                     error=str(e), 
                                     consecutive=consecutive_errors)
                
                # Уведомляем при ошибках
                if consecutive_errors == 1:
                    await self.notifier.send_message(
                        f"⚠️ <b>Watcher: ошибка</b>\n"
                        f"📛 {str(e)[:200]}\n"
                        f"🔄 Продолжаю работу..."
                    )
                elif consecutive_errors >= max_consecutive_errors:
                    await self.notifier.send_message(
                        f"🔴 <b>Watcher: {consecutive_errors} ошибок подряд!</b>\n"
                        f"📛 {str(e)[:200]}\n"
                        f"⏳ Пауза 60 сек, затем продолжу..."
                    )
                    await asyncio.sleep(60)  # Длинная пауза
                    consecutive_errors = 0
                    continue
            
            await asyncio.sleep(self.poll_interval)
        
        self.logger.info("position_watcher_loop_ended")

    async def stop(self):
        """Останавливает мониторинг."""
        self._running = False
        self.logger.info("position_watcher_stopped")

    def clear_tracked(self):
        """Очищает все отслеживаемые заявки."""
        count = len(self._tracked_orders)
        self._tracked_orders.clear()
        self._executed_orders.clear()
        self.logger.info("tracked_orders_cleared", count=count)
        return count

    @property
    def is_running(self) -> bool:
        """Возвращает статус работы."""
        return self._running

    async def _check_orders(self):
        """Проверяет статус всех отслеживаемых заявок."""
        if not self._tracked_orders:
            return
        
        self.logger.debug("checking_orders", count=len(self._tracked_orders))
        
        try:
            async with TinkoffClient(self.config.tinkoff) as client:
                # Получаем все стоп-заявки
                services = client._services
                response = await services.stop_orders.get_stop_orders(
                    account_id=self.config.tinkoff.account_id
                )
                
                # Словарь текущих заявок по order_id
                current_orders = {
                    order.stop_order_id: order 
                    for order in response.stop_orders
                }
                
                # Проверяем каждую отслеживаемую заявку
                for order_id, tracked in list(self._tracked_orders.items()):
                    try:
                        await self._process_order(client, order_id, tracked, current_orders)
                    except Exception as e:
                        # Ошибка одной заявки не должна ронять весь watcher!
                        self.logger.exception("process_order_error", 
                                            order_id=order_id, 
                                            ticker=tracked.ticker,
                                            error=str(e))
                        # Продолжаем с остальными заявками
                        continue
                        
        except Exception as e:
            # Ошибка подключения к API — логируем, но не падаем
            self.logger.error("check_orders_api_error", error=str(e))

    async def _process_order(
        self, 
        client: TinkoffClient,
        order_id: str, 
        tracked: TrackedOrder, 
        current_orders: Dict
    ):
        """Обрабатывает одну заявку."""
        # Пропускаем уже обработанные
        if order_id in self._executed_orders:
            return
        
        api_order = current_orders.get(order_id)
        
        if api_order is None:
            # Заявка исчезла из списка активных = исполнена или отменена
            # Проверяем через историю операций
            await self._handle_missing_order(client, tracked)
            return
        
        status = api_order.status.name
        
        if status == "STOP_ORDER_STATUS_EXECUTED":
            await self._handle_executed_order(client, tracked, api_order)
        elif status == "STOP_ORDER_STATUS_CANCELLED":
            await self._handle_cancelled_order(tracked)

    async def _handle_missing_order(self, client: TinkoffClient, tracked: TrackedOrder):
        """Обрабатывает исчезнувшую заявку (исполнена или отменена)."""
        self.logger.info("order_missing_from_active", 
                        order_id=tracked.order_id, 
                        ticker=tracked.ticker)
        
        # Проверяем позицию — если появилась, значит заявка исполнилась
        services = client._services
        portfolio = await services.operations.get_portfolio(
            account_id=self.config.tinkoff.account_id
        )
        
        has_position = False
        for pos in portfolio.positions:
            if pos.figi == tracked.figi:
                from t_tech.invest.utils import quotation_to_decimal
                qty = float(quotation_to_decimal(pos.quantity))
                if qty > 0:
                    has_position = True
                    executed_price = float(quotation_to_decimal(pos.average_position_price))
                    break
        
        if has_position and tracked.order_type == OrderType.ENTRY_BUY:
            # Заявка на вход исполнилась!
            tracked.is_executed = True
            tracked.executed_price = executed_price
            tracked.executed_at = datetime.utcnow()
            self._executed_orders.add(tracked.order_id)
            
            await self._on_entry_executed(client, tracked, executed_price)
        else:
            # Заявка отменена или не найдена
            await self._handle_cancelled_order(tracked)

    async def _handle_executed_order(
        self, 
        client: TinkoffClient, 
        tracked: TrackedOrder, 
        api_order
    ):
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
            await self._on_entry_executed(client, tracked, executed_price)
        elif tracked.order_type == OrderType.STOP_LOSS:
            await self._on_stop_loss_executed(tracked, executed_price)
        elif tracked.order_type == OrderType.TAKE_PROFIT:
            await self._on_take_profit_executed(tracked, executed_price)

    async def _handle_cancelled_order(self, tracked: TrackedOrder):
        """Обрабатывает отменённую заявку."""
        self.logger.info("order_cancelled", 
                        order_id=tracked.order_id, 
                        ticker=tracked.ticker)
        
        await self.notifier.send_message(
            f"⚪ <b>Заявка отменена</b>\n"
            f"📌 {tracked.ticker}\n"
            f"📋 Тип: {tracked.order_type.value}"
        )
        
        self.untrack_order(tracked.order_id)

    async def _on_entry_executed(
        self, 
        client: TinkoffClient, 
        tracked: TrackedOrder, 
        executed_price: float
    ):
        """
        Заявка на ВХОД исполнена → выставляем SL и TP.
        """
        self.logger.info("entry_executed_placing_sl_tp",
                        ticker=tracked.ticker,
                        executed_price=executed_price)
        
        # Рассчитываем SL и TP от реальной цены входа
        sl_price = executed_price - tracked.stop_offset
        tp_price = executed_price + tracked.take_offset
        
        # Проценты от цены входа
        sl_pct = (tracked.stop_offset / executed_price * 100) if executed_price > 0 else 0
        tp_pct = (tracked.take_offset / executed_price * 100) if executed_price > 0 else 0
        
        # Потенциальные убыток/прибыль
        potential_loss = tracked.stop_offset * tracked.quantity * tracked.lot_size
        potential_profit = tracked.take_offset * tracked.quantity * tracked.lot_size
        
        # Уведомляем о входе
        await self.notifier.send_message(
            f"✅ <b>Позиция открыта!</b>\n"
            f"📌 {tracked.ticker}\n"
            f"💰 Цена входа: {executed_price:,.2f} ₽\n"
            f"📦 Кол-во: {tracked.quantity} лот(ов)\n\n"
            f"⏳ Выставляю SL и TP..."
        )
        
        services = client._services
        from decimal import Decimal
        from t_tech.invest.utils import decimal_to_quotation
        from t_tech.invest import (
            StopOrderDirection,
            StopOrderType,
            StopOrderExpirationType,
        )
        
        sl_success = False
        tp_success = False
        
        # === STOP-LOSS на продажу ===
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
            
            # Добавляем SL в отслеживание
            self.track_order(
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
            )
            
            self.logger.info("stop_loss_placed", 
                           order_id=sl_response.stop_order_id,
                           price=sl_price)
            
        except Exception as e:
            self.logger.exception("stop_loss_error", error=str(e))
            await self.notifier.send_error(f"SL не выставлен: {str(e)}", tracked.ticker)
        
        # === TAKE-PROFIT на продажу ===
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
            
            # Добавляем TP в отслеживание
            self.track_order(
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
            )
            
            self.logger.info("take_profit_placed", 
                           order_id=tp_response.stop_order_id,
                           price=tp_price)
            
        except Exception as e:
            self.logger.exception("take_profit_error", error=str(e))
            await self.notifier.send_error(f"TP не выставлен: {str(e)}", tracked.ticker)
        
        # Итоговое уведомление
        if sl_success and tp_success:
            # ATR %
            sl_atr_pct = (tracked.stop_offset / tracked.atr * 100) if tracked.atr > 0 else 0
            tp_atr_pct = (tracked.take_offset / tracked.atr * 100) if tracked.atr > 0 else 0
            
            atr_line = f"📊 ATR: {tracked.atr:,.2f} ₽\n\n" if tracked.atr > 0 else "\n"
            sl_atr_info = f" = {sl_atr_pct:.0f}% ATR" if tracked.atr > 0 else ""
            tp_atr_info = f" = {tp_atr_pct:.0f}% ATR" if tracked.atr > 0 else ""
            
            rr_line = f"⚖️ R:R = 1:{potential_profit/potential_loss:.1f}" if potential_loss > 0 else ""
            
            await self.notifier.send_message(
                f"🎯 <b>SL и TP выставлены!</b>\n\n"
                f"📌 {tracked.ticker}\n"
                f"💰 Вход: {executed_price:,.2f} ₽\n"
                f"{atr_line}"
                f"🛑 <b>Стоп-лосс:</b> {sl_price:,.2f} ₽\n"
                f"   📉 -{tracked.stop_offset:,.2f} ₽ ({sl_pct:.2f}%{sl_atr_info})\n"
                f"   💸 Макс. убыток: {potential_loss:,.0f} ₽\n\n"
                f"🎯 <b>Тейк-профит:</b> {tp_price:,.2f} ₽\n"
                f"   📈 +{tracked.take_offset:,.2f} ₽ ({tp_pct:.2f}%{tp_atr_info})\n"
                f"   💰 Потенц. прибыль: {potential_profit:,.0f} ₽\n\n"
                f"📦 Кол-во: {tracked.quantity} лот(ов)\n"
                f"{rr_line}"
            )
        elif sl_success:
            await self.notifier.send_message(
                f"⚠️ <b>Только SL выставлен!</b>\n"
                f"📌 {tracked.ticker}\n"
                f"🛑 Стоп-лосс: {sl_price:,.2f} ₽\n"
                f"❌ Тейк-профит НЕ выставлен!"
            )
        elif tp_success:
            await self.notifier.send_message(
                f"⚠️ <b>Только TP выставлен!</b>\n"
                f"📌 {tracked.ticker}\n"
                f"🎯 Тейк-профит: {tp_price:,.2f} ₽\n"
                f"❌ Стоп-лосс НЕ выставлен! ОПАСНО!"
            )
        else:
            await self.notifier.send_message(
                f"❌ <b>SL и TP НЕ выставлены!</b>\n"
                f"📌 {tracked.ticker}\n"
                f"⚠️ Позиция БЕЗ ЗАЩИТЫ!"
            )
        
        # Удаляем entry из отслеживания (SL и TP уже добавлены)
        self.untrack_order(tracked.order_id)

    async def _on_stop_loss_executed(self, tracked: TrackedOrder, executed_price: float):
        """Стоп-лосс сработал → отменяем TP."""
        pnl = (executed_price - tracked.entry_price) * tracked.quantity * tracked.lot_size
        pnl_pct = (executed_price - tracked.entry_price) / tracked.entry_price * 100
        
        await self.notifier.send_message(
            f"🛑 <b>СТОП-ЛОСС сработал!</b>\n"
            f"📌 {tracked.ticker}\n"
            f"💰 Вход: {tracked.entry_price:,.2f} ₽\n"
            f"📤 Выход: {executed_price:,.2f} ₽\n"
            f"📦 Кол-во: {tracked.quantity} лот(ов)\n"
            f"💸 P&L: {pnl:+,.0f} ₽ ({pnl_pct:+.2f}%)"
        )
        
        # Отменяем связанную TP заявку
        await self._cancel_related_order(tracked, "tp")
        
        self.untrack_order(tracked.order_id)

    async def _on_take_profit_executed(self, tracked: TrackedOrder, executed_price: float):
        """Тейк-профит сработал → отменяем SL."""
        pnl = (executed_price - tracked.entry_price) * tracked.quantity * tracked.lot_size
        pnl_pct = (executed_price - tracked.entry_price) / tracked.entry_price * 100
        
        await self.notifier.send_message(
            f"🎯 <b>ТЕЙК-ПРОФИТ сработал!</b>\n"
            f"📌 {tracked.ticker}\n"
            f"💰 Вход: {tracked.entry_price:,.2f} ₽\n"
            f"📤 Выход: {executed_price:,.2f} ₽\n"
            f"📦 Кол-во: {tracked.quantity} лот(ов)\n"
            f"💰 P&L: {pnl:+,.0f} ₽ ({pnl_pct:+.2f}%)"
        )
        
        # Отменяем связанную SL заявку
        await self._cancel_related_order(tracked, "sl")
        
        self.untrack_order(tracked.order_id)

    async def _cancel_related_order(self, tracked: TrackedOrder, order_type: str):
        """
        Отменяет связанную заявку (SL или TP).
        
        Когда SL сработал → отменяем TP
        Когда TP сработал → отменяем SL
        """
        # Ищем связанную заявку по ticker и типу
        related_order_id = None
        target_type = OrderType.TAKE_PROFIT if order_type == "tp" else OrderType.STOP_LOSS
        
        for oid, order in list(self._tracked_orders.items()):
            if (order.ticker == tracked.ticker and 
                order.order_type == target_type and 
                not order.is_executed):
                related_order_id = oid
                break
        
        if not related_order_id:
            self.logger.debug("no_related_order_to_cancel", ticker=tracked.ticker, type=order_type)
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
                               type=order_type,
                               ticker=tracked.ticker)
                
                # Удаляем из отслеживания
                self.untrack_order(related_order_id)
                
                await self.notifier.send_message(
                    f"🗑 Связанная {order_type.upper()} заявка отменена"
                )
                
        except Exception as e:
            self.logger.exception("cancel_related_order_error", error=str(e))

    @property
    def tracked_count(self) -> int:
        """Количество отслеживаемых заявок."""
        return len(self._tracked_orders)

    def get_tracked_orders(self) -> Dict[str, TrackedOrder]:
        """Возвращает все отслеживаемые заявки."""
        return self._tracked_orders.copy()