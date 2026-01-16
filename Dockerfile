FROM python:3.11-slim

WORKDIR /app

# Установка системных зависимостей
# Добавляем git для установки из GitLab
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    git \
    && rm -rf /var/lib/apt/lists/*

# Копируем requirements и устанавливаем зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код
COPY src/ ./src/

# Рабочая директория
WORKDIR /app/src

# Healthcheck
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s \
    CMD python -c "import sys; sys.exit(0)"

# Запуск
CMD ["python", "main.py"]