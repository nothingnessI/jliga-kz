import uvicorn
from fastapi import FastAPI
from pathlib import Path

from  fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

from fastapi.middleware.cors import CORSMiddleware

from contextlib import asynccontextmanager
from db import engine
from core import settings, Base

from app.api.v1.api import api_router

from app.api.v1 import pages


STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield

    await engine.dispose()


app = FastAPI(
    title=settings.APP_NAME,
    lifespan=lifespan
)


app.include_router(api_router)
app.include_router(pages.router)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


templates = Jinja2Templates(directory="app/templates")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=settings.APP_HOST,
        port=settings.APP_PORT,
    )