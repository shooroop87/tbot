"""
Alembic ENV для async SQLAlchemy.

Поддерживает:
- Async PostgreSQL (asyncpg)
- Автогенерация миграций из моделей
- Загрузка переменных из .env.dev (локально) или .env (production)
"""
import asyncio
import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# ═══════════════════════════════════════════════════════════
# Загрузка .env (приоритет: .env.dev → .env)
# ═══════════════════════════════════════════════════════════
from dotenv import load_dotenv

# Путь: src/alembic/env.py → src/ → project_root/
current_dir = Path(__file__).parent  # alembic/
src_dir = current_dir.parent          # src/
project_root = src_dir.parent         # tbot/

# Приоритет: .env.dev для локальной разработки
env_files = [
    project_root / ".env.dev",       # ← ПЕРВЫЙ (локальная разработка)
    src_dir / ".env.dev",
    project_root / ".env",           # production
    src_dir / ".env",
]

loaded_env = None
for env_path in env_files:
    if env_path.exists():
        load_dotenv(env_path)
        loaded_env = env_path
        print(f"📁 Loaded env from: {env_path}")
        break

if not loaded_env:
    print("⚠️ No .env file found!")

# ═══════════════════════════════════════════════════════════
# Добавляем src в path для импорта моделей
# ═══════════════════════════════════════════════════════════
sys.path.insert(0, str(src_dir))

from db.models import Base  # noqa: E402

# Alembic Config
config = context.config

# Logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Метаданные моделей для автогенерации
target_metadata = Base.metadata


def get_url() -> str:
    """Получает URL базы из переменных окружения."""
    user = os.getenv("POSTGRES_USER", "trader")
    password = os.getenv("POSTGRES_PASSWORD", "")
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    db = os.getenv("POSTGRES_DB", "trading_bot")
    
    # Для отладки
    print(f"🔌 Connecting to: {host}:{port}/{db} as {user}")
    
    url = f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{db}"
    return url


def run_migrations_offline() -> None:
    """Миграции в offline режиме (генерация SQL)."""
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Запуск миграций с подключением."""
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Async миграции."""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = get_url()
    
    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Миграции в online режиме."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()