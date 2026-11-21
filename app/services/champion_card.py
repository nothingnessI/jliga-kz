
"""Генерация PNG-карточки чемпиона J-LIGA через Jinja2 и Playwright."""

import logging
import os
import uuid
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote, urlsplit

from jinja2 import Environment, FileSystemLoader, select_autoescape
from playwright.async_api import async_playwright
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.tournament import Tournament
from app.models.tournament_standing import TournamentStanding
from app.services.ai_background import get_ai_background
from app.services.stats import StatsService


logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE_DIR / "templates"
UPLOAD_DIR = BASE_DIR / "static" / "uploads" / "champions"

SERVER_URL = os.getenv(
    "JLIGA_SERVER_URL",
    "http://localhost:8000",
).rstrip("/")


def make_absolute_url(
    url: str | None,
    team_name: str = "",
) -> str:
    """Возвращает URL изображения, доступный Playwright."""

    if not url or not url.strip():
        return (
            "https://ui-avatars.com/api/"
            f"?name={quote(team_name or 'Team', safe='')}"
            "&background=0f2537&color=fff&size=128&rounded=true"
        )

    url = url.strip()
    parsed = urlsplit(url)

    if parsed.scheme in ("http", "https") and parsed.netloc:
        return url

    if url.startswith("//") or parsed.scheme or parsed.netloc:
        raise ValueError("Недопустимый URL изображения")

    return f"{SERVER_URL}/{url.lstrip('/')}"


def _view(obj):
    """Копирует поля ORM-объекта без изменения данных БД."""
    return SimpleNamespace(
        **{
            column.key: getattr(obj, column.key)
            for column in obj.__mapper__.columns
        }
    )


async def generate_champion_card(
    db: AsyncSession,
    tournament_id: int,
    prompt_index: int = 0,
) -> str:
    """
    Создаёт PNG-карточку чемпиона.

    Возвращает относительный URL:
    /static/uploads/champions/<uuid>.png
    """

    tournament = await db.get(Tournament, tournament_id)

    if tournament is None:
        raise ValueError(f"Турнир {tournament_id} не найден")

    stmt = (
        select(TournamentStanding)
        .where(
            TournamentStanding.tournament_id == tournament_id
        )
        .options(
            selectinload(TournamentStanding.team)
        )
        .order_by(
            TournamentStanding.points.desc(),
            (
                TournamentStanding.goals_for
                - TournamentStanding.goals_against
            ).desc(),
            TournamentStanding.goals_for.desc(),
            TournamentStanding.team_id.asc(),
        )
    )

    standings = (await db.scalars(stmt)).all()

    if not standings:
        raise ValueError(
            "В турнире нет турнирной таблицы"
        )

    # Создаём копии данных для шаблона.
    # Исходные SQLAlchemy-объекты не изменяем.

    rendered_standings = []

    for standing in standings:
        standing_view = _view(standing)
        team = standing.team

        if team is not None:
            team_view = _view(team)

            team_view.logo_url = make_absolute_url(
                team.logo_url,
                team.name,
            )
        else:
            team_view = None

        standing_view.team = team_view
        rendered_standings.append(standing_view)

    champion = rendered_standings[0].team

    if champion is None:
        raise ValueError(
            "У лидера турнирной таблицы не найдена команда"
        )

    # Загружаем AI-фон.

    background_url = await get_ai_background(
        prompt_index
    )
    background_abs = make_absolute_url(
        background_url
    )

    # Получаем лучшего бомбардира.

    scorers = await StatsService.get_top_scorers(
        db,
        tournament_id,
        limit=1,
    )

    top_scorer = scorers[0] if scorers else None

    # Генерируем HTML.

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
    )

    template = env.get_template(
        "champion_card.html"
    )

    html = template.render(
        tournament=tournament,
        standings=rendered_standings,
        champion=champion,
        top_scorer=top_scorer,
        background_url=background_abs,
    )

    # Создаём изображение.

    UPLOAD_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    filename = f"{uuid.uuid4().hex}.png"
    file_path = UPLOAD_DIR / filename

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                args=["--no-sandbox"]
            )

            try:
                page = await browser.new_page(
                    viewport={
                        "width": 1400,
                        "height": 1000,
                    },
                    device_scale_factor=1,
                )

                await page.set_content(
                    html,
                    wait_until="load",
                    timeout=30000,
                )

                # Ждём загрузки шрифтов.
                await page.evaluate(
                    "document.fonts.ready"
                )

                # Ждём загрузки изображений.
                await page.evaluate(
                    """async () => {
                        await Promise.all(
                            Array.from(document.images).map(img => {
                                if (img.complete) {
                                    return Promise.resolve();
                                }

                                return new Promise(resolve => {
                                    img.addEventListener(
                                        'load',
                                        resolve,
                                        {once: true}
                                    );

                                    img.addEventListener(
                                        'error',
                                        resolve,
                                        {once: true}
                                    );
                                });
                            })
                        );
                    }"""
                )

                card = page.locator(".card")

                await card.wait_for(
                    state="visible",
                    timeout=15000,
                )

                await card.screenshot(
                    path=str(file_path),
                    timeout=30000,
                )

            finally:
                await browser.close()

    except Exception:
        file_path.unlink(missing_ok=True)

        logger.exception(
            "Ошибка создания карточки чемпиона "
            "турнира %s",
            tournament_id,
        )

        raise

    logger.info(
        "Карточка чемпиона создана: %s",
        file_path,
    )

    return (
        f"/static/uploads/champions/{filename}"
    )
