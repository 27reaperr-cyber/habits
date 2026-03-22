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

# Копируем .env если есть (можно передавать через -e или --env-file)
COPY .env* ./

# Создаём директорию для базы данных
RUN mkdir -p data

# Монтируй сюда volume чтобы БД сохранялась между перезапусками:
#   docker run -v $(pwd)/data:/app/data ...
VOLUME ["/app/data"]

CMD ["python", "-u", "bot.py"]
