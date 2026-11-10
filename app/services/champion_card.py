"""
Генерация карточки чемпиона (PNG):
1. AI-фон через Pollinations
2. HTML-шаблон с таблицей поверх фона
3. Рендер в PNG через Playwright
"""
import uuid
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from playwright.async_api import async_playwright
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.tournament import Tournament
from app.models.tournament_standing import TournamentStanding
from app.services.ai_background import get_ai_background
from app.services.stats import StatsService


BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE_DIR / "templates"
UPLOAD_DIR = BASE_DIR / "static" / "uploads" / "champions"


async def generate_champion_card(
    db: AsyncSession,
    tournament_id: int,
    prompt_index: int = 0,
) -> str:
    """
    Возвращает относительный URL готовой карточки:
    /static/uploads/champions/<uuid>.png
    """
    # 1. Данные турнира и таблицы
    tournament = await db.get(Tournament, tournament_id)
    if not tournament:
        raise ValueError(f"Турнир {tournament_id} не найден")

    stmt = (
        select(TournamentStanding)
        .where(TournamentStanding.tournament_id == tournament_id)
        .options(selectinload(TournamentStanding.team))
        .order_by(
            TournamentStanding.points.desc(),
            (TournamentStanding.goals_for - TournamentStanding.goals_against).desc(),
            TournamentStanding.goals_for.desc(),
        )
    )
    standings = (await db.execute(stmt)).scalars().all()

    if not standings:
        raise ValueError("В турнире нет таблицы — нечего рендерить")

    # 2. AI-фон (кешируется внутри)
    background_url = await get_ai_background(prompt_index)
    # Делаем абсолютный URL, чтобы Playwright подтянул его при set_content
    background_abs = f"http://localhost:8000{background_url}"

    # 3. Топ-бомбардир
    scorers = await StatsService.get_top_scorers(db, tournament_id, limit=1)
    top_scorer = scorers[0] if scorers else None

    # 4. Рендер HTML
    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)))
    template = env.get_template("champion_card.html")
    html = template.render(
        tournament=tournament,
        standings=standings,
        champion=standings[0].team,
        top_scorer=top_scorer,
        background_url=background_abs,  # абсолютный для Playwright
    )

    # 5. Скриншот через Playwright
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex}.png"
    file_path = UPLOAD_DIR / filename

    async with async_playwright() as p:
        browser = await p.chromium.launch(args=["--no-sandbox"])
        page = await browser.new_page(viewport={"width": 1400, "height": 1000})
        await page.set_content(html, wait_until="networkidle")
        # Дать шрифтам/фоновой картинке догрузиться
        await page.wait_for_timeout(1500)
        await page.locator(".card").screenshot(path=str(file_path))
        await browser.close()

    return f"/static/uploads/champions/{filename}"