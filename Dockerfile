# Используем легкий образ Python
FROM python:3.12-slim

# Устанавливаем рабочую директорию
WORKDIR /app

# Устанавливаем системные зависимости для работы с Postgres и компиляции пакетов
RUN apt-get update && apt-get install -y \
    libpq-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Копируем зависимости и устанавливаем их
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install --with-deps chromium

# Копируем весь проект
COPY . .

# Создаем папки для загрузок (чтобы не было ошибок, если их нет)
RUN mkdir -p app/static/uploads/external/teams app/static/uploads/external/players

# Открываем порт
EXPOSE 8000

# Запускаем миграции и сервер
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]