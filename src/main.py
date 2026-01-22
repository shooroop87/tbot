#!/usr/bin/env python3
"""
Trading Bot - Стратегическая торговля на MOEX.

Запуск:
    python main.py         # Ждёт расписания (06:30 МСК) + Telegram
    python main.py --now   # Немедленный расчёт + Telegram
    python main.py --now --once  # Только расчёт, без polling

Управление через Telegram:
    /status  - текущий статус
    /pause   - приостановить бота
    /resume  - возобновить работу
    /auto    - авто-режим (SL/TP автоматически)
    /manual  - ручной режим (только уведомления)
    /kill    - экстренное отключение

⚠️ БЕЗОПАСНОСТЬ:
- Бот запускается ВЫКЛЮЧЕННЫМ (is_active=False)
- Нужно явно включить через /resume или /auto
- Все заявки сохраняются в БД и переживают рестарт

⚠️ ПРЕДУПРЕЖДЕНИЕ: Торговля на бирже несёт риск потери капитала.
"""
import asyncio
import sys
from pathlib import Path

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

# Добавляем src в путь
sys.path.insert(0, str(Path(__file__).parent))

from config import load_config
from api.telegram_notifier import TelegramNotifier
from api.telegram_bot import TelegramBotAiogram, update_shares_cache, set_globals
from db.repository import Repository
from scheduler.jobs import DailyCalculationJob
from executor.position_watcher import PositionWatcher

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
VERSION = "0.4.0"


async def main():
    """Главная точка входа."""
    logger.info("bot_starting", version=VERSION)

    # ═══════════════════════════════════════════════════════════════════════
    # ЗАГРУЗКА КОНФИГУРАЦИИ
    # ═══════════════════════════════════════════════════════════════════════
    try:
        config_path = Path(__file__).parent.parent / "config.yaml"
        config = load_config(str(config_path))
        logger.info("config_loaded", 
                   dry_run=config.dry_run,
                   authorized_users=len(config.telegram.authorized_users))
    except Exception as e:
        logger.error("config_load_error", error=str(e))
        return

    # ═══════════════════════════════════════════════════════════════════════
    # ИНИЦИАЛИЗАЦИЯ КОМПОНЕНТОВ
    # ═══════════════════════════════════════════════════════════════════════
    
    # База данных
    repo = Repository(config.database.url)
    await repo.init_db()
    
    # Telegram notifier
    notifier = TelegramNotifier(config.telegram)
    
    # Position Watcher (с персистентностью и kill switch)
    position_watcher = PositionWatcher(
        config=config, 
        repository=repo,
        notifier=notifier, 
        poll_interval=5.0
    )
    
    # Daily calculation job
    daily_job = DailyCalculationJob(config, repo, notifier)
    
    # Telegram бот
    telegram_bot = TelegramBotAiogram(config)
    
    # ═══════════════════════════════════════════════════════════════════════
    # СВЯЗЫВАНИЕ КОМПОНЕНТОВ
    # ═══════════════════════════════════════════════════════════════════════
    
    # Передаём зависимости в telegram_bot
    set_globals(
        watcher=position_watcher,
        repo=repo,
        config=config,
    )

    # ═══════════════════════════════════════════════════════════════════════
    # ПРОВЕРКА СТАТУСА БОТА
    # ═══════════════════════════════════════════════════════════════════════
    
    settings = await repo.get_bot_settings()
    logger.info("bot_settings_loaded",
               is_active=settings.is_active,
               mode=settings.mode,
               last_change=settings.last_change_reason)
    
    # Уведомляем о запуске
    status_text = "🟢 Активен" if settings.is_active else "🔴 Выключен"
    await notifier.send_message(
        f"🤖 <b>Бот запущен</b>\n"
        f"📌 Версия: {VERSION}\n"
        f"⚙️ Статус: {status_text}\n"
        f"🎛 Режим: {settings.mode.upper()}\n\n"
        f"{'⏰ Ожидание расчёта в 06:30 МСК' if settings.is_active else '⚠️ Включите бота: /resume или /auto'}\n\n"
        f"💡 Команды: /help"
    )

    # ═══════════════════════════════════════════════════════════════════════
    # РЕЖИМ --now: НЕМЕДЛЕННЫЙ РАСЧЁТ
    # ═══════════════════════════════════════════════════════════════════════
    
    if "--now" in sys.argv:
        logger.info("running_immediate_calculation")
        await daily_job.run()
        
        # Режим --once: только расчёт, без polling
        if "--once" in sys.argv:
            logger.info("single_run_complete")
            await repo.close()
            return

    # ═══════════════════════════════════════════════════════════════════════
    # ПЛАНИРОВЩИК
    # ═══════════════════════════════════════════════════════════════════════
    
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
    set_globals(scheduler=scheduler)
    logger.info("scheduler_started", daily_calc_time=f"{hour:02d}:{minute:02d} MSK")

    # ═══════════════════════════════════════════════════════════════════════
    # POSITION WATCHER (фоновая задача)
    # ═══════════════════════════════════════════════════════════════════════
    
    watcher_task = asyncio.create_task(position_watcher.start())
    set_globals(watcher_task=watcher_task)
    logger.info("position_watcher_started", interval=5.0)

    # ═══════════════════════════════════════════════════════════════════════
    # TELEGRAM POLLING (блокирующий)
    # ═══════════════════════════════════════════════════════════════════════
    
    logger.info("telegram_polling_started", message="Бот готов. Ctrl+C для остановки")
    
    try:
        await telegram_bot.start()
    except asyncio.CancelledError:
        logger.info("polling_cancelled")
    finally:
        # ═══════════════════════════════════════════════════════════════════
        # GRACEFUL SHUTDOWN
        # ═══════════════════════════════════════════════════════════════════
        logger.info("bot_stopping")
        
        # Останавливаем watcher
        await position_watcher.stop()
        watcher_task.cancel()
        try:
            await watcher_task
        except asyncio.CancelledError:
            pass
        
        # Останавливаем Telegram бот
        await telegram_bot.stop()
        
        # Останавливаем scheduler
        scheduler.shutdown(wait=False)
        
        # Закрываем БД
        await repo.close()
        
        logger.info("bot_stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("keyboard_interrupt")