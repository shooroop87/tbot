# 🚀 Деплой Trading Bot через GitHub Actions

## Обзор

```
[Push в main] → [Тесты] → [Build Docker] → [Push DockerHub] → [Deploy SSH] → [Telegram ✅]
```

**Гарантии безопасности:**
- ✅ База данных PostgreSQL **НЕ перезаписывается** (named volume)
- ✅ Graceful shutdown бота (60 сек на завершение)
- ✅ Секреты только в GitHub Secrets
- ✅ Docker образ в private репозитории

---

## 1. Настройка GitHub Secrets

Перейди в **Settings → Secrets and variables → Actions** и добавь:

### Docker Hub
| Secret | Описание |
|--------|----------|
| `DOCKER_USERNAME` | Логин Docker Hub |
| `DOCKER_TOKEN` | Access Token (не пароль!) |

**Как получить Docker Token:**
1. https://hub.docker.com/settings/security
2. New Access Token → Read & Write

### SSH доступ к серверу
| Secret | Описание |
|--------|----------|
| `HOST` | IP или домен сервера |
| `USER` | SSH пользователь (например `deploy`) |
| `SSH_KEY` | Приватный ключ (всё содержимое файла) |
| `SSH_PASSPHRASE` | Пароль ключа (если есть) |

**Генерация ключа:**
```bash
ssh-keygen -t ed25519 -C "github-actions" -f ~/.ssh/github_deploy
# Публичный ключ добавить на сервер:
cat ~/.ssh/github_deploy.pub >> ~/.ssh/authorized_keys
# Приватный ключ скопировать в GitHub Secret SSH_KEY
cat ~/.ssh/github_deploy
```

### Telegram уведомления
| Secret | Описание |
|--------|----------|
| `TELEGRAM_TO` | Chat ID для уведомлений |
| `TELEGRAM_BOT_TOKEN` | Токен бота (от @BotFather) |

---

## 2. Подготовка сервера

### Установка Docker
```bash
# Ubuntu/Debian
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER

# Перелогиниться
exit
```

### Создание директории
```bash
mkdir -p ~/trading_bot
cd ~/trading_bot
```

### Создание .env файла
```bash
nano .env
```

```env
# Docker
DOCKER_IMAGE=your_username/trading_bot
DOCKER_TAG=latest

# Tinkoff
TINKOFF_TOKEN=your_real_token_here
TINKOFF_ACCOUNT_ID=

# Telegram
TELEGRAM_BOT_TOKEN=5855003660:AAEr4gXXwv_S45aTjK68-imsZ8sfBl7jR9Y
TELEGRAM_CHAT_ID=771081107

# PostgreSQL
POSTGRES_DB=trading_bot
POSTGRES_USER=trader
POSTGRES_PASSWORD=SuperSecurePassword123!

# Trading
DEPOSIT_RUB=1000000
RISK_PER_TRADE_PCT=0.01
MAX_POSITION_PCT=0.25
```

```bash
chmod 600 .env
```

---

## 3. Первый запуск (вручную)

```bash
cd ~/trading_bot

# Скопировать docker-compose.production.yml и config.yaml на сервер
# (или дождаться первого деплоя через GitHub Actions)

# Запуск
docker compose -f docker-compose.production.yml up -d

# Проверка
docker compose -f docker-compose.production.yml ps
docker compose -f docker-compose.production.yml logs -f bot
```

---

## 4. Workflow GitHub Actions

После настройки секретов, каждый push в `main` автоматически:

1. **tests** — Проверка кода
2. **build_and_push** — Сборка и пуш Docker образа
3. **deploy** — SSH на сервер:
   - Pull нового образа
   - Graceful restart бота
   - Миграции БД
   - Health check
4. **notify** — Telegram уведомление

### Ручной запуск
Actions → Trading Bot CI/CD → Run workflow

---

## 5. Команды на сервере

```bash
cd ~/trading_bot

# Статус
docker compose -f docker-compose.production.yml ps

# Логи в реальном времени
docker compose -f docker-compose.production.yml logs -f bot

# Перезапуск бота (без потери данных)
docker compose -f docker-compose.production.yml restart bot

# Остановка
docker compose -f docker-compose.production.yml stop

# Полная остановка (БД тоже)
docker compose -f docker-compose.production.yml down

# ⚠️ ОПАСНО: Удаление всего включая данные
# docker compose -f docker-compose.production.yml down -v

# Немедленный расчёт
docker compose -f docker-compose.production.yml exec bot python main.py --now --once
```

---

## 6. Бэкап базы данных

### Создание бэкапа
```bash
docker compose -f docker-compose.production.yml exec postgres \
  pg_dump -U trader trading_bot > backup_$(date +%Y%m%d_%H%M%S).sql
```

### Восстановление
```bash
docker compose -f docker-compose.production.yml exec -T postgres \
  psql -U trader trading_bot < backup_20241220_120000.sql
```

### Автоматический бэкап (cron)
```bash
crontab -e
```
```
# Бэкап каждый день в 3:00
0 3 * * * cd ~/trading_bot && docker compose -f docker-compose.production.yml exec -T postgres pg_dump -U trader trading_bot > ~/backups/trading_$(date +\%Y\%m\%d).sql
```

---

## 7. Мониторинг

### Проверка здоровья
```bash
# Статус контейнеров
docker compose -f docker-compose.production.yml ps

# Использование ресурсов
docker stats --no-stream

# Место на диске
docker system df
```

### Логи
```bash
# Последние 100 строк
docker compose -f docker-compose.production.yml logs --tail=100 bot

# Ошибки
docker compose -f docker-compose.production.yml logs bot 2>&1 | grep -i error
```

---

## 8. Troubleshooting

### Бот не запускается
```bash
# Проверить логи
docker compose -f docker-compose.production.yml logs bot

# Проверить .env
cat .env | grep -v PASSWORD

# Проверить доступ к БД
docker compose -f docker-compose.production.yml exec postgres psql -U trader -d trading_bot -c "SELECT 1"
```

### Нет Telegram уведомлений
```bash
# Тест отправки
curl -X POST "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/sendMessage" \
  -d "chat_id=$TELEGRAM_CHAT_ID" \
  -d "text=Test from server"
```

### База данных повреждена
```bash
# Восстановить из бэкапа
docker compose -f docker-compose.production.yml down
docker volume rm trading_postgres_data
docker compose -f docker-compose.production.yml up -d postgres
# Восстановить бэкап...
docker compose -f docker-compose.production.yml up -d bot
```

---

## 9. Безопасность

### Firewall
```bash
# Разрешить только SSH и HTTP/HTTPS
sudo ufw allow 22
sudo ufw allow 80
sudo ufw allow 443
sudo ufw enable
```

### Права на файлы
```bash
chmod 600 .env
chmod 644 docker-compose.production.yml
chmod 644 config.yaml
```

### Обновления
```bash
# Регулярно обновлять систему
sudo apt update && sudo apt upgrade -y

# Обновить Docker образы
docker compose -f docker-compose.production.yml pull
docker compose -f docker-compose.production.yml up -d
```

---

**⚠️ Торговля на бирже несёт риск потери капитала.**
