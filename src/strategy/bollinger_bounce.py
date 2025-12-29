"""
Стратегия Bollinger Bounce - отскок от нижней линии Боллинджера.

Логика:
- Вход: цена касается нижней линии BB (или близко к ней)
- Выход: +0.5 ATR (тейк-профит) или -0.3 ATR (стоп-лосс)

Мат. ожидание:
- При TP = 0.5 ATR, SL = 0.3 ATR
- Соотношение Risk:Reward = 1:1.67
- Для безубытка нужен WinRate > 37.5%
"""
from datetime import datetime
from typing import Optional, Dict, Any, List

import structlog

from strategy.base import BaseStrategy, Signal, SignalType, AnalysisResult
from indicators.atr import calculate_atr_from_candles
from indicators.bollinger import calculate_bb_from_candles
from risk.position_sizer import calculate_position_size, calculate_take_profit, calculate_stop_loss

logger = structlog.get_logger()


class BollingerBounceStrategy(BaseStrategy):
    """
    Стратегия отскока от нижней линии Боллинджера.
    
    Параметры конфига:
    - bollinger_period: период BB (default: 20)
    - bollinger_std: множитель σ (default: 2.0)
    - atr_period: период ATR (default: 14)
    - entry_threshold_pct: порог входа в % от нижней линии (default: 1.0)
    - stop_loss_atr: множитель ATR для стопа (default: 0.3)
    - take_profit_atr: множитель ATR для тейка (default: 0.5)
    
    Example:
        >>> strategy = BollingerBounceStrategy({
        ...     "bollinger_period": 20,
        ...     "entry_threshold_pct": 1.0,
        ... })
        >>> result = strategy.analyze("SBER", "BBG004730N88", candles, 280.0)
        >>> if result.signal:
        ...     print(f"Signal: {result.signal.type.value}")
    """

    @property
    def name(self) -> str:
        return "bollinger_bounce"

    def __init__(self, config: Dict[str, Any] = None):
        super().__init__(config)
        
        # Параметры стратегии
        self.bollinger_period = self.config.get("bollinger_period", 20)
        self.bollinger_std = self.config.get("bollinger_std", 2.0)
        self.atr_period = self.config.get("atr_period", 14)
        self.entry_threshold_pct = self.config.get("entry_threshold_pct", 1.0)
        self.stop_loss_atr = self.config.get("stop_loss_atr", 0.3)
        self.take_profit_atr = self.config.get("take_profit_atr", 0.5)
        
        # Параметры риска
        self.deposit = self.config.get("deposit", 1_000_000)
        self.risk_per_trade = self.config.get("risk_per_trade", 0.01)
        self.max_position_pct = self.config.get("max_position_pct", 0.25)
        
        # Торговые часы
        self.trading_start_hour = self.config.get("trading_start_hour", 10)
        self.trading_end_hour = self.config.get("trading_end_hour", 19)

    def analyze(
        self,
        ticker: str,
        figi: str,
        candles: List[Dict[str, Any]],
        current_price: float,
        lot_size: int = 1,
        **kwargs
    ) -> Optional[AnalysisResult]:
        """
        Анализирует инструмент по стратегии Bollinger Bounce.
        
        Args:
            ticker: Тикер
            figi: FIGI
            candles: Свечи из API
            current_price: Текущая цена
            lot_size: Размер лота
        
        Returns:
            AnalysisResult с индикаторами и возможным сигналом
        """
        self.logger.info("analyzing", ticker=ticker)

        # Расчёт ATR
        atr_data = calculate_atr_from_candles(
            candles,
            period=self.atr_period,
            start_hour=self.trading_start_hour,
            end_hour=self.trading_end_hour,
        )

        if not atr_data:
            self.logger.warning("atr_calculation_failed", ticker=ticker)
            return None

        # Расчёт Bollinger Bands
        bb_data = calculate_bb_from_candles(
            candles,
            period=self.bollinger_period,
            std_multiplier=self.bollinger_std,
            start_hour=self.trading_start_hour,
            end_hour=self.trading_end_hour,
        )

        if not bb_data:
            self.logger.warning("bollinger_calculation_failed", ticker=ticker)
            return None

        # Расчёт размера позиции
        position = calculate_position_size(
            price=current_price,
            atr=atr_data["atr"],
            stop_loss_atr=self.stop_loss_atr,
            deposit=self.deposit,
            risk_pct=self.risk_per_trade,
            max_position_pct=self.max_position_pct,
            lot_size=lot_size,
        )

        # Расстояние до нижнего BB
        distance_to_bb_pct = bb_data.get("distance_to_lower_pct", 
                                         (current_price - bb_data["lower"]) / current_price * 100)

        # Создаём результат анализа
        result = AnalysisResult(
            ticker=ticker,
            figi=figi,
            price=current_price,
            atr=atr_data["atr"],
            atr_pct=atr_data["atr_pct"],
            bb_lower=bb_data["lower"],
            bb_middle=bb_data["middle"],
            bb_upper=bb_data["upper"],
            position_size=position["size_shares"],
            position_value=position["position_value"],
            stop_rub=position["stop_rub"],
            max_loss=position["max_loss_rub"],
            distance_to_bb_pct=distance_to_bb_pct,
            strategy_name=self.name,
        )

        # Проверка условия входа
        signal = self._check_entry_signal(
            ticker=ticker,
            figi=figi,
            price=current_price,
            bb_lower=bb_data["lower"],
            distance_to_bb_pct=distance_to_bb_pct,
            atr=atr_data["atr"],
            position_size=position["size_shares"],
        )

        if signal:
            result.signal = signal

        self.logger.info(
            "analysis_complete",
            ticker=ticker,
            price=current_price,
            atr=atr_data["atr"],
            bb_lower=bb_data["lower"],
            distance_to_bb=round(distance_to_bb_pct, 2),
            signal=signal.type.value if signal else None,
        )

        return result

    def _check_entry_signal(
        self,
        ticker: str,
        figi: str,
        price: float,
        bb_lower: float,
        distance_to_bb_pct: float,
        atr: float,
        position_size: int,
    ) -> Optional[Signal]:
        """
        Проверяет условие для сигнала на вход.
        
        Условие: цена близко к нижней линии BB (< entry_threshold_pct)
        """
        # Условие: цена в пределах threshold от нижней линии
        if distance_to_bb_pct <= self.entry_threshold_pct:
            
            # Рассчитываем цели
            target_price = calculate_take_profit(price, atr, self.take_profit_atr)
            stop_price = calculate_stop_loss(price, atr, self.stop_loss_atr)

            return Signal(
                type=SignalType.BUY,
                ticker=ticker,
                figi=figi,
                price=price,
                timestamp=datetime.utcnow(),
                target_price=target_price,
                stop_price=stop_price,
                position_size=position_size,
                strategy_name=self.name,
                reason=f"Цена {price:.2f} близко к BB lower {bb_lower:.2f} ({distance_to_bb_pct:.1f}%)",
                confidence=self._calculate_confidence(distance_to_bb_pct),
                indicators={
                    "bb_lower": bb_lower,
                    "atr": atr,
                    "distance_to_bb_pct": distance_to_bb_pct,
                },
            )

        return None

    def _calculate_confidence(self, distance_to_bb_pct: float) -> float:
        """
        Рассчитывает уверенность в сигнале.
        
        Чем ближе к BB, тем выше уверенность.
        """
        # 0% distance = 1.0 confidence
        # entry_threshold% distance = 0.5 confidence
        if distance_to_bb_pct <= 0:
            return 1.0
        elif distance_to_bb_pct >= self.entry_threshold_pct:
            return 0.5
        else:
            return 1.0 - (distance_to_bb_pct / self.entry_threshold_pct) * 0.5
