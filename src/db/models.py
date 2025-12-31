"""
Модели базы данных.

Таблицы:
- instruments: справочник инструментов (акции, фьючерсы)
- candles_daily: дневные свечи
- indicators_daily: ежедневные индикаторы (ATR, BB, etc)
- signals: торговые сигналы
- trades: завершённые сделки
"""
from datetime import datetime, date

from sqlalchemy import (
    Column, Integer, String, Float, DateTime, Date, Boolean, Text,
    ForeignKey, Index, UniqueConstraint, JSON
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class Instrument(Base):
    """Справочник инструментов (акции/фьючерсы)."""
    __tablename__ = "instruments"

    id = Column(Integer, primary_key=True)
    figi = Column(String(20), unique=True, nullable=False, index=True)
    ticker = Column(String(20), nullable=False, index=True)
    name = Column(String(200))
    instrument_type = Column(String(20))  # share, future
    currency = Column(String(10), default="rub")
    exchange = Column(String(50))
    lot_size = Column(Integer, default=1)
    min_price_increment = Column(Float)
    
    # Для фьючерсов
    expiration_date = Column(Date)
    basic_asset = Column(String(20))  # USD, RTS, etc
    
    # Статус
    is_active = Column(Boolean, default=True)
    is_liquid = Column(Boolean, default=False)
    
    # Метаданные ликвидности (обновляются ежедневно)
    avg_volume_rub = Column(Float)
    avg_spread_pct = Column(Float)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Связи
    candles = relationship("CandleDaily", back_populates="instrument", cascade="all, delete-orphan")
    indicators = relationship("IndicatorDaily", back_populates="instrument", cascade="all, delete-orphan")
    signals = relationship("Signal", back_populates="instrument", cascade="all, delete-orphan")


class CandleDaily(Base):
    """Дневные свечи (агрегированные из часовых 10-19 МСК)."""
    __tablename__ = "candles_daily"

    id = Column(Integer, primary_key=True)
    instrument_id = Column(Integer, ForeignKey("instruments.id"), nullable=False)
    date = Column(Date, nullable=False)
    
    open = Column(Float, nullable=False)
    high = Column(Float, nullable=False)
    low = Column(Float, nullable=False)
    close = Column(Float, nullable=False)
    volume = Column(Float)
    
    # Источник данных
    source = Column(String(20), default="api_hourly")  # api_hourly, api_daily
    
    created_at = Column(DateTime, default=datetime.utcnow)

    instrument = relationship("Instrument", back_populates="candles")

    __table_args__ = (
        UniqueConstraint("instrument_id", "date", name="uq_candle_instrument_date"),
        Index("ix_candles_date", "date"),
    )


class IndicatorDaily(Base):
    """
    Ежедневные индикаторы по инструменту.
    
    Хранит предрассчитанные значения индикаторов.
    Можно добавлять новые столбцы или использовать JSON поле extra.
    """
    __tablename__ = "indicators_daily"

    id = Column(Integer, primary_key=True)
    instrument_id = Column(Integer, ForeignKey("instruments.id"), nullable=False)
    date = Column(Date, nullable=False)

    # Цена закрытия
    close = Column(Float)

    # ATR (14)
    atr = Column(Float)
    atr_pct = Column(Float)  # ATR в % от цены

    # Bollinger Bands (20, 2)
    bb_upper = Column(Float)
    bb_middle = Column(Float)
    bb_lower = Column(Float)
    bb_bandwidth = Column(Float)  # (upper - lower) / middle * 100
    
    # Позиция цены относительно BB
    bb_position_pct = Column(Float)  # 0 = на lower, 100 = на upper
    distance_to_bb_lower_pct = Column(Float)  # расстояние до нижней в %

    # Ликвидность за день
    volume_rub = Column(Float)
    spread_pct = Column(Float)

    # Расширяемое поле для новых индикаторов
    extra = Column(JSON, default={})
    
    created_at = Column(DateTime, default=datetime.utcnow)

    instrument = relationship("Instrument", back_populates="indicators")

    __table_args__ = (
        UniqueConstraint("instrument_id", "date", name="uq_indicator_instrument_date"),
        Index("ix_indicators_date", "date"),
        Index("ix_indicators_instrument_date", "instrument_id", "date"),
    )


class Signal(Base):
    """Торговые сигналы."""
    __tablename__ = "signals"

    id = Column(Integer, primary_key=True)
    instrument_id = Column(Integer, ForeignKey("instruments.id"), nullable=False)

    signal_type = Column(String(10))  # BUY, SELL, CLOSE
    signal_date = Column(Date, nullable=False)
    signal_time = Column(DateTime)
    
    # Цены
    price = Column(Float, nullable=False)
    target_price = Column(Float)
    stop_price = Column(Float)
    
    # Параметры позиции
    position_size = Column(Integer)
    position_value = Column(Float)
    max_loss = Column(Float)
    
    # Причина и стратегия
    strategy = Column(String(50))  # bollinger_bounce
    reason = Column(Text)
    confidence = Column(Float)

    # Индикаторы на момент сигнала (для анализа)
    indicators_snapshot = Column(JSON)

    # Статус исполнения
    is_executed = Column(Boolean, default=False)
    executed_at = Column(DateTime)
    executed_price = Column(Float)

    created_at = Column(DateTime, default=datetime.utcnow)

    instrument = relationship("Instrument", back_populates="signals")

    __table_args__ = (
        Index("ix_signals_date", "signal_date"),
        Index("ix_signals_instrument", "instrument_id"),
    )


class Trade(Base):
    """Завершённые сделки."""
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True)
    instrument_id = Column(Integer, ForeignKey("instruments.id"), nullable=False)
    signal_id = Column(Integer, ForeignKey("signals.id"))

    # Вход
    entry_date = Column(Date, nullable=False)
    entry_time = Column(DateTime)
    entry_price = Column(Float, nullable=False)
    entry_size = Column(Integer, nullable=False)

    # Выход
    exit_date = Column(Date)
    exit_time = Column(DateTime)
    exit_price = Column(Float)
    exit_reason = Column(String(50))  # take_profit, stop_loss, manual, timeout

    # Результат
    pnl_rub = Column(Float)
    pnl_pct = Column(Float)
    commission_rub = Column(Float, default=0)

    # Метаданные
    strategy = Column(String(50))
    notes = Column(Text)

    created_at = Column(DateTime, default=datetime.utcnow)

    instrument = relationship("Instrument")
    signal = relationship("Signal")