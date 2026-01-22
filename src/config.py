"""
Загрузка и валидация конфигурации.
"""
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

import yaml
from dotenv import load_dotenv

load_dotenv()


@dataclass
class TinkoffConfig:
    token: str
    account_id: str = ""


@dataclass
class TelegramConfig:
    bot_token: str
    chat_id: str
    authorized_users: List[int] = field(default_factory=list)


@dataclass
class DatabaseConfig:
    host: str
    port: int
    name: str
    user: str
    password: str

    @property
    def url(self) -> str:
        return f"postgresql+asyncpg://{self.user}:{self.password}@{self.host}:{self.port}/{self.name}"


@dataclass
class TradingConfig:
    deposit_rub: float
    risk_per_trade_pct: float
    max_position_pct: float


@dataclass
class BollingerConfig:
    period: int = 20
    std_multiplier: float = 2.0
    entry_threshold_pct: float = 1.0


@dataclass
class LiquidityConfig:
    min_avg_volume_rub: float = 50_000_000
    max_spread_pct: float = 0.15
    lookback_days: int = 20
    max_instruments: int = 30
    extended_lookback_days: int = 90
    min_trading_days: int = 26


@dataclass
class ScheduleConfig:
    daily_calc_time: str = "06:30"


@dataclass
class Config:
    tinkoff: TinkoffConfig
    telegram: TelegramConfig
    database: DatabaseConfig
    trading: TradingConfig
    bollinger: BollingerConfig
    liquidity: LiquidityConfig
    schedule: ScheduleConfig
    dry_run: bool = True


def _parse_authorized_users(env_value: str) -> List[int]:
    """Парсит список user_id из строки."""
    if not env_value:
        return []
    users = []
    for part in env_value.split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            users.append(int(part))
    return users


def load_config(config_path: str = "config.yaml") -> Config:
    """Загружает конфигурацию."""
    if not Path(config_path).exists():
        raise FileNotFoundError(f"Config not found: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    required_env = ["TINKOFF_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]
    missing = [e for e in required_env if not os.getenv(e)]
    if missing:
        raise ValueError(f"Missing env vars: {', '.join(missing)}")

    authorized_users = _parse_authorized_users(
        os.getenv("TELEGRAM_AUTHORIZED_USERS", "")
    )
    if not authorized_users:
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        if chat_id.lstrip("-").isdigit():
            authorized_users = [int(chat_id)]

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
            period=cfg.get("strategy", {}).get("bollinger_bounce", {}).get("bollinger_period", 20),
            std_multiplier=cfg.get("strategy", {}).get("bollinger_bounce", {}).get("bollinger_std", 2.0),
            entry_threshold_pct=cfg.get("strategy", {}).get("bollinger_bounce", {}).get("entry_threshold_pct", 1.0),
        ),
        liquidity=LiquidityConfig(
            min_avg_volume_rub=cfg.get("liquidity", {}).get("min_avg_volume_rub", 50_000_000),
            max_spread_pct=cfg.get("liquidity", {}).get("max_spread_pct", 0.15),
            lookback_days=cfg.get("liquidity", {}).get("lookback_days", 20),
            max_instruments=cfg.get("liquidity", {}).get("max_instruments", 30),
        ),
        schedule=ScheduleConfig(
            daily_calc_time=cfg.get("schedule", {}).get("daily_calc_time", "06:30"),
        ),
        dry_run=cfg.get("safety", {}).get("dry_run", True),
    )