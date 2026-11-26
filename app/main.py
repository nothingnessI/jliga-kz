
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.v1.api import api_router
from app.api.v1 import pages
from app.api.v1.endpoints.coach import router as coach_router

from app.web.broadcast import router as broadcast_web_router
from app.web.admin_users import router as admin_users_router
from app.web.rating import router as rating_router


BASE_DIR = Path(__file__).resolve().parent


app = FastAPI(
    title="J-Liga",
    description="Официальный сайт футбольной лиги",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://jliga.kz",
        "https://www.jliga.kz",
    ],
    allow_credentials=True,
    allow_methods=[
        "GET",
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
    ],
    allow_headers=[
        "Authorization",
        "Content-Type",
    ],
)


app.mount(
    "/static",
    StaticFiles(
        directory=BASE_DIR / "static",
    ),
    name="static",
)


# Основной JSON API.
app.include_router(
    api_router,
    prefix="/api/v1",
)


# API регистрации и работы тренеров.
app.include_router(
    coach_router,
    prefix="/api/v1/coach",
    tags=["Coach"],
)


# Jinja-страницы сайта.
app.include_router(
    pages.router,
)


# Центр трансляций, OBS и LIVE.
app.include_router(
    broadcast_web_router,
)


# Управление пользователями.
app.include_router(
    admin_users_router,
)

app.include_router(
    rating_router
)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8000,
    )
