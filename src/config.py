"""
Загрузка и валидация конфигурации.

Источники:
- config.yaml: параметры стратегий, риска, расписания
- .env: секреты (токены, пароли)
"""
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

import yaml
from dotenv import load_dotenv

load_dotenv()


# ═══════════════════════════════════════════════════════════════════════════════
# DATA CLASSES
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class TinkoffConfig:
    """Настройки Tinkoff API."""
    token: str
    account_id: str = ""


@dataclass
class TelegramConfig:
    """Настройки Telegram бота."""
    bot_token: str
    chat_id: str
    authorized_users: List[int] = field(default_factory=list)


@dataclass
class DatabaseConfig:
    """Настройки PostgreSQL."""
    host: str
    port: int
    name: str
    user: str
    password: str

    @property
    def url(self) -> str:
        """Строка подключения для SQLAlchemy (async)."""
        return f"postgresql+asyncpg://{self.user}:{self.password}@{self.host}:{self.port}/{self.name}"

    @property
    def sync_url(self) -> str:
        """Синхронная строка подключения."""
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.name}"


@dataclass
class TradingConfig:
    """Настройки торговли."""
    deposit_rub: float
    risk_per_trade_pct: float
    max_position_pct: float


@dataclass
class BollingerConfig:
    """Настройки Bollinger Bands."""
    period: int = 20
    std_multiplier: float = 2.0
    entry_threshold_pct: float = 1.0


@dataclass
class RiskConfig:
    """Настройки риск-менеджмента."""
    stop_loss_atr: float = 1.0
    take_profit_atr: float = 3.0
    max_position_pct: float = 0.25
    max_margin_pct: float = 0.30
    max_open_positions: int = 3
    max_daily_loss_pct: float = 0.03
    cooldown_after_stop_min: int = 30


@dataclass
class LiquidityConfig:
    """Настройки фильтра ликвидности."""
    min_avg_volume_rub: float = 50_000_000
    min_trades_per_day: int = 1000
    max_spread_pct: float = 0.15
    lookback_days: int = 20
    max_instruments: int = 30
    extended_lookback_days: int = 90
    min_trading_days: int = 26


@dataclass
class TradingHoursConfig:
    """Торговые часы."""
    start: str = "10:00"
    end: str = "19:00"
    timezone: str = "Europe/Moscow"


@dataclass
class ScheduleConfig:
    """Расписание задач."""
    daily_calc_time: str = "06:30"


@dataclass
class FreeTradeConfig:
    """
    Настройки для свободного трейдинга.
    
    Позволяет выставлять заявки с произвольной ценой
    через команду /buy TICKER PRICE [LOTS]
    """
    # Включено ли свободный трейдинг
    enabled: bool = False
    
    # Валидация цены входа
    max_price_deviation_pct: float = 5.0  # Макс отклонение от текущей цены
    
    # Лимиты безопасности
    max_concurrent_positions: int = 3     # Макс одновременных позиций
    max_daily_trades: int = 10            # Макс сделок в день
    max_daily_loss_rub: float = 10_000    # Макс дневной убыток
    
    # Таймауты
    sl_placement_timeout_sec: int = 10    # Если SL не выставился → аварийное закрытие
    confirmation_timeout_sec: int = 60    # Таймаут на подтверждение заявки
    
    # Торговые часы (МСК) — не торгуем в волатильные периоды
    trading_start: str = "10:05"  # Пропускаем первые 5 минут
    trading_end: str = "18:40"    # Пропускаем последние 20 минут
    
    # ATR-based SL/TP (по умолчанию R:R = 1:3)
    sl_atr_multiplier: float = 1.0
    tp_atr_multiplier: float = 3.0


@dataclass
class Config:
    """Главный конфиг приложения."""
    tinkoff: TinkoffConfig
    telegram: TelegramConfig
    database: DatabaseConfig
    trading: TradingConfig
    bollinger: BollingerConfig
    risk: RiskConfig
    liquidity: LiquidityConfig
    trading_hours: TradingHoursConfig
    schedule: ScheduleConfig
    free_trading: FreeTradeConfig
    dry_run: bool = True


# ═══════════════════════════════════════════════════════════════════════════════
# LOADING
# ═══════════════════════════════════════════════════════════════════════════════

def _parse_authorized_users(env_value: str) -> List[int]:
    """
    Парсит список авторизованных user_id из строки.
    
    Формат: "123456789,987654321" или "123456789"
    """
    if not env_value:
        return []
    
    users = []
    for part in env_value.split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            users.append(int(part))
    return users


def load_config(config_path: str = "config.yaml") -> Config:
    """
    Загружает конфигурацию из YAML и env-переменных.
    
    Args:
        config_path: Путь к config.yaml
    
    Returns:
        Config объект
    
    Raises:
        ValueError: Если не хватает обязательных переменных
        FileNotFoundError: Если config.yaml не найден
    """
    # Проверяем наличие файла
    if not Path(config_path).exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    # Загружаем YAML
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    # Валидация обязательных env-переменных
    required_env = ["TINKOFF_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]
    missing = [e for e in required_env if not os.getenv(e)]
    if missing:
        raise ValueError(f"Missing required env vars: {', '.join(missing)}")

    # Парсим авторизованных пользователей
    authorized_users = _parse_authorized_users(
        os.getenv("TELEGRAM_AUTHORIZED_USERS", "")
    )
    
    # Если не указаны — используем chat_id как единственного авторизованного
    if not authorized_users:
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        if chat_id.lstrip("-").isdigit():
            authorized_users = [int(chat_id)]

    # Секции YAML
    risk_cfg = cfg.get("risk", {})
    liquidity_cfg = cfg.get("liquidity", {})
    trading_hours_cfg = cfg.get("trading_hours", {})
    schedule_cfg = cfg.get("schedule", {})
    strategy_cfg = cfg.get("strategy", {}).get("bollinger_bounce", {})
    free_trading_cfg = cfg.get("free_trading", {})
    safety_cfg = cfg.get("safety", {})

    return Config(
        tinkoff=TinkoffConfig(
            token=os.getenv("TINKOFF_TOKEN", ""),
            account_id=os.getenv("TINKOFF_ACCOUNT_ID", ""),
        ),
        telegram=TelegramConfig(
            bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
            authorized_users=authorized_users,
        ),
        database=DatabaseConfig(
            host=os.getenv("POSTGRES_HOST", "localhost"),
            port=int(os.getenv("POSTGRES_PORT", "5432")),
            name=os.getenv("POSTGRES_DB", "trading_bot"),
            user=os.getenv("POSTGRES_USER", "trader"),
            password=os.getenv("POSTGRES_PASSWORD", ""),
        ),
        trading=TradingConfig(
            deposit_rub=float(os.getenv("DEPOSIT_RUB", "1000000")),
            risk_per_trade_pct=float(os.getenv("RISK_PER_TRADE_PCT", "0.01")),
            max_position_pct=float(os.getenv("MAX_POSITION_PCT", "0.25")),
        ),
        bollinger=BollingerConfig(
            period=strategy_cfg.get("bollinger_period", 20),
            std_multiplier=strategy_cfg.get("bollinger_std", 2.0),
            entry_threshold_pct=strategy_cfg.get("entry_threshold_pct", 1.0),
        ),
        risk=RiskConfig(
            stop_loss_atr=risk_cfg.get("stop_loss_atr", 1.0),
            take_profit_atr=risk_cfg.get("take_profit_atr", 3.0),
            max_position_pct=risk_cfg.get("max_position_pct", 0.25),
            max_margin_pct=risk_cfg.get("max_margin_pct", 0.30),
            max_open_positions=risk_cfg.get("max_open_positions", 3),
            max_daily_loss_pct=risk_cfg.get("max_daily_loss_pct", 0.03),
            cooldown_after_stop_min=risk_cfg.get("cooldown_after_stop_min", 30),
        ),
        liquidity=LiquidityConfig(
            min_avg_volume_rub=liquidity_cfg.get("min_avg_volume_rub", 50_000_000),
            min_trades_per_day=liquidity_cfg.get("min_trades_per_day", 1000),
            max_spread_pct=liquidity_cfg.get("max_spread_pct", 0.15),
            lookback_days=liquidity_cfg.get("lookback_days", 20),
            max_instruments=liquidity_cfg.get("max_instruments", 30),
            extended_lookback_days=liquidity_cfg.get("extended_lookback_days", 90),
            min_trading_days=liquidity_cfg.get("min_trading_days", 26),
        ),
        trading_hours=TradingHoursConfig(
            start=trading_hours_cfg.get("start", "10:00"),
            end=trading_hours_cfg.get("end", "19:00"),
            timezone=trading_hours_cfg.get("timezone", "Europe/Moscow"),
        ),
        schedule=ScheduleConfig(
            daily_calc_time=schedule_cfg.get("daily_calc_time", "06:30"),
        ),
        free_trading=FreeTradeConfig(
            enabled=free_trading_cfg.get("enabled", False),
            max_price_deviation_pct=free_trading_cfg.get("max_price_deviation_pct", 5.0),
            max_concurrent_positions=free_trading_cfg.get("max_concurrent_positions", 3),
            max_daily_trades=free_trading_cfg.get("max_daily_trades", 10),
            max_daily_loss_rub=free_trading_cfg.get("max_daily_loss_rub", 10_000),
            sl_placement_timeout_sec=free_trading_cfg.get("sl_placement_timeout_sec", 10),
            confirmation_timeout_sec=free_trading_cfg.get("confirmation_timeout_sec", 60),
            trading_start=free_trading_cfg.get("trading_start", "10:05"),
            trading_end=free_trading_cfg.get("trading_end", "18:40"),
            sl_atr_multiplier=free_trading_cfg.get("sl_atr_multiplier", 1.0),
            tp_atr_multiplier=free_trading_cfg.get("tp_atr_multiplier", 3.0),
        ),
        dry_run=safety_cfg.get("dry_run", True),
    )
