"""
Модели базы данных.

Таблицы:
- instruments: инструменты (акции, фьючерсы)
- daily_indicators: ежедневные индикаторы
- signals: торговые сигналы
- trades: завершённые сделки
- daily_stats: ежедневная статистика
"""
from datetime import datetime

from sqlalchemy import (
    Column, Integer, String, Float, DateTime, Boolean, Text, 
    ForeignKey, Index, UniqueConstraint
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class Instrument(Base):
    """Инструмент (акция/фьючерс)."""
    __tablename__ = "instruments"

    id = Column(Integer, primary_key=True)
    figi = Column(String(20), unique=True, nullable=False, index=True)
    ticker = Column(String(20), nullable=False, index=True)
    name = Column(String(200))
    instrument_type = Column(String(20))  # share, future
    lot_size = Column(Integer, default=1)
    min_price_increment = Column(Float)
    currency = Column(String(10), default="rub")

    # Метаданные ликвидности
    avg_volume_rub = Column(Float)
    spread_pct = Column(Float)
    is_liquid = Column(Boolean, default=False)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Связи
    indicators = relationship("DailyIndicator", back_populates="instrument", cascade="all, delete-orphan")
    signals = relationship("Signal", back_populates="instrument", cascade="all, delete-orphan")
    trades = relationship("Trade", back_populates="instrument", cascade="all, delete-orphan")


class DailyIndicator(Base):
    """Ежедневные индикаторы по инструменту."""
    __tablename__ = "daily_indicators"

    id = Column(Integer, primary_key=True)
    instrument_id = Column(Integer, ForeignKey("instruments.id"), nullable=False)
    date = Column(DateTime, nullable=False)

    # Цены
    last_price = Column(Float)

    # ATR
    atr = Column(Float)
    atr_pct = Column(Float)

    # Bollinger Bands
    bb_upper = Column(Float)
    bb_middle = Column(Float)
    bb_lower = Column(Float)
    bb_bandwidth = Column(Float)

    # Расчётные параметры позиции
    recommended_size = Column(Integer)
    stop_rub = Column(Float)
    max_loss_rub = Column(Float)

    created_at = Column(DateTime, default=datetime.utcnow)

    instrument = relationship("Instrument", back_populates="indicators")

    __table_args__ = (
        Index("ix_daily_indicators_date_instrument", "date", "instrument_id"),
        UniqueConstraint("instrument_id", "date", name="uq_instrument_date"),
    )


class Signal(Base):
    """Торговые сигналы."""
    __tablename__ = "signals"

    id = Column(Integer, primary_key=True)
    instrument_id = Column(Integer, ForeignKey("instruments.id"), nullable=False)

    signal_type = Column(String(10))  # BUY, SELL, CLOSE
    signal_time = Column(DateTime, nullable=False)
    price = Column(Float, nullable=False)

    # Параметры сигнала
    target_price = Column(Float)
    stop_price = Column(Float)
    position_size = Column(Integer)

    # Причина
    strategy = Column(String(50))  # bollinger_bounce, ema_crossover
    reason = Column(Text)
    confidence = Column(Float)

    # Статус
    is_executed = Column(Boolean, default=False)
    executed_at = Column(DateTime)
    executed_price = Column(Float)

    created_at = Column(DateTime, default=datetime.utcnow)

    instrument = relationship("Instrument", back_populates="signals")


class Trade(Base):
    """Завершённые сделки."""
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True)
    instrument_id = Column(Integer, ForeignKey("instruments.id"), nullable=False)

    # Вход
    entry_time = Column(DateTime, nullable=False)
    entry_price = Column(Float, nullable=False)
    entry_size = Column(Integer, nullable=False)
    entry_signal_id = Column(Integer, ForeignKey("signals.id"))

    # Выход
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

    instrument = relationship("Instrument", back_populates="trades")


class DailyStats(Base):
    """Ежедневная статистика торговли."""
    __tablename__ = "daily_stats"

    id = Column(Integer, primary_key=True)
    date = Column(DateTime, nullable=False, unique=True)

    # Общая статистика
    trades_count = Column(Integer, default=0)
    winning_trades = Column(Integer, default=0)
    losing_trades = Column(Integer, default=0)

    # P&L
    total_pnl_rub = Column(Float, default=0)
    total_pnl_pct = Column(Float, default=0)
    max_drawdown_rub = Column(Float, default=0)

    # Капитал
    start_balance = Column(Float)
    end_balance = Column(Float)

    created_at = Column(DateTime, default=datetime.utcnow)
