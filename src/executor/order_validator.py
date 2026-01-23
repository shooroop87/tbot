"""
Валидатор заявок для свободного трейдинга.

Расположение: src/executor/order_validator.py

Проверки:
1. Цена входа ±N% от текущей рыночной цены
2. Размер позиции в пределах лимитов
3. Дневной лимит сделок/убытков
4. Торговые часы
5. Concurrent positions limit
"""
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Optional, Dict, Any, List, Tuple
from decimal import Decimal, ROUND_DOWN

import structlog
from zoneinfo import ZoneInfo

logger = structlog.get_logger()

MSK = ZoneInfo("Europe/Moscow")


@dataclass
class ValidationResult:
    """Результат валидации."""
    is_valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    
    # Рассчитанные параметры (если валидно)
    sl_price: Optional[float] = None
    tp_price: Optional[float] = None
    risk_rub: Optional[float] = None
    risk_pct: Optional[float] = None
    reward_rub: Optional[float] = None
    risk_reward_ratio: Optional[float] = None
    position_value: Optional[float] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_valid": self.is_valid,
            "errors": self.errors,
            "warnings": self.warnings,
            "sl_price": self.sl_price,
            "tp_price": self.tp_price,
            "risk_rub": self.risk_rub,
            "risk_pct": self.risk_pct,
            "reward_rub": self.reward_rub,
            "risk_reward_ratio": self.risk_reward_ratio,
            "position_value": self.position_value,
        }


@dataclass
class FreeTradeConfig:
    """Настройки для свободного трейдинга."""
    # Включено ли
    enabled: bool = False
    
    # Валидация цены
    max_price_deviation_pct: float = 5.0  # Макс отклонение от текущей цены
    
    # Лимиты
    max_concurrent_positions: int = 3
    max_daily_trades: int = 10
    max_daily_loss_rub: float = 10_000
    
    # Таймауты безопасности
    sl_placement_timeout_sec: int = 10  # Если SL не выставился — отменить entry
    confirmation_timeout_sec: int = 60  # Таймаут на подтверждение заявки
    
    # Торговые часы (МСК)
    trading_start: str = "10:05"  # Не торгуем первые 5 минут
    trading_end: str = "18:40"    # Не торгуем последние 20 минут
    
    # ATR-based SL/TP (по умолчанию R:R = 1:3)
    sl_atr_multiplier: float = 1.0   # SL = entry - ATR * multiplier
    tp_atr_multiplier: float = 3.0   # TP = entry + ATR * multiplier


class OrderValidator:
    """
    Валидатор заявок для свободного трейдинга.
    
    Пример:
        >>> from config import Config
        >>> config = load_config()
        >>> validator = OrderValidator(config)
        >>> result = await validator.validate_buy_order(
        ...     ticker="SBER",
        ...     entry_price=250.0,
        ...     quantity_lots=10,
        ...     current_price=252.0,
        ...     atr=5.0,
        ...     lot_size=10
        ... )
        >>> if result.is_valid:
        ...     print(f"SL: {result.sl_price}, TP: {result.tp_price}")
    """
    
    def __init__(self, config, free_trade_config: FreeTradeConfig = None):
        self.config = config
        self.ft = free_trade_config or FreeTradeConfig()
        self.logger = logger.bind(component="order_validator")
        
        # Счётчики дневных операций (сбрасываются в полночь)
        self._daily_trades: Dict[str, int] = {}  # date -> count
        self._daily_loss: Dict[str, float] = {}  # date -> loss_rub
    
    def _today_key(self) -> str:
        """Ключ для дневных счётчиков."""
        return datetime.now(MSK).strftime("%Y-%m-%d")
    
    def _get_daily_trades(self) -> int:
        """Количество сделок сегодня."""
        return self._daily_trades.get(self._today_key(), 0)
    
    def _get_daily_loss(self) -> float:
        """Убыток сегодня."""
        return self._daily_loss.get(self._today_key(), 0.0)
    
    def increment_daily_trades(self):
        """Увеличивает счётчик дневных сделок."""
        key = self._today_key()
        self._daily_trades[key] = self._daily_trades.get(key, 0) + 1
    
    def add_daily_loss(self, loss_rub: float):
        """Добавляет убыток к дневному счётчику."""
        if loss_rub > 0:
            key = self._today_key()
            self._daily_loss[key] = self._daily_loss.get(key, 0.0) + loss_rub
    
    def reset_daily_counters(self):
        """Сбрасывает дневные счётчики (вызывать в полночь)."""
        today = self._today_key()
        # Оставляем только сегодняшние данные
        self._daily_trades = {k: v for k, v in self._daily_trades.items() if k == today}
        self._daily_loss = {k: v for k, v in self._daily_loss.items() if k == today}
    
    def is_trading_hours(self) -> Tuple[bool, str]:
        """
        Проверяет торговые часы.
        
        Returns:
            (is_ok, reason)
        """
        now = datetime.now(MSK)
        
        # Выходные
        if now.weekday() >= 5:
            return False, "Выходной день"
        
        current_time = now.time()
        
        # Парсим время из конфига
        start_h, start_m = map(int, self.ft.trading_start.split(":"))
        end_h, end_m = map(int, self.ft.trading_end.split(":"))
        
        start_time = time(start_h, start_m)
        end_time = time(end_h, end_m)
        
        if current_time < start_time:
            return False, f"Торги начинаются в {self.ft.trading_start} МСК"
        
        if current_time > end_time:
            return False, f"Торги заканчиваются в {self.ft.trading_end} МСК"
        
        return True, "OK"
    
    def validate_price(
        self,
        entry_price: float,
        current_price: float
    ) -> Tuple[bool, str]:
        """
        Проверяет отклонение цены входа от текущей.
        
        Entry price для TAKE_PROFIT BUY должна быть НИЖЕ текущей.
        """
        if entry_price <= 0:
            return False, "Цена должна быть > 0"
        
        if current_price <= 0:
            return False, "Текущая цена недоступна"
        
        # Для TP BUY цена должна быть ниже текущей
        if entry_price >= current_price:
            return False, f"Цена входа ({entry_price:.2f}) должна быть НИЖЕ текущей ({current_price:.2f})"
        
        # Проверяем отклонение
        deviation_pct = abs(entry_price - current_price) / current_price * 100
        
        if deviation_pct > self.ft.max_price_deviation_pct:
            return False, (
                f"Отклонение {deviation_pct:.1f}% превышает лимит "
                f"{self.ft.max_price_deviation_pct}%"
            )
        
        return True, "OK"
    
    def validate_quantity(
        self,
        quantity_lots: int,
        entry_price: float,
        lot_size: int = 1
    ) -> Tuple[bool, str]:
        """Проверяет размер позиции."""
        if quantity_lots <= 0:
            return False, "Количество лотов должно быть > 0"
        
        position_value = quantity_lots * lot_size * entry_price
        deposit = self.config.trading.deposit_rub
        max_position_pct = self.config.risk.max_position_pct
        
        max_position_value = deposit * max_position_pct
        
        if position_value > max_position_value:
            return False, (
                f"Позиция {position_value:,.0f}₽ превышает лимит "
                f"{max_position_value:,.0f}₽ ({max_position_pct*100:.0f}% депозита)"
            )
        
        return True, "OK"
    
    def validate_daily_limits(self) -> Tuple[bool, str]:
        """Проверяет дневные лимиты."""
        # Лимит сделок
        trades_today = self._get_daily_trades()
        if trades_today >= self.ft.max_daily_trades:
            return False, f"Достигнут лимит {self.ft.max_daily_trades} сделок в день"
        
        # Лимит убытков
        loss_today = self._get_daily_loss()
        if loss_today >= self.ft.max_daily_loss_rub:
            return False, (
                f"Достигнут лимит убытков {self.ft.max_daily_loss_rub:,.0f}₽ в день "
                f"(текущий: {loss_today:,.0f}₽)"
            )
        
        return True, "OK"
    
    def calculate_sl_tp(
        self,
        entry_price: float,
        atr: float,
        direction: str = "long"
    ) -> Tuple[float, float]:
        """
        Рассчитывает SL и TP на основе ATR.
        
        Returns:
            (sl_price, tp_price)
        """
        sl_offset = atr * self.ft.sl_atr_multiplier
        tp_offset = atr * self.ft.tp_atr_multiplier
        
        if direction == "long":
            sl_price = entry_price - sl_offset
            tp_price = entry_price + tp_offset
        else:
            sl_price = entry_price + sl_offset
            tp_price = entry_price - tp_offset
        
        return round(sl_price, 2), round(tp_price, 2)
    
    async def validate_buy_order(
        self,
        ticker: str,
        entry_price: float,
        quantity_lots: int,
        current_price: float,
        atr: float,
        lot_size: int = 1,
        current_positions: int = 0
    ) -> ValidationResult:
        """
        Полная валидация заявки на покупку.
        
        Args:
            ticker: Тикер
            entry_price: Цена входа
            quantity_lots: Количество лотов
            current_price: Текущая рыночная цена
            atr: ATR инструмента
            lot_size: Размер лота
            current_positions: Текущее кол-во открытых позиций
        
        Returns:
            ValidationResult
        """
        errors = []
        warnings = []
        
        self.logger.info(
            "validating_buy_order",
            ticker=ticker,
            entry_price=entry_price,
            quantity_lots=quantity_lots,
            current_price=current_price,
            atr=atr
        )
        
        # 1. Торговые часы
        is_ok, reason = self.is_trading_hours()
        if not is_ok:
            errors.append(f"⏰ {reason}")
        
        # 2. Concurrent positions
        if current_positions >= self.ft.max_concurrent_positions:
            errors.append(
                f"📊 Достигнут лимит {self.ft.max_concurrent_positions} "
                f"одновременных позиций"
            )
        
        # 3. Дневные лимиты
        is_ok, reason = self.validate_daily_limits()
        if not is_ok:
            errors.append(f"📅 {reason}")
        
        # 4. Валидация цены
        is_ok, reason = self.validate_price(entry_price, current_price)
        if not is_ok:
            errors.append(f"💰 {reason}")
        
        # 5. Валидация размера
        is_ok, reason = self.validate_quantity(quantity_lots, entry_price, lot_size)
        if not is_ok:
            errors.append(f"📦 {reason}")
        
        # Если есть ошибки — возвращаем сразу
        if errors:
            self.logger.warning(
                "validation_failed",
                ticker=ticker,
                errors=errors
            )
            return ValidationResult(
                is_valid=False,
                errors=errors,
                warnings=warnings
            )
        
        # 6. Рассчитываем SL/TP
        sl_price, tp_price = self.calculate_sl_tp(entry_price, atr, "long")
        
        # Проверка что SL положительный
        if sl_price <= 0:
            errors.append(f"🛑 SL получился отрицательным: {sl_price:.2f}")
            return ValidationResult(is_valid=False, errors=errors, warnings=warnings)
        
        # 7. Расчёт риска
        quantity_shares = quantity_lots * lot_size
        position_value = quantity_shares * entry_price
        
        sl_offset = entry_price - sl_price
        tp_offset = tp_price - entry_price
        
        risk_rub = sl_offset * quantity_shares
        reward_rub = tp_offset * quantity_shares
        risk_pct = risk_rub / self.config.trading.deposit_rub * 100
        
        risk_reward_ratio = reward_rub / risk_rub if risk_rub > 0 else 0
        
        # Предупреждения
        if risk_pct > self.config.trading.risk_per_trade_pct * 100 * 1.5:
            warnings.append(
                f"⚠️ Риск {risk_pct:.2f}% выше рекомендуемого "
                f"{self.config.trading.risk_per_trade_pct * 100:.1f}%"
            )
        
        if risk_reward_ratio < 2:
            warnings.append(
                f"⚠️ Risk/Reward {risk_reward_ratio:.1f}:1 ниже рекомендуемого 3:1"
            )
        
        # Проверка что цена TP выше current (иначе сработает сразу)
        if tp_price <= current_price:
            warnings.append(
                f"⚠️ TP ({tp_price:.2f}) ниже текущей цены ({current_price:.2f}) — "
                f"может сработать сразу после входа"
            )
        
        self.logger.info(
            "validation_passed",
            ticker=ticker,
            sl_price=sl_price,
            tp_price=tp_price,
            risk_rub=round(risk_rub, 0),
            risk_pct=round(risk_pct, 2)
        )
        
        return ValidationResult(
            is_valid=True,
            errors=[],
            warnings=warnings,
            sl_price=sl_price,
            tp_price=tp_price,
            risk_rub=round(risk_rub, 0),
            risk_pct=round(risk_pct, 2),
            reward_rub=round(reward_rub, 0),
            risk_reward_ratio=round(risk_reward_ratio, 1),
            position_value=round(position_value, 0)
        )


def format_confirmation_message(
    ticker: str,
    entry_price: float,
    quantity_lots: int,
    lot_size: int,
    validation: ValidationResult
) -> str:
    """
    Форматирует сообщение подтверждения для Telegram.
    
    Returns:
        HTML-форматированное сообщение
    """
    quantity_shares = quantity_lots * lot_size
    
    lines = [
        f"📋 <b>Подтвердите заявку</b>",
        "",
        f"📌 <b>{ticker}</b>",
        f"📥 Цена входа: <b>{entry_price:,.2f} ₽</b>",
        f"📦 Количество: {quantity_lots} лот ({quantity_shares} шт)",
        "",
        f"🛑 Stop-Loss: <b>{validation.sl_price:,.2f} ₽</b>",
        f"🎯 Take-Profit: <b>{validation.tp_price:,.2f} ₽</b>",
        "",
        f"💸 Риск: <b>{validation.risk_rub:,.0f} ₽</b> ({validation.risk_pct:.2f}%)",
        f"💰 Потенц. прибыль: {validation.reward_rub:,.0f} ₽",
        f"📊 R:R = 1:{validation.risk_reward_ratio:.1f}",
        f"💼 Размер позиции: {validation.position_value:,.0f} ₽",
    ]
    
    if validation.warnings:
        lines.append("")
        lines.extend(validation.warnings)
    
    return "\n".join(lines)


def format_error_message(ticker: str, validation: ValidationResult) -> str:
    """Форматирует сообщение об ошибке."""
    lines = [
        f"❌ <b>Заявка отклонена: {ticker}</b>",
        "",
    ]
    lines.extend(validation.errors)
    return "\n".join(lines)
