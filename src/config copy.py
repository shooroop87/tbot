"""
Загрузка и валидация конфигурации

Источники:
- config.yaml: параметры стратегий, риска, расписания
- .env: секреты (токены, пароли)
"""
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv

load_dotenv()


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
        """Строка подключения для SQLAlchemy."""
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
class ATRConfig:
    """Настройки ATR."""
    period: int = 14


@dataclass
class BollingerConfig:
    """Настройки Bollinger Bands."""
    period: int = 20
    std_multiplier: float = 2.0
    entry_threshold_pct: float = 1.0


@dataclass
class RiskConfig:
    """Настройки риск-менеджмента."""
    stop_loss_atr: float = 0.3
    take_profit_atr: float = 0.5
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
class Config:
    """Главный конфиг приложения."""
    tinkoff: TinkoffConfig
    telegram: TelegramConfig
    database: DatabaseConfig
    trading: TradingConfig
    atr: ATRConfig
    bollinger: BollingerConfig
    risk: RiskConfig
    liquidity: LiquidityConfig
    trading_hours: TradingHoursConfig
    schedule: ScheduleConfig
    dry_run: bool = True
    kill_switch: bool = False


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

    return Config(
        tinkoff=TinkoffConfig(
            token=os.getenv("TINKOFF_TOKEN", ""),
            account_id=os.getenv("TINKOFF_ACCOUNT_ID", ""),
        ),
        telegram=TelegramConfig(
            bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
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
        atr=ATRConfig(
            period=cfg.get("atr", {}).get("period", 14),
        ),
        bollinger=BollingerConfig(
            period=cfg.get("strategy", {}).get("bollinger_bounce", {}).get("bollinger_period", 20),
            std_multiplier=cfg.get("strategy", {}).get("bollinger_bounce", {}).get("bollinger_std", 2.0),
            entry_threshold_pct=cfg.get("strategy", {}).get("bollinger_bounce", {}).get("entry_threshold_pct", 1.0),
        ),
        risk=RiskConfig(
            stop_loss_atr=cfg.get("risk", {}).get("stop_loss_atr", 0.3),
            take_profit_atr=cfg.get("risk", {}).get("take_profit_atr", 0.5),
            max_position_pct=cfg.get("risk", {}).get("max_position_pct", 0.25),
            max_margin_pct=cfg.get("risk", {}).get("max_margin_pct", 0.30),
            max_open_positions=cfg.get("risk", {}).get("max_open_positions", 3),
            max_daily_loss_pct=cfg.get("risk", {}).get("max_daily_loss_pct", 0.03),
            cooldown_after_stop_min=cfg.get("risk", {}).get("cooldown_after_stop_min", 30),
        ),
        liquidity=LiquidityConfig(
            min_avg_volume_rub=cfg.get("liquidity", {}).get("min_avg_volume_rub", 50_000_000),
            min_trades_per_day=cfg.get("liquidity", {}).get("min_trades_per_day", 1000),
            max_spread_pct=cfg.get("liquidity", {}).get("max_spread_pct", 0.15),
            lookback_days=cfg.get("liquidity", {}).get("lookback_days", 20),
            max_instruments=cfg.get("liquidity", {}).get("max_instruments", 30),
        ),
        trading_hours=TradingHoursConfig(
            start=cfg.get("trading_hours", {}).get("start", "10:00"),
            end=cfg.get("trading_hours", {}).get("end", "19:00"),
            timezone=cfg.get("trading_hours", {}).get("timezone", "Europe/Moscow"),
        ),
        schedule=ScheduleConfig(
            daily_calc_time=cfg.get("schedule", {}).get("daily_calc_time", "06:30"),
        ),
        dry_run=cfg.get("safety", {}).get("dry_run", True),
        kill_switch=cfg.get("safety", {}).get("kill_switch", False),
    )
