#!/usr/bin/env python3
"""
Trading Bot - Стратегическая торговля на MOEX.

Точка входа приложения.

Запуск:
    python main.py         # Ждёт расписания (06:30 МСК)
    python main.py --now   # Немедленный расчёт

⚠️ ПРЕДУПРЕЖДЕНИЕ: Торговля на бирже несёт риск потери капитала.
"""
import asyncio
import sys
from pathlib import Path

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

# Настройка путей
sys.path.insert(0, str(Path(__file__).parent))

from config import load_config
from api.telegram_notifier import TelegramNotifier
from db.repository import Repository
from scheduler.jobs import DailyCalculationJob

# Настройка логирования
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.dev.ConsoleRenderer(colors=True)
    ]
)
logger = structlog.get_logger()

MSK = pytz.timezone("Europe/Moscow")
VERSION = "0.1.0"


async def main():
    """Главная точка входа."""
    logger.info("bot_starting", version=VERSION)

    # ═══════════════════════════════════════════════════════════
    # 1. Загрузка конфигурации
    # ═══════════════════════════════════════════════════════════
    try:
        config_path = Path(__file__).parent.parent / "config.yaml"
        config = load_config(str(config_path))
        logger.info("config_loaded", dry_run=config.dry_run)
    except Exception as e:
        logger.error("config_load_error", error=str(e))
        return

    # ═══════════════════════════════════════════════════════════
    # 2. Инициализация компонентов
    # ═══════════════════════════════════════════════════════════
    
    # База данных
    repo = Repository(config.database.url)
    await repo.init_db()

    # Telegram
    notifier = TelegramNotifier(config.telegram)

    # Job ежедневного расчёта
    daily_job = DailyCalculationJob(config, repo, notifier)

    # ═══════════════════════════════════════════════════════════
    # 3. Отправка стартового сообщения
    # ═══════════════════════════════════════════════════════════
    await notifier.send_startup(VERSION)

    # ═══════════════════════════════════════════════════════════
    # 4. Немедленный запуск (если --now)
    # ═══════════════════════════════════════════════════════════
    if "--now" in sys.argv:
        logger.info("running_immediate_calculation")
        await daily_job.run()
        
        # Если только расчёт, завершаем
        if "--once" in sys.argv:
            logger.info("single_run_complete")
            await repo.close()
            return

    # ═══════════════════════════════════════════════════════════
    # 5. Настройка планировщика
    # ═══════════════════════════════════════════════════════════
    scheduler = AsyncIOScheduler(timezone=MSK)

    # Парсим время расчёта
    calc_time = config.schedule.daily_calc_time
    hour, minute = map(int, calc_time.split(":"))

    # Ежедневный расчёт
    scheduler.add_job(
        daily_job.run,
        trigger=CronTrigger(hour=hour, minute=minute, timezone=MSK),
        id="daily_calculation",
        replace_existing=True,
        misfire_grace_time=3600,  # 1 час на случай перезапуска
    )

    scheduler.start()
    logger.info(
        "scheduler_started",
        daily_calc_time=f"{hour:02d}:{minute:02d} MSK"
    )

    # ═══════════════════════════════════════════════════════════
    # 6. Основной цикл
    # ═══════════════════════════════════════════════════════════
    try:
        logger.info("bot_running", message="Press Ctrl+C to stop")
        while True:
            await asyncio.sleep(60)
    except KeyboardInterrupt:
        logger.info("bot_stopping")
    finally:
        scheduler.shutdown(wait=False)
        await repo.close()
        logger.info("bot_stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
