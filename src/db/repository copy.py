"""Репозиторий для работы с БД."""
from datetime import datetime, date
from typing import Optional, List, Dict, Any

from sqlalchemy import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.dialects.postgresql import insert
import structlog

from db.models import Base, Instrument, IndicatorDaily, Signal, Trade

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
