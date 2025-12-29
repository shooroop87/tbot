"""
Базовый класс для торговых стратегий.

Каждая стратегия должна:
1. Наследоваться от BaseStrategy
2. Реализовать метод analyze()
3. Возвращать Signal или None
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Dict, Any, List

import structlog

logger = structlog.get_logger()


class SignalType(Enum):
    """Типы сигналов."""
    BUY = "BUY"
    SELL = "SELL"
    CLOSE = "CLOSE"
    HOLD = "HOLD"


@dataclass
class Signal:
    """Торговый сигнал."""
    type: SignalType
    ticker: str
    figi: str
    price: float
    timestamp: datetime
    
    # Параметры сделки
    target_price: Optional[float] = None
    stop_price: Optional[float] = None
    position_size: Optional[int] = None
    
    # Метаданные
    strategy_name: str = ""
    reason: str = ""
    confidence: float = 1.0  # 0.0 - 1.0
    indicators: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Конвертирует сигнал в словарь."""
        return {
            "type": self.type.value,
            "ticker": self.ticker,
            "figi": self.figi,
            "price": self.price,
            "timestamp": self.timestamp.isoformat(),
            "target_price": self.target_price,
            "stop_price": self.stop_price,
            "position_size": self.position_size,
            "strategy_name": self.strategy_name,
            "reason": self.reason,
            "confidence": self.confidence,
            "indicators": self.indicators,
        }


@dataclass
class AnalysisResult:
    """Результат анализа инструмента."""
    ticker: str
    figi: str
    price: float
    
    # Индикаторы
    atr: Optional[float] = None
    atr_pct: Optional[float] = None
    bb_lower: Optional[float] = None
    bb_middle: Optional[float] = None
    bb_upper: Optional[float] = None
    
    # Позиция
    position_size: Optional[int] = None
    position_value: Optional[float] = None
    stop_rub: Optional[float] = None
    max_loss: Optional[float] = None
    
    # Сигнал
    signal: Optional[Signal] = None
    distance_to_bb_pct: Optional[float] = None
    
    # Метаданные
    strategy_name: str = ""
    analyzed_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> Dict[str, Any]:
        """Конвертирует результат в словарь."""
        result = {
            "ticker": self.ticker,
            "figi": self.figi,
            "price": self.price,
            "atr": self.atr,
            "atr_pct": self.atr_pct,
            "bb_lower": self.bb_lower,
            "bb_middle": self.bb_middle,
            "bb_upper": self.bb_upper,
            "position_size": self.position_size,
            "position_value": self.position_value,
            "stop_rub": self.stop_rub,
            "max_loss": self.max_loss,
            "distance_to_bb_pct": self.distance_to_bb_pct,
            "strategy_name": self.strategy_name,
            "analyzed_at": self.analyzed_at.isoformat(),
        }
        if self.signal:
            result["signal"] = self.signal.type.value
        return result


class BaseStrategy(ABC):
    """
    Абстрактный базовый класс для стратегий.
    
    Каждая стратегия должна реализовать:
    - name: уникальное имя стратегии
    - analyze(): анализ инструмента и генерация сигнала
    """
    
    def __init__(self, config: Dict[str, Any] = None):
        """
        Args:
            config: Конфигурация стратегии
        """
        self.config = config or {}
        self.logger = structlog.get_logger().bind(strategy=self.name)

    @property
    @abstractmethod
    def name(self) -> str:
        """Уникальное имя стратегии."""
        pass

    @abstractmethod
    def analyze(
        self,
        ticker: str,
        figi: str,
        candles: List[Dict[str, Any]],
        current_price: float,
        **kwargs
    ) -> Optional[AnalysisResult]:
        """
        Анализирует инструмент и возвращает результат.
        
        Args:
            ticker: Тикер инструмента
            figi: FIGI инструмента
            candles: Исторические свечи
            current_price: Текущая цена
            **kwargs: Дополнительные параметры
        
        Returns:
            AnalysisResult с сигналом или None
        """
        pass

    def should_enter(self, analysis: AnalysisResult) -> bool:
        """
        Проверяет, нужно ли входить в позицию.
        
        Args:
            analysis: Результат анализа
        
        Returns:
            True если нужно входить
        """
        if analysis.signal and analysis.signal.type == SignalType.BUY:
            return True
        return False

    def should_exit(self, analysis: AnalysisResult) -> bool:
        """
        Проверяет, нужно ли выходить из позиции.
        
        Args:
            analysis: Результат анализа
        
        Returns:
            True если нужно выходить
        """
        if analysis.signal and analysis.signal.type in (SignalType.SELL, SignalType.CLOSE):
            return True
        return False
