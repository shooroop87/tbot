#!/usr/bin/env python3
"""
Trading Bot - Стратегическая торговля на MOEX.

Запуск:
    python main.py         # Ждёт расписания (06:30 МСК) + обработка кнопок
    python main.py --now   # Немедленный расчёт + обработка кнопок
    python main.py --now --once  # Только расчёт, без polling

⚠️ ПРЕДУПРЕЖДЕНИЕ: Торговля на бирже несёт риск потери капитала.
"""
import asyncio
import sys
from pathlib import Path

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

sys.path.insert(0, str(Path(__file__).parent))

from config import load_config
from api.telegram_notifier import TelegramNotifier
from api.telegram_bot import TelegramBot
from db.repository import Repository
from scheduler.jobs import DailyCalculationJob

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.dev.ConsoleRenderer(colors=True)
    ]
)
logger = structlog.get_logger()

MSK = pytz.timezone("Europe/Moscow")
VERSION = "0.2.0"


async def main():
    """Главная точка входа."""
    logger.info("bot_starting", version=VERSION)

    try:
        config_path = Path(__file__).parent.parent / "config.yaml"
        config = load_config(str(config_path))
        logger.info("config_loaded", dry_run=config.dry_run)
    except Exception as e:
        logger.error("config_load_error", error=str(e))
        return

    repo = Repository(config.database.url)
    await repo.init_db()

    notifier = TelegramNotifier(config.telegram)
    daily_job = DailyCalculationJob(config, repo, notifier)
    
    # Telegram бот для обработки кнопок
    telegram_bot = TelegramBot(config)

    await notifier.send_startup(VERSION)

    if "--now" in sys.argv:
        logger.info("running_immediate_calculation")
        await daily_job.run()
        
        if "--once" in sys.argv:
            logger.info("single_run_complete")
            await repo.close()
            return

    # Планировщик ежедневного расчёта
    scheduler = AsyncIOScheduler(timezone=MSK)

    calc_time = config.schedule.daily_calc_time
    hour, minute = map(int, calc_time.split(":"))

    scheduler.add_job(
        daily_job.run,
        trigger=CronTrigger(hour=hour, minute=minute, timezone=MSK),
        id="daily_calculation",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    scheduler.start()
    logger.info("scheduler_started", daily_calc_time=f"{hour:02d}:{minute:02d} MSK")

    # Запускаем polling для кнопок
    polling_task = asyncio.create_task(telegram_bot.start_polling())
    logger.info("telegram_polling_started", message="Кнопки активны")

    try:
        logger.info("bot_running", message="Press Ctrl+C to stop")
        while True:
            await asyncio.sleep(60)
    except KeyboardInterrupt:
        logger.info("bot_stopping")
    finally:
        await telegram_bot.stop()
        polling_task.cancel()
        scheduler.shutdown(wait=False)
        await repo.close()
        logger.info("bot_stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass