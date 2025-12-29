"""
Репозиторий для работы с БД.

Паттерн Repository для изоляции логики доступа к данным.
"""
from datetime import datetime, date
from typing import Optional, List, Dict, Any

from sqlalchemy import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.dialects.postgresql import insert
import structlog

from db.models import Base, Instrument, DailyIndicator, Signal, Trade, DailyStats

logger = structlog.get_logger()


class Repository:
    """Асинхронный репозиторий для работы с PostgreSQL."""

    def __init__(self, database_url: str):
        """
        Args:
            database_url: URL подключения (postgresql+asyncpg://...)
        """
        self.engine = create_async_engine(database_url, echo=False)
        self.async_session = async_sessionmaker(
            self.engine, 
            class_=AsyncSession, 
            expire_on_commit=False
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
    # Instruments
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

            # Получаем обновлённую запись
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
    # Daily Indicators
    # ═══════════════════════════════════════════════════════════

    async def save_daily_indicator(self, data: Dict[str, Any]) -> DailyIndicator:
        """Сохраняет дневные индикаторы (upsert по instrument_id + date)."""
        async with self.async_session() as session:
            # Проверяем существует ли запись
            existing = await session.execute(
                select(DailyIndicator).where(
                    and_(
                        DailyIndicator.instrument_id == data["instrument_id"],
                        func.date(DailyIndicator.date) == func.date(data["date"])
                    )
                )
            )
            indicator = existing.scalar_one_or_none()
            
            if indicator:
                # Обновляем
                for key, value in data.items():
                    setattr(indicator, key, value)
            else:
                # Создаём
                indicator = DailyIndicator(**data)
                session.add(indicator)
            
            await session.commit()
            await session.refresh(indicator)
            return indicator

    async def get_latest_indicator(self, instrument_id: int) -> Optional[DailyIndicator]:
        """Получает последние индикаторы по инструменту."""
        async with self.async_session() as session:
            result = await session.execute(
                select(DailyIndicator)
                .where(DailyIndicator.instrument_id == instrument_id)
                .order_by(DailyIndicator.date.desc())
                .limit(1)
            )
            return result.scalar_one_or_none()

    async def get_indicators_for_date(self, target_date: date) -> List[DailyIndicator]:
        """Получает индикаторы за дату."""
        async with self.async_session() as session:
            result = await session.execute(
                select(DailyIndicator)
                .where(func.date(DailyIndicator.date) == target_date)
            )
            return list(result.scalars().all())

    # ═══════════════════════════════════════════════════════════
    # Signals
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

    async def get_pending_signals(self, instrument_id: Optional[int] = None) -> List[Signal]:
        """Получает неисполненные сигналы."""
        async with self.async_session() as session:
            query = select(Signal).where(Signal.is_executed == False)
            if instrument_id:
                query = query.where(Signal.instrument_id == instrument_id)
            result = await session.execute(query)
            return list(result.scalars().all())

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

    async def mark_signal_executed(
        self,
        signal_id: int,
        executed_price: float,
        executed_at: Optional[datetime] = None
    ):
        """Отмечает сигнал как исполненный."""
        async with self.async_session() as session:
            result = await session.execute(
                select(Signal).where(Signal.id == signal_id)
            )
            signal = result.scalar_one_or_none()
            if signal:
                signal.is_executed = True
                signal.executed_price = executed_price
                signal.executed_at = executed_at or datetime.utcnow()
                await session.commit()

    # ═══════════════════════════════════════════════════════════
    # Trades
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

    async def get_today_trades(self) -> List[Trade]:
        """Получает сделки за сегодня."""
        async with self.async_session() as session:
            today = date.today()
            result = await session.execute(
                select(Trade)
                .where(func.date(Trade.entry_time) == today)
            )
            return list(result.scalars().all())

    async def get_today_pnl(self) -> float:
        """Считает P&L за сегодня."""
        trades = await self.get_today_trades()
        return sum(t.pnl_rub or 0 for t in trades)

    # ═══════════════════════════════════════════════════════════
    # Daily Stats
    # ═══════════════════════════════════════════════════════════

    async def update_daily_stats(self, stats_data: Dict[str, Any]):
        """Обновляет дневную статистику."""
        async with self.async_session() as session:
            stmt = insert(DailyStats).values(**stats_data)
            stmt = stmt.on_conflict_do_update(
                index_elements=["date"],
                set_={k: v for k, v in stats_data.items() if k != "date"}
            )
            await session.execute(stmt)
            await session.commit()
