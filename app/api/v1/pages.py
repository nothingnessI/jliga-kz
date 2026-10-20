from pathlib import Path
from datetime import datetime
from fastapi import APIRouter, Request, Depends, HTTPException, Form
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy import select, func
from jose import jwt, JWTError

from app.db.session import get_db
from app.core.security import create_access_token, verify_password, ALGORITHM
from app.core.config import settings

from app.models.tournament import Tournament
from app.models.tournament_standing import TournamentStanding
from app.models.match import Match
from app.models.match_event import MatchEvent
from app.models.news import News
from app.models.team import Team
from app.models.player import Player
from app.models.user import User
from app.models.enums import MatchStatus, TournamentStatus, UserRole
from app.services.standings import StandingsService
from app.services.stats import StatsService
from app.crud.user import user as crud_user
from app.crud.stadium import stadium as crud_stadium

router = APIRouter()
BASE_DIR = Path(__file__).resolve().parent.parent.parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")


# ==========================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==========================================

async def get_mega_menu_context(db: AsyncSession):
    """Собирает данные для мега-меню (строго текущий год)."""
    current_year = str(datetime.now().year)

    # Берем ТОЛЬКО турниры текущего года
    stmt = select(Tournament).where(Tournament.season == current_year)
    tournaments = (await db.execute(stmt)).scalars().all()

    championship = []
    champions_league = []
    junior = []

    for t in tournaments:
        name_upper = t.name.upper()
        div_upper = t.division.upper() if t.division else ""
        age_upper = t.age_category.upper() if t.age_category else ""

        # Распределяем по колонкам
        if "ЛЛЧ" in div_upper or "CHAMPIONS" in div_upper or "ЛИГА ЧЕМПИОНОВ" in name_upper:
            champions_league.append(t)
        elif age_upper == "ADULTS" or not age_upper:
            championship.append(t)
        else:
            junior.append(t)

    return {
        "menu_championship": championship,
        "menu_champions_league": champions_league,
        "menu_junior": junior,
        "current_year": current_year
    }


async def render_page(request: Request, db: AsyncSession, name: str, context: dict = None):
    """Обертка для рендера: всегда подкидывает данные меню и проверяет куки."""
    if context is None:
        context = {}

    menu_ctx = await get_mega_menu_context(db)
    context.update(menu_ctx)

    token = request.cookies.get("access_token")
    current_user = None
    if token:
        try:
            payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
            email = payload.get("sub")
            if email:
                current_user = await crud_user.get_by_email(db, email=email)
        except JWTError:
            pass

    context["current_user"] = current_user
    context["user"] = current_user  # Для шаблонов админки

    return templates.TemplateResponse(request=request, name=name, context=context)


# ==========================================
# ПУБЛИЧНЫЕ СТРАНИЦЫ
# ==========================================

@router.get("/")
async def home_page(request: Request, db: AsyncSession = Depends(get_db)):
    """Главная страница (Дашборд)."""
    stmt_tournaments = select(Tournament).where(
        Tournament.status.in_([TournamentStatus.ACTIVE, TournamentStatus.ACCEPTING_APPLICATIONS])
    ).order_by(Tournament.start_at.desc())
    tournaments = (await db.execute(stmt_tournaments)).scalars().all()

    stmt_last_matches = select(Match).where(
        Match.status == MatchStatus.FINISHED
    ).options(
        selectinload(Match.home_team), selectinload(Match.away_team), selectinload(Match.tournament)
    ).order_by(Match.match_date.desc()).limit(4)
    last_matches = (await db.execute(stmt_last_matches)).scalars().all()

    stmt_upcoming_matches = select(Match).where(
        Match.status == MatchStatus.SCHEDULED
    ).options(
        selectinload(Match.home_team), selectinload(Match.away_team), selectinload(Match.tournament)
    ).order_by(Match.match_date.asc()).limit(4)
    upcoming_matches = (await db.execute(stmt_upcoming_matches)).scalars().all()

    stmt_news = select(News).where(News.is_published == True).order_by(News.published_at.desc()).limit(3)
    latest_news = (await db.execute(stmt_news)).scalars().all()

    standings = []
    top_scorers = []
    active_tournament = tournaments[0] if tournaments else None

    if active_tournament:
        await StandingsService.recalculate_standings(db, active_tournament.id)
        stmt_standings = select(TournamentStanding).where(
            TournamentStanding.tournament_id == active_tournament.id
        ).options(selectinload(TournamentStanding.team)).order_by(TournamentStanding.points.desc()).limit(5)
        standings = (await db.execute(stmt_standings)).scalars().all()
        top_scorers = await StatsService.get_top_scorers(db, active_tournament.id, limit=5)

    return await render_page(request, db, "index.html", context={
        "tournaments": tournaments, "last_matches": last_matches,
        "upcoming_matches": upcoming_matches, "latest_news": latest_news,
        "standings": standings, "top_scorers": top_scorers, "active_tournament": active_tournament
    })


@router.get("/about")
async def about_page(request: Request, db: AsyncSession = Depends(get_db)):
    return await render_page(request, db, "about.html")


@router.get("/tournaments")
async def tournaments_list_page(request: Request, db: AsyncSession = Depends(get_db)):
    tournaments = (await db.execute(select(Tournament).order_by(Tournament.start_at.desc()))).scalars().all()
    return await render_page(request, db, "tournaments.html", context={"tournaments": tournaments})


@router.get("/tournaments/{tournament_id}")
async def tournament_page(tournament_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    tournament = await db.get(Tournament, tournament_id)
    if not tournament:
        raise HTTPException(status_code=404, detail="Турнир не найден")

    await StandingsService.recalculate_standings(db, tournament_id)

    stmt = select(TournamentStanding).where(TournamentStanding.tournament_id == tournament_id).options(
        selectinload(TournamentStanding.team)).order_by(TournamentStanding.points.desc(),
                                                        TournamentStanding.goals_for.desc())
    standings = (await db.execute(stmt)).scalars().all()

    stmt_matches = select(Match).where(Match.tournament_id == tournament_id).options(selectinload(Match.home_team),
                                                                                     selectinload(
                                                                                         Match.away_team)).order_by(
        Match.match_date)
    matches = (await db.execute(stmt_matches)).scalars().all()

    return await render_page(request, db, "tournament_detail.html",
                             context={"tournament": tournament, "standings": standings, "matches": matches})


@router.get("/participants/teams")
async def participants_teams_page(request: Request, db: AsyncSession = Depends(get_db)):
    teams = (await db.execute(select(Team).order_by(Team.name))).scalars().all()
    return await render_page(request, db, "participants.html", context={"teams": teams})


@router.get("/participants/players")
async def participants_players_page(request: Request, db: AsyncSession = Depends(get_db)):
    players = (await db.execute(select(Player).order_by(Player.last_name))).scalars().all()
    return await render_page(request, db, "players_list.html", context={"players": players})


@router.get("/participants/coaches")
async def participants_coaches_page(request: Request, db: AsyncSession = Depends(get_db)):
    return await render_page(request, db, "coaches.html")


@router.get("/participants/hall-of-fame")
async def participants_hall_page(request: Request, db: AsyncSession = Depends(get_db)):
    return await render_page(request, db, "hall_of_fame.html")


@router.get("/news")
async def news_page(request: Request, db: AsyncSession = Depends(get_db)):
    news_list = (await db.execute(
        select(News).where(News.is_published == True).order_by(News.published_at.desc()))).scalars().all()
    return await render_page(request, db, "news.html", context={"news_list": news_list})


@router.get("/media")
async def media_page(request: Request, db: AsyncSession = Depends(get_db)):
    return await render_page(request, db, "media.html")


@router.get("/matches/{match_id}")
async def match_protocol_page(match_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    stmt = select(Match).where(Match.id == match_id).options(
        selectinload(Match.home_team), selectinload(Match.away_team), selectinload(Match.tournament),
        selectinload(Match.events).selectinload(MatchEvent.player),
        selectinload(Match.events).selectinload(MatchEvent.team)
    )
    match = (await db.execute(stmt)).scalar_one_or_none()
    if not match:
        raise HTTPException(status_code=404, detail="Матч не найден")
    if match.events:
        match.events.sort(key=lambda e: e.minute)
    return await render_page(request, db, "match_detail.html", context={"match": match})


# --- СТРАНИЦЫ ИЗ МЕНЮ "О ЛИГЕ" ---

@router.get("/stadiums")
async def stadiums_page(request: Request, db: AsyncSession = Depends(get_db)):
    stadiums = await crud_stadium.get_multi(db, limit=100)
    return await render_page(request, db, "stadiums.html", context={"stadiums": stadiums})


@router.get("/management")
async def management_page(request: Request, db: AsyncSession = Depends(get_db)):
    return await render_page(request, db, "management.html")


@router.get("/partners")
async def partners_page(request: Request, db: AsyncSession = Depends(get_db)):
    return await render_page(request, db, "partners.html")


@router.get("/facts")
async def facts_page(request: Request, db: AsyncSession = Depends(get_db)):
    return await render_page(request, db, "facts.html")


@router.get("/rating")
async def rating_page(request: Request, db: AsyncSession = Depends(get_db)):
    return await render_page(request, db, "rating.html")


@router.get("/disciplinary")
async def disciplinary_page(request: Request, db: AsyncSession = Depends(get_db)):
    return await render_page(request, db, "disciplinary.html")


# ==========================================
# АВТОРИЗАЦИЯ И ПАНЕЛЬ УПРАВЛЕНИЯ (ТОЛЬКО ДЛЯ АДМИНА)
# ==========================================

@router.get("/login")
async def login_page(request: Request, db: AsyncSession = Depends(get_db)):
    return await render_page(request, db, "login.html")


@router.post("/web-login")
async def web_login(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        db: AsyncSession = Depends(get_db)
):
    user = await crud_user.get_by_email(db, email=username)

    # Проверяем пароль и то, что это именно АДМИН
    if not user or not verify_password(password, user.hashed_password) or user.role != UserRole.ADMIN:
        return RedirectResponse(url="/login?error=1", status_code=303)

    access_token = create_access_token(subject=user.email)
    # Перенаправляем в админку
    response = RedirectResponse(url="/admin", status_code=303)
    response.set_cookie(key="access_token", value=access_token, httponly=True, max_age=60 * 60 * 24 * 7)
    return response


@router.get("/logout")
async def logout():
    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie("access_token")
    return response


@router.get("/admin")
async def admin_panel(request: Request, db: AsyncSession = Depends(get_db)):
    token = request.cookies.get("access_token")
    if not token:
        return RedirectResponse(url="/login", status_code=303)

    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
        email = payload.get("sub")
        user = await crud_user.get_by_email(db, email=email)
        if not user or user.role != UserRole.ADMIN:
            return RedirectResponse(url="/login", status_code=303)
    except JWTError:
        return RedirectResponse(url="/login", status_code=303)

    users_count = await db.scalar(select(func.count()).select_from(User))
    news_count = await db.scalar(select(func.count()).select_from(News))

    return await render_page(request, db, "admin.html", context={
        "user": user, "users_count": users_count, "news_count": news_count
    })


@router.get("/admin/tournaments")
async def admin_tournaments(request: Request, db: AsyncSession = Depends(get_db)):
    token = request.cookies.get("access_token")
    if not token: return RedirectResponse(url="/login", status_code=303)

    tournaments = (await db.execute(select(Tournament).order_by(Tournament.created_at.desc()))).scalars().all()
    payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
    user = await crud_user.get_by_email(db, email=payload.get("sub"))

    return await render_page(request, db, "admin_tournaments.html", context={"user": user, "tournaments": tournaments})


@router.get("/admin/matches")
async def admin_matches(request: Request, db: AsyncSession = Depends(get_db)):
    token = request.cookies.get("access_token")
    if not token: return RedirectResponse(url="/login", status_code=303)
    payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
    user = await crud_user.get_by_email(db, email=payload.get("sub"))

    return await render_page(request, db, "admin_matches.html", context={"user": user})


@router.get("/admin/teams")
async def admin_teams(request: Request, db: AsyncSession = Depends(get_db)):
    token = request.cookies.get("access_token")
    if not token: return RedirectResponse(url="/login", status_code=303)
    payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
    user = await crud_user.get_by_email(db, email=payload.get("sub"))

    return await render_page(request, db, "admin_teams.html", context={"user": user})