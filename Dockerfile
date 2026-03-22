# ─────────────────────────────────────────────────────────────
# Telegram Task & Habit Tracker Bot — Dockerfile
# ─────────────────────────────────────────────────────────────
FROM python:3.11-slim

WORKDIR /app

# Системные зависимости:
#   gcc/g++  — для компиляции numpy (нужен timezonefinder)
#   tzdata   — данные часовых поясов
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        g++ \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

# Установка Python-зависимостей (кэшируется отдельным слоем)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

# Копируем исходный код
COPY bot.py .

# Создаём директорию для базы данных
RUN mkdir -p data

# Монтируй сюда volume чтобы БД сохранялась между перезапусками:
#   docker run -v $(pwd)/data:/app/data ...
VOLUME ["/app/data"]

# Переменные окружения передаются через --env-file при запуске.
# .env НЕ копируется в образ — передавай снаружи:
#   docker run --env-file .env -v $(pwd)/data:/app/data task_bot
#   docker-compose автоматически читает .env из папки проекта

CMD ["python", "-u", "bot.py"]
