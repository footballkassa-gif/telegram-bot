# Telegram Post Bot — Railway Deploy

## Быстрый деплой на Railway (бесплатно)

### 1. Создай GitHub репозиторий
1. Зайди на github.com → New repository
2. Назови `telegram-bot` → Create
3. Загрузи три файла: `bot.py`, `requirements.txt`, `Procfile`

### 2. Задеплой на Railway
1. Зайди на railway.app → Login with GitHub
2. New Project → Deploy from GitHub repo → выбери `telegram-bot`
3. Подожди 1-2 минуты

### 3. Переменные окружения (Variables)
В Railway → твой проект → Variables → добавь:

| Ключ | Значение |
|------|----------|
| `TELEGRAM_BOT_TOKEN` | твой токен бота |
| `OPENROUTER_API_KEY` | твой ключ OpenRouter |

### Готово!
Бот работает 24/7 без компьютера.
