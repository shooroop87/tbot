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
from api.telegram_bot import (
    TelegramBotAiogram, 
    update_shares_cache, 
    set_position_watcher,
    set_scheduler,
    set_watcher_task,
)
from db.repository import Repository
from scheduler.jobs import DailyCalculationJob
from executor.position_watcher import PositionWatcher

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.dev.ConsoleRenderer(colors=True)
    ]
)
logger = structlog.get_logger()

MSK = pytz.timezone("Europe/Moscow")
VERSION = "0.3.0"


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
    
    # Telegram бот (aiogram)
    telegram_bot = TelegramBotAiogram(config)
    
    # Position Watcher для отслеживания заявок
    position_watcher = PositionWatcher(config, notifier, poll_interval=5.0)
    set_position_watcher(position_watcher)

    await notifier.send_startup(VERSION)

    # Режим --now: немедленный расчёт
    if "--now" in sys.argv:
        logger.info("running_immediate_calculation")
        await daily_job.run()
        
        # Режим --once: только расчёт, без polling
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
    set_scheduler(scheduler)
    logger.info("scheduler_started", daily_calc_time=f"{hour:02d}:{minute:02d} MSK")

    # Запускаем aiogram polling (блокирующий)
    logger.info("telegram_polling_started", message="Бот готов. Ctrl+C для остановки")
    
    # Запускаем Position Watcher в фоне
    watcher_task = asyncio.create_task(position_watcher.start())
    set_watcher_task(watcher_task)
    logger.info("position_watcher_started", interval=5.0)
    
    try:
        # start_polling блокирует до остановки
        await telegram_bot.start()
    except asyncio.CancelledError:
        logger.info("polling_cancelled")
    finally:
        logger.info("bot_stopping")
        await position_watcher.stop()
        watcher_task.cancel()
        await telegram_bot.stop()
        scheduler.shutdown(wait=False)
        await repo.close()
        logger.info("bot_stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("keyboard_interrupt")