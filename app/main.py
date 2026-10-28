from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

# Импортируем наши роутеры
from app.api.v1.api import api_router
from app.api.v1 import pages

# Определяем базовую директорию (папка app)
BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(
    title="J-Liga",
    description="Официальный сайт футбольной лиги",
    version="1.0.0",
)

# Настройка CORS (чтобы фронтенд мог делать запросы)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # В продакшене заменить на ["https://jliga.kz"]
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Подключаем папку со статикой (картинки, css, js)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

# 1. Подключаем API (эндпоинты для Swagger / JSON)
app.include_router(api_router, prefix="/api/v1")

# 2. Подключаем HTML-страницы (Jinja2)
app.include_router(pages.router)

# Запуск сервера (для запуска через python app/main.py)
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)