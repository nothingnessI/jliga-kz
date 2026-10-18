from pathlib import Path
from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy import select
from app.crud.stadium import stadium as crud_stadium
from app.db.session import get_db
from app.models.tournament import Tournament
from app.models.tournament_standing import TournamentStanding
from app.models.match import Match
from app.models.match_event import MatchEvent
from app.models.news import News
from app.models.team import Team
from app.models.player import Player
from app.models.enums import MatchStatus, TournamentStatus
from app.services.standings import StandingsService
from app.services.stats import StatsService

router = APIRouter()
BASE_DIR = Path(__file__).resolve().parent.parent.parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")


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

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "tournaments": tournaments, "last_matches": last_matches,
            "upcoming_matches": upcoming_matches, "latest_news": latest_news,
            "standings": standings, "top_scorers": top_scorers, "active_tournament": active_tournament
        }
    )


@router.get("/about")
async def about_page(request: Request):
    """Страница О лиге."""
    return templates.TemplateResponse(request=request, name="about.html")


@router.get("/tournaments")
async def tournaments_list_page(request: Request, db: AsyncSession = Depends(get_db)):
    """Страница всех турниров."""
    result = await db.execute(select(Tournament).order_by(Tournament.start_at.desc()))
    tournaments = result.scalars().all()
    return templates.TemplateResponse(request=request, name="tournaments.html", context={"tournaments": tournaments})


@router.get("/tournaments/{tournament_id}")
async def tournament_page(tournament_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """Страница конкретного турнира."""
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

    return templates.TemplateResponse(request=request, name="tournament_detail.html",
                                      context={"tournament": tournament, "standings": standings, "matches": matches})


@router.get("/participants")
async def participants_page(request: Request, db: AsyncSession = Depends(get_db)):
    """Страница участников (команды)."""
    teams = (await db.execute(select(Team).order_by(Team.name))).scalars().all()
    return templates.TemplateResponse(request=request, name="participants.html", context={"teams": teams})


@router.get("/news")
async def news_page(request: Request, db: AsyncSession = Depends(get_db)):
    """Страница всех новостей."""
    news_list = (await db.execute(
        select(News).where(News.is_published == True).order_by(News.published_at.desc()))).scalars().all()
    return templates.TemplateResponse(request=request, name="news.html", context={"news_list": news_list})


@router.get("/media")
async def media_page(request: Request):
    """Страница медиа (фото/видео)."""
    return templates.TemplateResponse(request=request, name="media.html")


@router.get("/matches/{match_id}")
async def match_protocol_page(match_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """Протокол матча."""
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
    return templates.TemplateResponse(request=request, name="match_detail.html", context={"match": match})


@router.get("/stadiums")
async def stadiums_page(request: Request, db: AsyncSession = Depends(get_db)):
    """Страница со списком стадионов."""
    # Используем CRUD для получения всех стадионов
    from app.crud.stadium import stadium as crud_stadium
    stadiums = await crud_stadium.get_multi(db, limit=100)

    return templates.TemplateResponse(
        request=request,
        name="stadiums.html",
        context={"stadiums": stadiums}
    )

@router.get("/management")
async def management_page(request: Request):
    """Страница руководителей лиги."""
    return templates.TemplateResponse(
        request=request,
        name="managment.html"
    )


@router.get("/partners")
async def partners_page(request: Request):
    """Страница партнеров лиги."""
    return templates.TemplateResponse(
        request=request,
        name="partners.html"
    )