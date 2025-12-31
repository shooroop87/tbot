"""Репозиторий для работы с БД."""
from datetime import datetime, date
from typing import Optional, List, Dict, Any

from sqlalchemy import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.dialects.postgresql import insert
import structlog

from db.models import Base, Instrument, Signal, Trade

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

    async def get_liquid_instruments(self) -> List[Instrument]:
        """Получает все ликвидные инструменты."""
        async with self.async_session() as session:
            result = await session.execute(
                select(Instrument)
                .where(Instrument.is_liquid == True)
                .order_by(Instrument.avg_volume_rub.desc())
            )
            return list(result.scalars().all())

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

    async def save_trade(self, data: Dict[str, Any]) -> Trade:
        """Сохраняет сделку."""
        async with self.async_session() as session:
            trade = Trade(**data)
            session.add(trade)
            await session.commit()
            await session.refresh(trade)
            logger.info("trade_saved", trade_id=trade.id, pnl=trade.pnl_rub)
            return trade