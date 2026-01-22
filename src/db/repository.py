"""Репозиторий для работы с БД."""
from datetime import datetime, date, timedelta
from typing import Optional, List, Dict, Any

from sqlalchemy import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.dialects.postgresql import insert
import structlog

from db.models import (
    Base, Instrument, IndicatorDaily, Signal, Trade,
    BotSettings, TrackedOrderDB
)

logger = structlog.get_logger()


class Repository:
    """Асинхронный репозиторий для работы с PostgreSQL."""

    def __init__(self, database_url: str):
        self.engine = create_async_engine(database_url, echo=False)
        self.async_session = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )

    async def init_db(self):
        """Создаёт таблицы если не существуют."""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("database_initialized")

    async def close(self):
        """Закрывает подключение."""
        await self.engine.dispose()

    # ═══════════════════════════════════════════════════════════
    # Bot Settings (Kill Switch + Mode)
    # ═══════════════════════════════════════════════════════════

    async def get_bot_settings(self) -> BotSettings:
        """
        Получает настройки бота (singleton, id=1).
        Создаёт запись если не существует.
        """
        async with self.async_session() as session:
            result = await session.execute(
                select(BotSettings).where(BotSettings.id == 1)
            )
            settings = result.scalar_one_or_none()
            
            if not settings:
                # Создаём с безопасными дефолтами
                settings = BotSettings(
                    id=1,
                    is_active=False,  # Бот ВЫКЛЮЧЕН по умолчанию!
                    mode="manual",
                    last_change_reason="Initial setup",
                    last_change_by="system",
                    last_change_at=datetime.utcnow(),
                )
                session.add(settings)
                await session.commit()
                await session.refresh(settings)
                logger.info("bot_settings_created", is_active=False, mode="manual")
            
            return settings

    async def is_bot_active(self) -> bool:
        """Проверяет активен ли бот (kill switch)."""
        try:
            settings = await self.get_bot_settings()
            return settings.is_active
        except Exception as e:
            logger.error("is_bot_active_error", error=str(e))
            return False  # При ошибке — безопасное состояние

    async def get_bot_mode(self) -> str:
        """Возвращает режим бота: auto / manual / monitor_only."""
        try:
            settings = await self.get_bot_settings()
            return settings.mode
        except Exception as e:
            logger.error("get_bot_mode_error", error=str(e))
            return "manual"  # При ошибке — безопасный режим

    async def set_bot_active(
        self,
        is_active: bool,
        reason: str = "",
        changed_by: str = "system"
    ) -> BotSettings:
        """
        Включает/выключает бота (kill switch).
        
        Args:
            is_active: True = бот работает, False = бот остановлен
            reason: Причина изменения (для аудита)
            changed_by: Кто изменил (telegram user_id / system)
        """
        async with self.async_session() as session:
            result = await session.execute(
                select(BotSettings).where(BotSettings.id == 1)
            )
            settings = result.scalar_one_or_none()
            
            if not settings:
                settings = BotSettings(id=1)
                session.add(settings)
            
            settings.is_active = is_active
            settings.last_change_reason = reason[:200] if reason else ""
            settings.last_change_by = str(changed_by)[:50]
            settings.last_change_at = datetime.utcnow()
            
            await session.commit()
            await session.refresh(settings)
            
            logger.info("bot_active_changed",
                       is_active=is_active,
                       reason=reason,
                       by=changed_by)
            return settings

    async def set_bot_mode(
        self,
        mode: str,
        reason: str = "",
        changed_by: str = "system"
    ) -> BotSettings:
        """
        Устанавливает режим бота.
        
        Args:
            mode: auto / manual / monitor_only
            reason: Причина изменения
            changed_by: Кто изменил
        
        Raises:
            ValueError: Если mode невалидный
        """
        valid_modes = ("auto", "manual", "monitor_only")
        if mode not in valid_modes:
            raise ValueError(f"Invalid mode: {mode}. Valid: {valid_modes}")
        
        async with self.async_session() as session:
            result = await session.execute(
                select(BotSettings).where(BotSettings.id == 1)
            )
            settings = result.scalar_one_or_none()
            
            if not settings:
                settings = BotSettings(id=1)
                session.add(settings)
            
            settings.mode = mode
            settings.last_change_reason = reason[:200] if reason else ""
            settings.last_change_by = str(changed_by)[:50]
            settings.last_change_at = datetime.utcnow()
            
            await session.commit()
            await session.refresh(settings)
            
            logger.info("bot_mode_changed",
                       mode=mode,
                       reason=reason,
                       by=changed_by)
            return settings

    async def pause_bot_until(
        self,
        until: datetime,
        reason: str = "",
        changed_by: str = "system"
    ) -> BotSettings:
        """Приостанавливает бота до указанного времени."""
        async with self.async_session() as session:
            result = await session.execute(
                select(BotSettings).where(BotSettings.id == 1)
            )
            settings = result.scalar_one_or_none()
            
            if not settings:
                settings = BotSettings(id=1)
                session.add(settings)
            
            settings.is_active = False
            settings.pause_until = until
            settings.last_change_reason = reason[:200] if reason else f"Paused until {until}"
            settings.last_change_by = str(changed_by)[:50]
            settings.last_change_at = datetime.utcnow()
            
            await session.commit()
            await session.refresh(settings)
            
            logger.info("bot_paused_until", until=until.isoformat(), by=changed_by)
            return settings

    async def increment_bot_stats(
        self,
        orders: int = 0,
        sl_triggered: int = 0,
        tp_triggered: int = 0,
        pnl_rub: float = 0
    ):
        """
        Обновляет статистику бота.
        
        Args:
            orders: Добавить к счётчику заявок
            sl_triggered: Добавить к счётчику сработавших SL
            tp_triggered: Добавить к счётчику сработавших TP
            pnl_rub: Добавить к общему PnL
        """
        async with self.async_session() as session:
            result = await session.execute(
                select(BotSettings).where(BotSettings.id == 1)
            )
            settings = result.scalar_one_or_none()
            
            if settings:
                if orders:
                    settings.total_orders = (settings.total_orders or 0) + orders
                if sl_triggered:
                    settings.total_sl_triggered = (settings.total_sl_triggered or 0) + sl_triggered
                if tp_triggered:
                    settings.total_tp_triggered = (settings.total_tp_triggered or 0) + tp_triggered
                if pnl_rub:
                    settings.total_pnl_rub = (settings.total_pnl_rub or 0) + pnl_rub
                
                await session.commit()
                logger.debug("bot_stats_updated",
                           orders=orders,
                           sl=sl_triggered,
                           tp=tp_triggered,
                           pnl=pnl_rub)

    async def get_bot_stats(self) -> Dict[str, Any]:
        """Возвращает статистику бота."""
        settings = await self.get_bot_settings()
        total_trades = (settings.total_sl_triggered or 0) + (settings.total_tp_triggered or 0)
        return {
            "total_orders": settings.total_orders or 0,
            "total_sl_triggered": settings.total_sl_triggered or 0,
            "total_tp_triggered": settings.total_tp_triggered or 0,
            "total_pnl_rub": settings.total_pnl_rub or 0,
            "win_rate": (
                (settings.total_tp_triggered or 0) / total_trades * 100
                if total_trades > 0
                else 0
            ),
        }

    # ═══════════════════════════════════════════════════════════
    # Tracked Orders (Persistence)
    # ═══════════════════════════════════════════════════════════

    async def save_tracked_order(self, data: Dict[str, Any]) -> TrackedOrderDB:
        """
        Сохраняет заявку в БД для персистентности.
        
        Args:
            data: Словарь с полями TrackedOrderDB
        """
        async with self.async_session() as session:
            order = TrackedOrderDB(**data)
            session.add(order)
            await session.commit()
            await session.refresh(order)
            
            logger.info("tracked_order_saved",
                       order_id=order.order_id,
                       ticker=order.ticker,
                       order_type=order.order_type)
            return order

    async def get_tracked_order(self, order_id: str) -> Optional[TrackedOrderDB]:
        """Получает заявку по order_id."""
        async with self.async_session() as session:
            result = await session.execute(
                select(TrackedOrderDB).where(TrackedOrderDB.order_id == order_id)
            )
            return result.scalar_one_or_none()

    async def get_pending_orders(self) -> List[TrackedOrderDB]:
        """
        Получает все pending заявки.
        Используется при старте бота для восстановления состояния.
        """
        async with self.async_session() as session:
            result = await session.execute(
                select(TrackedOrderDB)
                .where(TrackedOrderDB.status == "pending")
                .order_by(TrackedOrderDB.created_at)
            )
            orders = list(result.scalars().all())
            logger.info("pending_orders_fetched", count=len(orders))
            return orders

    async def get_orders_by_ticker(self, ticker: str) -> List[TrackedOrderDB]:
        """Получает все заявки по тикеру."""
        async with self.async_session() as session:
            result = await session.execute(
                select(TrackedOrderDB)
                .where(TrackedOrderDB.ticker == ticker)
                .order_by(TrackedOrderDB.created_at.desc())
            )
            return list(result.scalars().all())

    async def get_orders_by_status(self, status: str) -> List[TrackedOrderDB]:
        """Получает заявки по статусу: pending / executed / cancelled."""
        async with self.async_session() as session:
            result = await session.execute(
                select(TrackedOrderDB)
                .where(TrackedOrderDB.status == status)
                .order_by(TrackedOrderDB.created_at.desc())
            )
            return list(result.scalars().all())

    async def update_tracked_order(self, order_id: str, data: Dict[str, Any]) -> bool:
        """
        Обновляет поля заявки.
        
        Args:
            order_id: ID заявки
            data: Поля для обновления
        
        Returns:
            True если заявка найдена и обновлена
        """
        async with self.async_session() as session:
            result = await session.execute(
                select(TrackedOrderDB).where(TrackedOrderDB.order_id == order_id)
            )
            order = result.scalar_one_or_none()
            
            if not order:
                logger.warning("update_tracked_order_not_found", order_id=order_id)
                return False
            
            for key, value in data.items():
                if hasattr(order, key):
                    setattr(order, key, value)
            
            order.updated_at = datetime.utcnow()
            await session.commit()
            
            logger.debug("tracked_order_updated", order_id=order_id, fields=list(data.keys()))
            return True

    async def mark_order_executed(
        self,
        order_id: str,
        executed_price: float,
        pnl_rub: float = None,
        pnl_pct: float = None
    ) -> bool:
        """
        Помечает заявку как исполненную.
        
        Args:
            order_id: ID заявки
            executed_price: Цена исполнения
            pnl_rub: P&L в рублях (для SL/TP)
            pnl_pct: P&L в процентах
        """
        return await self.update_tracked_order(order_id, {
            "status": "executed",
            "is_executed": True,
            "executed_price": executed_price,
            "executed_at": datetime.utcnow(),
            "pnl_rub": pnl_rub,
            "pnl_pct": pnl_pct,
        })

    async def mark_order_cancelled(self, order_id: str, reason: str = "") -> bool:
        """Помечает заявку как отменённую."""
        data = {"status": "cancelled"}
        if reason:
            data["cancel_reason"] = reason
        return await self.update_tracked_order(order_id, data)

    async def link_sl_tp_orders(
        self,
        entry_order_id: str,
        sl_order_id: str = None,
        tp_order_id: str = None
    ) -> bool:
        """
        Связывает entry заявку с SL и TP.
        
        Args:
            entry_order_id: ID entry заявки
            sl_order_id: ID стоп-лосс заявки
            tp_order_id: ID тейк-профит заявки
        """
        data = {}
        if sl_order_id:
            data["sl_order_id"] = sl_order_id
        if tp_order_id:
            data["tp_order_id"] = tp_order_id
        
        if data:
            return await self.update_tracked_order(entry_order_id, data)
        return False

    async def get_order_stats(self) -> Dict[str, Any]:
        """Возвращает статистику по заявкам."""
        async with self.async_session() as session:
            # Подсчёт по статусам
            result = await session.execute(
                select(
                    TrackedOrderDB.status,
                    func.count(TrackedOrderDB.id).label("count")
                )
                .group_by(TrackedOrderDB.status)
            )
            status_counts = {row.status: row.count for row in result}
            
            # Подсчёт по типам
            result = await session.execute(
                select(
                    TrackedOrderDB.order_type,
                    func.count(TrackedOrderDB.id).label("count")
                )
                .group_by(TrackedOrderDB.order_type)
            )
            type_counts = {row.order_type: row.count for row in result}
            
            # Общий PnL
            result = await session.execute(
                select(func.sum(TrackedOrderDB.pnl_rub))
                .where(TrackedOrderDB.status == "executed")
            )
            total_pnl = result.scalar() or 0
            
            return {
                "by_status": status_counts,
                "by_type": type_counts,
                "total_pnl_rub": total_pnl,
                "pending": status_counts.get("pending", 0),
                "executed": status_counts.get("executed", 0),
                "cancelled": status_counts.get("cancelled", 0),
            }

    async def cleanup_old_orders(self, days: int = 30) -> int:
        """
        Удаляет старые исполненные/отменённые заявки.
        
        Args:
            days: Удалять заявки старше N дней
        
        Returns:
            Количество удалённых записей
        """
        async with self.async_session() as session:
            cutoff = datetime.utcnow() - timedelta(days=days)
            
            result = await session.execute(
                select(TrackedOrderDB)
                .where(
                    and_(
                        TrackedOrderDB.status.in_(["executed", "cancelled"]),
                        TrackedOrderDB.updated_at < cutoff
                    )
                )
            )
            orders = result.scalars().all()
            
            count = 0
            for order in orders:
                await session.delete(order)
                count += 1
            
            await session.commit()
            logger.info("old_orders_cleaned", count=count, days=days)
            return count

    # ═══════════════════════════════════════════════════════════
    # Инструменты
    # ═══════════════════════════════════════════════════════════

    async def upsert_instrument(self, data: Dict[str, Any]) -> Instrument:
        """Создаёт или обновляет инструмент."""
        async with self.async_session() as session:
            stmt = insert(Instrument).values(**data)
            stmt = stmt.on_conflict_do_update(
                index_elements=["figi"],
                set_={k: v for k, v in data.items() if k != "figi"}
            )
            await session.execute(stmt)
            await session.commit()
            result = await session.execute(
                select(Instrument).where(Instrument.figi == data["figi"])
            )
            return result.scalar_one()

    async def get_instrument_by_figi(self, figi: str) -> Optional[Instrument]:
        """Получает инструмент по FIGI."""
        async with self.async_session() as session:
            result = await session.execute(
                select(Instrument).where(Instrument.figi == figi)
            )
            return result.scalar_one_or_none()

    async def get_instrument_by_ticker(self, ticker: str) -> Optional[Instrument]:
        """Получает инструмент по тикеру."""
        async with self.async_session() as session:
            result = await session.execute(
                select(Instrument).where(Instrument.ticker == ticker)
            )
            return result.scalar_one_or_none()

    async def get_liquid_instruments(self) -> List[Instrument]:
        """Получает все ликвидные инструменты."""
        async with self.async_session() as session:
            result = await session.execute(
                select(Instrument)
                .where(Instrument.is_liquid == True)
                .order_by(Instrument.avg_volume_rub.desc())
            )
            return list(result.scalars().all())

    # ═══════════════════════════════════════════════════════════
    # Индикаторы
    # ═══════════════════════════════════════════════════════════

    async def save_indicator_daily(
        self,
        instrument_id: int,
        calc_date: date,
        data: Dict[str, Any]
    ) -> IndicatorDaily:
        """
        Сохраняет или обновляет дневные индикаторы.
        
        Args:
            instrument_id: ID инструмента
            calc_date: Дата расчёта
            data: Словарь с индикаторами
        """
        async with self.async_session() as session:
            # Формируем данные для вставки
            indicator_data = {
                "instrument_id": instrument_id,
                "date": calc_date,
                "close": data.get("close"),
                "atr": data.get("atr"),
                "atr_pct": data.get("atr_pct"),
                "bb_upper": data.get("bb_upper"),
                "bb_middle": data.get("bb_middle"),
                "bb_lower": data.get("bb_lower"),
                "distance_to_bb_lower_pct": data.get("distance_to_bb_pct"),
                # EMA (13/26)
                "ema_13": data.get("ema_13"),
                "ema_26": data.get("ema_26"),
                "ema_trend": data.get("ema_trend"),
                "ema_diff_pct": data.get("ema_diff_pct"),
                "ema_13_slope": data.get("ema_13_slope"),
                "ema_26_slope": data.get("ema_26_slope"),
                "distance_to_ema_13_pct": data.get("distance_to_ema_13_pct"),
                "distance_to_ema_26_pct": data.get("distance_to_ema_26_pct"),
                # Ликвидность
                "volume_rub": data.get("volume_rub"),
                "spread_pct": data.get("spread_pct"),
            }
            
            # Upsert: вставка или обновление по (instrument_id, date)
            stmt = insert(IndicatorDaily).values(**indicator_data)
            stmt = stmt.on_conflict_do_update(
                constraint="uq_indicator_instrument_date",
                set_={k: v for k, v in indicator_data.items()
                      if k not in ("instrument_id", "date")}
            )
            await session.execute(stmt)
            await session.commit()
            
            # Возвращаем сохранённую запись
            result = await session.execute(
                select(IndicatorDaily).where(
                    and_(
                        IndicatorDaily.instrument_id == instrument_id,
                        IndicatorDaily.date == calc_date
                    )
                )
            )
            indicator = result.scalar_one()
            
            logger.info("indicator_saved",
                       instrument_id=instrument_id,
                       date=str(calc_date),
                       ema_13=data.get("ema_13"),
                       ema_26=data.get("ema_26"),
                       ema_trend=data.get("ema_trend"))
            
            return indicator

    async def get_indicators_by_date(self, calc_date: date) -> List[IndicatorDaily]:
        """Получает все индикаторы за дату."""
        async with self.async_session() as session:
            result = await session.execute(
                select(IndicatorDaily)
                .where(IndicatorDaily.date == calc_date)
                .order_by(IndicatorDaily.instrument_id)
            )
            return list(result.scalars().all())

    async def get_indicators_history(
        self,
        instrument_id: int,
        days: int = 30
    ) -> List[IndicatorDaily]:
        """Получает историю индикаторов за N дней."""
        async with self.async_session() as session:
            result = await session.execute(
                select(IndicatorDaily)
                .where(IndicatorDaily.instrument_id == instrument_id)
                .order_by(IndicatorDaily.date.desc())
                .limit(days)
            )
            return list(result.scalars().all())

    # ═══════════════════════════════════════════════════════════
    # Сигналы
    # ═══════════════════════════════════════════════════════════

    async def save_signal(self, data: Dict[str, Any]) -> Signal:
        """Сохраняет сигнал."""
        async with self.async_session() as session:
            signal = Signal(**data)
            session.add(signal)
            await session.commit()
            await session.refresh(signal)
            logger.info("signal_saved", signal_id=signal.id, type=signal.signal_type)
            return signal

    async def get_today_signals(self) -> List[Signal]:
        """Получает сигналы за сегодня."""
        async with self.async_session() as session:
            today = date.today()
            result = await session.execute(
                select(Signal)
                .where(func.date(Signal.signal_time) == today)
                .order_by(Signal.signal_time.desc())
            )
            return list(result.scalars().all())

    # ═══════════════════════════════════════════════════════════
    # Сделки
    # ═══════════════════════════════════════════════════════════

    async def save_trade(self, data: Dict[str, Any]) -> Trade:
        """Сохраняет сделку."""
        async with self.async_session() as session:
            trade = Trade(**data)
            session.add(trade)
            await session.commit()
            await session.refresh(trade)
            logger.info("trade_saved", trade_id=trade.id, pnl=trade.pnl_rub)
            return trade