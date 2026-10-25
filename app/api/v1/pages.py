from pathlib import Path
from datetime import datetime
import shutil
import uuid
from fastapi import APIRouter, Request, Depends, HTTPException, Form, UploadFile, File
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
from app.models.enums import MatchStatus, TournamentStatus, UserRole, TournamentFormat, PlayerPosition, MatchEventType
from app.models.media import MediaAlbum, MediaItem

from app.services.standings import StandingsService
from app.services.stats import StatsService
from app.services.match import MatchService

from app.crud.user import user as crud_user
from app.crud.stadium import stadium as crud_stadium
from app.crud.team import team as crud_team
from app.crud.tournament import tournament as crud_tournament
from app.crud.match import match as crud_match
from app.crud.player import player as crud_player
from app.crud.team_player import team_player as crud_team_player
from app.crud.match_event import match_event as crud_match_event
from app.crud.news import news as crud_news

from app.schemas.stadium import StadiumCreate, StadiumUpdate
from app.schemas.team import TeamCreate, TeamUpdate
from app.schemas.tournament import TournamentCreate, TournamentUpdate
from app.schemas.match import MatchCreate, MatchUpdate
from app.schemas.player import PlayerCreate, PlayerUpdate
from app.schemas.team_player import TeamPlayerCreate
from app.schemas.match_event import MatchEventCreate
from app.schemas.news import NewsCreate, NewsUpdate

router = APIRouter()
BASE_DIR = Path(__file__).resolve().parent.parent.parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def fix_logo_url(url: str | None) -> str | None:
    if url and url.startswith("/"):
        return f"https://jliga.ru{url}"
    return url


async def get_mega_menu_context(db: AsyncSession):
    latest_season_stmt = select(Tournament.season).order_by(Tournament.season.desc()).limit(1)
    latest_season_result = await db.execute(latest_season_stmt)
    current_year = latest_season_result.scalar() or str(datetime.now().year)

    stmt = select(Tournament).where(Tournament.season == current_year)
    tournaments = (await db.execute(stmt)).scalars().all()

    championship = []
    champions_league = []
    junior = []

    for t in tournaments:
        name_upper = t.name.upper()
        div_upper = t.division.upper() if t.division else ""
        age_upper = t.age_category.upper() if t.age_category else ""

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
    context["user"] = current_user

    return templates.TemplateResponse(request=request, name=name, context=context)


async def get_admin_from_cookie(request: Request, db: AsyncSession):
    token = request.cookies.get("access_token")
    if not token:
        return RedirectResponse(url="/login", status_code=303)
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
        user = await crud_user.get_by_email(db, email=payload.get("sub"))
        if not user or user.role != UserRole.ADMIN:
            return RedirectResponse(url="/login", status_code=303)
        return user
    except JWTError:
        return RedirectResponse(url="/login", status_code=303)


async def save_upload_file(upload_file: UploadFile, folder: str) -> str | None:
    if not upload_file or not upload_file.filename:
        return None

    ext = Path(upload_file.filename).suffix
    filename = f"{uuid.uuid4().hex}{ext}"

    upload_dir = BASE_DIR / "static" / "uploads" / folder
    upload_dir.mkdir(parents=True, exist_ok=True)

    file_path = upload_dir / filename
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(upload_file.file, buffer)

    return f"/static/uploads/{folder}/{filename}"


@router.get("/")
async def home_page(request: Request, db: AsyncSession = Depends(get_db)):
    stmt_tournaments = select(Tournament).where(
        Tournament.status.in_([TournamentStatus.ACTIVE, TournamentStatus.ACCEPTING_APPLICATIONS])
    ).order_by(Tournament.start_at.desc())
    tournaments = (await db.execute(stmt_tournaments)).scalars().all()

    for t in tournaments:
        t.logo_url = fix_logo_url(t.logo_url)

    stmt_last_matches = select(Match).where(
        Match.status == MatchStatus.FINISHED
    ).options(selectinload(Match.home_team), selectinload(Match.away_team), selectinload(Match.tournament)).order_by(
        Match.match_date.desc()).limit(4)
    last_matches = (await db.execute(stmt_last_matches)).scalars().all()

    stmt_upcoming_matches = select(Match).where(
        Match.status == MatchStatus.SCHEDULED
    ).options(selectinload(Match.home_team), selectinload(Match.away_team), selectinload(Match.tournament)).order_by(
        Match.match_date.asc()).limit(4)
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
async def tournaments_list_page(request: Request, season: str = None, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Tournament.season).distinct().order_by(Tournament.season.desc()))
    available_seasons = result.scalars().all()

    if not season and available_seasons:
        season = available_seasons[0]

    stmt = select(Tournament)
    if season:
        stmt = stmt.where(Tournament.season == season)

    tournaments = (await db.execute(stmt.order_by(Tournament.division, Tournament.name))).scalars().all()

    for t in tournaments:
        t.logo_url = fix_logo_url(t.logo_url)

    return await render_page(request, db, "tournaments.html", context={
        "tournaments": tournaments,
        "available_seasons": available_seasons,
        "current_season": season
    })


@router.get("/tournaments/{tournament_id}")
async def tournament_page(tournament_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    tournament = await db.get(Tournament, tournament_id)
    if not tournament:
        raise HTTPException(status_code=404, detail="Турнир не найден")

    tournament.logo_url = fix_logo_url(tournament.logo_url)

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
async def participants_teams_page(request: Request, page: int = 1, db: AsyncSession = Depends(get_db)):
    limit = 24
    skip = (page - 1) * limit

    total_teams = await db.scalar(select(func.count()).select_from(Team))
    stmt = select(Team).order_by(Team.name).offset(skip).limit(limit)
    teams = (await db.execute(stmt)).scalars().all()

    total_pages = (total_teams + limit - 1) // limit if total_teams > 0 else 1

    return await render_page(request, db, "participants.html", context={
        "teams": teams, "page": page, "total_pages": total_pages, "total_teams": total_teams
    })


@router.get("/participants/players")
async def participants_players_page(request: Request, page: int = 1, db: AsyncSession = Depends(get_db)):
    limit = 30
    skip = (page - 1) * limit

    total_players = await db.scalar(select(func.count()).select_from(Player))
    stmt = select(Player).order_by(Player.last_name).offset(skip).limit(limit)
    players = (await db.execute(stmt)).scalars().all()

    total_pages = (total_players + limit - 1) // limit if total_players > 0 else 1

    start_page = max(1, page - 2)
    end_page = min(total_pages, page + 2)

    return await render_page(request, db, "players_list.html", context={
        "players": players, "page": page, "total_pages": total_pages,
        "total_players": total_players, "start_page": start_page, "end_page": end_page
    })


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
    stmt = select(MediaAlbum).options(selectinload(MediaAlbum.items)).order_by(MediaAlbum.created_at.desc())
    albums = (await db.execute(stmt)).scalars().all()
    return await render_page(request, db, "media.html", context={"albums": albums})


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

    if not user or not verify_password(password, user.hashed_password) or user.role != UserRole.ADMIN:
        return RedirectResponse(url="/login?error=1", status_code=303)

    access_token = create_access_token(subject=user.email)
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
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    users_count = await db.scalar(select(func.count()).select_from(User))
    news_count = await db.scalar(select(func.count()).select_from(News))

    return await render_page(request, db, "admin.html", context={
        "user": user, "users_count": users_count, "news_count": news_count
    })


@router.get("/admin/stadiums")
async def admin_stadiums_list(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user
    stadiums = await crud_stadium.get_multi(db, limit=100)
    return await render_page(request, db, "admin_stadiums.html", context={"user": user, "stadiums": stadiums})


@router.get("/admin/stadiums/create")
async def admin_stadium_create_form(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user
    return await render_page(request, db, "admin_stadium_form.html", context={"user": user, "stadium": None})


@router.post("/admin/stadiums/create")
async def admin_stadium_create(
        request: Request, name: str = Form(...), city: str = Form(None), address: str = Form(None),
        capacity: int = Form(None), image: UploadFile = File(None), latitude: float = Form(None),
        longitude: float = Form(None), db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    image_url = await save_upload_file(image, "stadiums")
    stadium_in = StadiumCreate(name=name, city=city, address=address, capacity=capacity, image_url=image_url,
                               latitude=latitude, longitude=longitude)
    await crud_stadium.create(db, obj_in=stadium_in)
    return RedirectResponse(url="/admin/stadiums", status_code=303)


@router.get("/admin/stadiums/{stadium_id}/edit")
async def admin_stadium_edit_form(stadium_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user
    stadium = await crud_stadium.get(db, stadium_id)
    if not stadium: return RedirectResponse(url="/admin/stadiums", status_code=303)
    return await render_page(request, db, "admin_stadium_form.html", context={"user": user, "stadium": stadium})


@router.post("/admin/stadiums/{stadium_id}/edit")
async def admin_stadium_edit(
        stadium_id: int, request: Request, name: str = Form(...), city: str = Form(None), address: str = Form(None),
        capacity: int = Form(None), image: UploadFile = File(None), latitude: float = Form(None),
        longitude: float = Form(None), db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    stadium = await crud_stadium.get(db, stadium_id)
    if not stadium: return RedirectResponse(url="/admin/stadiums", status_code=303)

    new_image_url = await save_upload_file(image, "stadiums")
    final_image_url = new_image_url if new_image_url else stadium.image_url

    stadium_in = StadiumUpdate(name=name, city=city, address=address, capacity=capacity, image_url=final_image_url,
                               latitude=latitude, longitude=longitude)
    await crud_stadium.update(db, db_obj=stadium, obj_in=stadium_in)
    return RedirectResponse(url="/admin/stadiums", status_code=303)


@router.post("/admin/stadiums/{stadium_id}/delete")
async def admin_stadium_delete(stadium_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user
    stadium = await crud_stadium.get(db, stadium_id)
    if stadium: await crud_stadium.delete(db, db_obj=stadium)
    return RedirectResponse(url="/admin/stadiums", status_code=303)


@router.get("/admin/teams")
async def admin_teams_list(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user
    teams = (await db.execute(select(Team).order_by(Team.name))).scalars().all()
    return await render_page(request, db, "admin_teams.html", context={"user": user, "teams": teams})


@router.get("/admin/teams/create")
async def admin_team_create_form(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user
    return await render_page(request, db, "admin_team_form.html", context={"user": user, "team": None})


@router.post("/admin/teams/create")
async def admin_team_create(
        request: Request, name: str = Form(...), slug: str = Form(...), city: str = Form(None),
        coach_name: str = Form(None), logo: UploadFile = File(None), cover: UploadFile = File(None),
        is_active: bool = Form(False), db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    logo_url = await save_upload_file(logo, "teams")
    cover_url = await save_upload_file(cover, "teams")

    team_in = TeamCreate(
        name=name, slug=slug, city=city, coach_name=coach_name,
        logo_url=logo_url, cover_url=cover_url, is_active=is_active
    )
    await crud_team.create(db, obj_in=team_in)
    return RedirectResponse(url="/admin/teams", status_code=303)


@router.get("/admin/teams/{team_id}/edit")
async def admin_team_edit_form(team_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user
    team = await crud_team.get(db, team_id)
    if not team: return RedirectResponse(url="/admin/teams", status_code=303)
    return await render_page(request, db, "admin_team_form.html", context={"user": user, "team": team})


@router.post("/admin/teams/{team_id}/edit")
async def admin_team_edit(
        team_id: int, request: Request, name: str = Form(...), slug: str = Form(...), city: str = Form(None),
        coach_name: str = Form(None), logo: UploadFile = File(None), cover: UploadFile = File(None),
        is_active: bool = Form(False), db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    team = await crud_team.get(db, team_id)
    if not team: return RedirectResponse(url="/admin/teams", status_code=303)

    new_logo_url = await save_upload_file(logo, "teams")
    final_logo_url = new_logo_url if new_logo_url else team.logo_url

    new_cover_url = await save_upload_file(cover, "teams")
    final_cover_url = new_cover_url if new_cover_url else team.cover_url

    team_in = TeamUpdate(
        name=name, slug=slug, city=city, coach_name=coach_name,
        logo_url=final_logo_url, cover_url=final_cover_url, is_active=is_active
    )
    await crud_team.update(db, db_obj=team, obj_in=team_in)
    return RedirectResponse(url="/admin/teams", status_code=303)


@router.post("/admin/teams/{team_id}/delete")
async def admin_team_delete(team_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user
    team = await crud_team.get(db, team_id)
    if team: await crud_team.delete(db, db_obj=team)
    return RedirectResponse(url="/admin/teams", status_code=303)


@router.get("/admin/tournaments")
async def admin_tournaments_list(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user
    tournaments = (await db.execute(select(Tournament).order_by(Tournament.created_at.desc()))).scalars().all()
    return await render_page(request, db, "admin_tournaments.html", context={"user": user, "tournaments": tournaments})


@router.get("/admin/tournaments/create")
async def admin_tournament_create_form(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user
    return await render_page(request, db, "admin_tournament_form.html", context={
        "user": user, "tournament": None, "statuses": TournamentStatus, "formats": TournamentFormat
    })


@router.post("/admin/tournaments/create")
async def admin_tournament_create(
        request: Request, name: str = Form(...), description: str = Form(None), start_at: str = Form(None),
        end_at: str = Form(None), division: str = Form(...), season: str = Form(...), age_category: str = Form(...),
        status: TournamentStatus = Form(...), max_teams: int = Form(...),
        tournament_format: TournamentFormat = Form(None),
        logo: UploadFile = File(None), db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    logo_url = await save_upload_file(logo, "tournaments")
    start_date = datetime.fromisoformat(start_at) if start_at else None
    end_date = datetime.fromisoformat(end_at) if end_at else None

    tournament_in = TournamentCreate(
        name=name, description=description, start_at=start_date, end_at=end_date,
        division=division, season=season, age_category=age_category,
        status=status, max_teams=max_teams, logo_url=logo_url, tournament_format=tournament_format
    )
    await crud_tournament.create(db, obj_in=tournament_in)
    return RedirectResponse(url="/admin/tournaments", status_code=303)


@router.get("/admin/tournaments/{tournament_id}/edit")
async def admin_tournament_edit_form(tournament_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    tournament = await crud_tournament.get(db, tournament_id)
    if not tournament: return RedirectResponse(url="/admin/tournaments", status_code=303)

    return await render_page(request, db, "admin_tournament_form.html", context={
        "user": user, "tournament": tournament, "statuses": TournamentStatus, "formats": TournamentFormat
    })


@router.post("/admin/tournaments/{tournament_id}/edit")
async def admin_tournament_edit(
        tournament_id: int, request: Request, name: str = Form(...), description: str = Form(None),
        start_at: str = Form(None), end_at: str = Form(None), division: str = Form(...), season: str = Form(...),
        age_category: str = Form(...), status: TournamentStatus = Form(...), max_teams: int = Form(...),
        tournament_format: TournamentFormat = Form(None), logo: UploadFile = File(None),
        db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    tournament = await crud_tournament.get(db, tournament_id)
    if not tournament: return RedirectResponse(url="/admin/tournaments", status_code=303)

    new_logo_url = await save_upload_file(logo, "tournaments")
    final_logo_url = new_logo_url if new_logo_url else tournament.logo_url

    start_date = datetime.fromisoformat(start_at) if start_at else None
    end_date = datetime.fromisoformat(end_at) if end_at else None

    tournament_in = TournamentUpdate(
        name=name, description=description, start_at=start_date, end_at=end_date,
        division=division, season=season, age_category=age_category,
        status=status, max_teams=max_teams, logo_url=final_logo_url, tournament_format=tournament_format
    )
    await crud_tournament.update(db, db_obj=tournament, obj_in=tournament_in)
    return RedirectResponse(url="/admin/tournaments", status_code=303)


@router.post("/admin/tournaments/{tournament_id}/delete")
async def admin_tournament_delete(tournament_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    tournament = await crud_tournament.get(db, tournament_id)
    if tournament: await crud_tournament.delete(db, db_obj=tournament)
    return RedirectResponse(url="/admin/tournaments", status_code=303)


@router.post("/admin/tournaments/{tournament_id}/generate-calendar")
async def admin_generate_calendar(tournament_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    tournament = await crud_tournament.get(db, tournament_id)
    if not tournament:
        return RedirectResponse(url="/admin/tournaments", status_code=303)

    teams = (await db.execute(select(Team).where(Team.is_active == True))).scalars().all()
    if len(teams) < 2:
        return RedirectResponse(url="/admin/tournaments", status_code=303)

    team_ids = [t.id for t in teams]
    pairs = MatchService.generate_round_robin(team_ids)

    for home_id, away_id in pairs:
        match_in = MatchCreate(
            tournament_id=tournament_id, home_team_id=home_id, away_team_id=away_id,
            status=MatchStatus.SCHEDULED, round=1
        )
        await crud_match.create(db, obj_in=match_in)

    return RedirectResponse(url="/admin/tournaments", status_code=303)


@router.get("/admin/matches")
async def admin_matches_list(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    stmt = select(Match).options(
        selectinload(Match.home_team), selectinload(Match.away_team), selectinload(Match.tournament)
    ).order_by(Match.match_date.desc())
    matches = (await db.execute(stmt)).scalars().all()

    return await render_page(request, db, "admin_matches.html", context={"user": user, "matches": matches})


@router.get("/admin/matches/create")
async def admin_match_create_form(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    tournaments = (await db.execute(select(Tournament).order_by(Tournament.name))).scalars().all()
    teams = (await db.execute(select(Team).order_by(Team.name))).scalars().all()
    stadiums = await crud_stadium.get_multi(db, limit=100)

    return await render_page(request, db, "admin_match_form.html", context={
        "user": user, "match": None, "tournaments": tournaments,
        "teams": teams, "stadiums": stadiums, "statuses": MatchStatus
    })


@router.post("/admin/matches/create")
async def admin_match_create(
        request: Request, tournament_id: int = Form(...), home_team_id: int = Form(...),
        away_team_id: int = Form(...), match_date: str = Form(None), stadium: str = Form(None),
        status: MatchStatus = Form(...), round: int = Form(None), group_name: str = Form(None),
        db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    if home_team_id == away_team_id:
        raise HTTPException(status_code=400, detail="Команда не может играть сама с собой")

    parsed_date = datetime.fromisoformat(match_date) if match_date else None

    match_in = MatchCreate(
        tournament_id=tournament_id, home_team_id=home_team_id, away_team_id=away_team_id,
        match_date=parsed_date, stadium=stadium, status=status, round=round, group_name=group_name
    )
    await crud_match.create(db, obj_in=match_in)
    return RedirectResponse(url="/admin/matches", status_code=303)


@router.get("/admin/matches/{match_id}/edit")
async def admin_match_edit_form(match_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    match = await crud_match.get(db, match_id)
    if not match: return RedirectResponse(url="/admin/matches", status_code=303)

    tournaments = (await db.execute(select(Tournament).order_by(Tournament.name))).scalars().all()
    teams = (await db.execute(select(Team).order_by(Team.name))).scalars().all()
    stadiums = await crud_stadium.get_multi(db, limit=100)

    return await render_page(request, db, "admin_match_form.html", context={
        "user": user, "match": match, "tournaments": tournaments,
        "teams": teams, "stadiums": stadiums, "statuses": MatchStatus
    })


@router.post("/admin/matches/{match_id}/edit")
async def admin_match_edit(
        match_id: int, request: Request, tournament_id: int = Form(...), home_team_id: int = Form(...),
        away_team_id: int = Form(...), match_date: str = Form(None), stadium: str = Form(None),
        home_score: int = Form(None), away_score: int = Form(None), status: MatchStatus = Form(...),
        round: int = Form(None), group_name: str = Form(None), db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    match = await crud_match.get(db, match_id)
    if not match: return RedirectResponse(url="/admin/matches", status_code=303)

    if home_team_id == away_team_id:
        raise HTTPException(status_code=400, detail="Команда не может играть сама с собой")

    parsed_date = datetime.fromisoformat(match_date) if match_date else None

    match_in = MatchUpdate(
        tournament_id=tournament_id, home_team_id=home_team_id, away_team_id=away_team_id,
        match_date=parsed_date, stadium=stadium, home_score=home_score, away_score=away_score,
        status=status, round=round, group_name=group_name
    )
    await crud_match.update(db, db_obj=match, obj_in=match_in)

    if status == MatchStatus.FINISHED:
        await StandingsService.recalculate_standings(db, tournament_id)

    return RedirectResponse(url="/admin/matches", status_code=303)


@router.post("/admin/matches/{match_id}/delete")
async def admin_match_delete(match_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    match = await crud_match.get(db, match_id)
    if match: await crud_match.delete(db, db_obj=match)

    return RedirectResponse(url="/admin/matches", status_code=303)


@router.get("/admin/players")
async def admin_players_list(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    players = (await db.execute(select(Player).order_by(Player.last_name))).scalars().all()
    return await render_page(request, db, "admin_players.html", context={"user": user, "players": players})


@router.get("/admin/players/create")
async def admin_player_create_form(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user
    return await render_page(request, db, "admin_player_form.html", context={
        "user": user, "player": None, "positions": PlayerPosition
    })


@router.post("/admin/players/create")
async def admin_player_create(
        request: Request, first_name: str = Form(...), last_name: str = Form(...), date_of_birth: str = Form(None),
        position: PlayerPosition = Form(None), photo: UploadFile = File(None), db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    photo_url = await save_upload_file(photo, "players")
    parsed_date = datetime.strptime(date_of_birth, "%Y-%m-%d").date() if date_of_birth else None

    player_in = PlayerCreate(
        first_name=first_name, last_name=last_name, date_of_birth=parsed_date, position=position, photo_url=photo_url
    )
    await crud_player.create(db, obj_in=player_in)
    return RedirectResponse(url="/admin/players", status_code=303)


@router.get("/admin/players/{player_id}/edit")
async def admin_player_edit_form(player_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    player = await crud_player.get(db, player_id)
    if not player: return RedirectResponse(url="/admin/players", status_code=303)

    return await render_page(request, db, "admin_player_form.html", context={
        "user": user, "player": player, "positions": PlayerPosition
    })


@router.post("/admin/players/{player_id}/edit")
async def admin_player_edit(
        player_id: int, request: Request, first_name: str = Form(...), last_name: str = Form(...),
        date_of_birth: str = Form(None), position: PlayerPosition = Form(None),
        photo: UploadFile = File(None), db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    player = await crud_player.get(db, player_id)
    if not player: return RedirectResponse(url="/admin/players", status_code=303)

    new_photo_url = await save_upload_file(photo, "players")
    final_photo_url = new_photo_url if new_photo_url else player.photo_url

    parsed_date = datetime.strptime(date_of_birth, "%Y-%m-%d").date() if date_of_birth else None

    player_in = PlayerUpdate(
        first_name=first_name, last_name=last_name, date_of_birth=parsed_date, position=position,
        photo_url=final_photo_url
    )
    await crud_player.update(db, db_obj=player, obj_in=player_in)
    return RedirectResponse(url="/admin/players", status_code=303)


@router.post("/admin/players/{player_id}/delete")
async def admin_player_delete(player_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    player = await crud_player.get(db, player_id)
    if player: await crud_player.delete(db, db_obj=player)

    return RedirectResponse(url="/admin/players", status_code=303)


@router.get("/admin/teams/{team_id}/roster")
async def admin_team_roster(
        team_id: int, request: Request, tournament_id: int = None, db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    team = await crud_team.get(db, team_id)
    if not team: return RedirectResponse(url="/admin/teams", status_code=303)

    tournaments = (await db.execute(select(Tournament).order_by(Tournament.created_at.desc()))).scalars().all()
    all_players = (await db.execute(select(Player).order_by(Player.last_name))).scalars().all()

    roster = []
    if tournament_id:
        roster = await crud_team_player.get_by_tournament_and_team(db, tournament_id=tournament_id, team_id=team_id)

    return await render_page(request, db, "admin_roster.html", context={
        "user": user, "team": team, "tournaments": tournaments,
        "all_players": all_players, "roster": roster, "selected_tournament_id": tournament_id
    })


@router.post("/admin/teams/{team_id}/roster/add")
async def admin_team_roster_add(
        team_id: int, request: Request, tournament_id: int = Form(...), player_id: int = Form(...),
        jersey_number: int = Form(None), is_captain: bool = Form(False), db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    existing = await crud_team_player.get_by_tournament_and_team(db, tournament_id=tournament_id, team_id=team_id)
    if any(tp.player_id == player_id for tp in existing):
        return RedirectResponse(url=f"/admin/teams/{team_id}/roster?tournament_id={tournament_id}", status_code=303)

    obj_in = TeamPlayerCreate(
        team_id=team_id, tournament_id=tournament_id, player_id=player_id,
        jersey_number=jersey_number, is_captain=is_captain, is_active=True
    )
    await crud_team_player.create(db, obj_in=obj_in)

    return RedirectResponse(url=f"/admin/teams/{team_id}/roster?tournament_id={tournament_id}", status_code=303)


@router.post("/admin/roster/{roster_id}/delete")
async def admin_roster_delete(
        roster_id: int, request: Request, team_id: int = Form(...), tournament_id: int = Form(...),
        db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    roster_item = await crud_team_player.get(db, roster_id)
    if roster_item:
        await crud_team_player.delete(db, db_obj=roster_item)

    return RedirectResponse(url=f"/admin/teams/{team_id}/roster?tournament_id={tournament_id}", status_code=303)


async def recalculate_match_score(match: Match, db: AsyncSession):
    events = (await db.execute(select(MatchEvent).where(MatchEvent.match_id == match.id))).scalars().all()

    home_goals = 0
    away_goals = 0

    for e in events:
        if e.event_type in [MatchEventType.GOAL, MatchEventType.PENALTY_GOAL]:
            if e.team_id == match.home_team_id:
                home_goals += 1
            elif e.team_id == match.away_team_id:
                away_goals += 1
        elif e.event_type == MatchEventType.OWN_GOAL:
            if e.team_id == match.home_team_id:
                away_goals += 1
            elif e.team_id == match.away_team_id:
                home_goals += 1

    match.home_score = home_goals
    match.away_score = away_goals
    db.add(match)
    await db.commit()


@router.get("/admin/matches/{match_id}/events")
async def admin_match_events(match_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    match = await crud_match.get_with_teams(db, match_id=match_id)
    if not match: return RedirectResponse(url="/admin/matches", status_code=303)

    events = await crud_match_event.get_by_match(db, match_id=match_id)
    home_players = await crud_team_player.get_by_tournament_and_team(db, tournament_id=match.tournament_id,
                                                                     team_id=match.home_team_id)
    away_players = await crud_team_player.get_by_tournament_and_team(db, tournament_id=match.tournament_id,
                                                                     team_id=match.away_team_id)

    return await render_page(request, db, "admin_match_events.html", context={
        "user": user, "match": match, "events": events,
        "home_players": home_players, "away_players": away_players, "event_types": MatchEventType
    })


@router.post("/admin/matches/{match_id}/events/add")
async def admin_match_event_add(
        match_id: int, request: Request, team_id: int = Form(...), player_id: int = Form(...),
        event_type: MatchEventType = Form(...), minute: int = Form(...), comment: str = Form(None),
        db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    match = await db.get(Match, match_id)
    if not match: return RedirectResponse(url="/admin/matches", status_code=303)

    event_in = MatchEventCreate(
        match_id=match_id, team_id=team_id, player_id=player_id, event_type=event_type, minute=minute, comment=comment
    )
    await crud_match_event.create(db, obj_in=event_in)
    await recalculate_match_score(match, db)

    return RedirectResponse(url=f"/admin/matches/{match_id}/events", status_code=303)


@router.post("/admin/matches/{match_id}/events/{event_id}/delete")
async def admin_match_event_delete(match_id: int, event_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    event = await crud_match_event.get(db, event_id)
    if event:
        match = await db.get(Match, match_id)
        await crud_match_event.delete(db, db_obj=event)
        if match:
            await recalculate_match_score(match, db)

    return RedirectResponse(url=f"/admin/matches/{match_id}/events", status_code=303)


@router.get("/admin/news")
async def admin_news_list(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    news_list = (await db.execute(select(News).order_by(News.created_at.desc()))).scalars().all()
    return await render_page(request, db, "admin_news.html", context={"user": user, "news_list": news_list})


@router.get("/admin/news/create")
async def admin_news_create_form(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user
    return await render_page(request, db, "admin_news_form.html", context={"user": user, "news": None})


@router.post("/admin/news/create")
async def admin_news_create(
        request: Request, title: str = Form(...), slug: str = Form(...), content: str = Form(...),
        published_at: str = Form(None), is_published: bool = Form(False), image: UploadFile = File(None),
        db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    image_url = await save_upload_file(image, "news")
    parsed_date = datetime.fromisoformat(published_at) if published_at else datetime.now()

    news_in = NewsCreate(
        title=title, slug=slug, content=content, image_url=image_url, is_published=is_published,
        published_at=parsed_date
    )
    await crud_news.create(db, obj_in=news_in)
    return RedirectResponse(url="/admin/news", status_code=303)


@router.get("/admin/news/{news_id}/edit")
async def admin_news_edit_form(news_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    news_item = await crud_news.get(db, news_id)
    if not news_item: return RedirectResponse(url="/admin/news", status_code=303)

    return await render_page(request, db, "admin_news_form.html", context={"user": user, "news": news_item})


@router.post("/admin/news/{news_id}/edit")
async def admin_news_edit(
        news_id: int, request: Request, title: str = Form(...), slug: str = Form(...), content: str = Form(...),
        published_at: str = Form(None), is_published: bool = Form(False), image: UploadFile = File(None),
        db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    news_item = await crud_news.get(db, news_id)
    if not news_item: return RedirectResponse(url="/admin/news", status_code=303)

    new_image_url = await save_upload_file(image, "news")
    final_image_url = new_image_url if new_image_url else news_item.image_url
    parsed_date = datetime.fromisoformat(published_at) if published_at else news_item.published_at

    news_in = NewsUpdate(
        title=title, slug=slug, content=content, image_url=final_image_url,
        is_published=is_published, published_at=parsed_date
    )
    await crud_news.update(db, db_obj=news_item, obj_in=news_in)
    return RedirectResponse(url="/admin/news", status_code=303)


@router.post("/admin/news/{news_id}/delete")
async def admin_news_delete(news_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    news_item = await crud_news.get(db, news_id)
    if news_item: await crud_news.delete(db, db_obj=news_item)

    return RedirectResponse(url="/admin/news", status_code=303)


@router.get("/matches/{match_id}/print")
async def match_protocol_print(match_id: int, request: Request, db: AsyncSession = Depends(get_db)):
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

    home_events = [e for e in match.events if e.team_id == match.home_team_id]
    away_events = [e for e in match.events if e.team_id == match.away_team_id]

    return templates.TemplateResponse(
        request=request, name="protocol_print.html",
        context={"match": match, "home_events": home_events, "away_events": away_events}
    )


@router.get("/admin/media")
async def admin_media_list(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    stmt = select(MediaAlbum).options(selectinload(MediaAlbum.items)).order_by(MediaAlbum.created_at.desc())
    albums = (await db.execute(stmt)).scalars().all()
    return await render_page(request, db, "admin_media.html", context={"user": user, "albums": albums})


@router.get("/admin/media/create")
async def admin_media_create_form(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    tournaments = (await db.execute(select(Tournament).order_by(Tournament.name))).scalars().all()
    return await render_page(request, db, "admin_media_form.html",
                             context={"user": user, "album": None, "tournaments": tournaments})


@router.post("/admin/media/create")
async def admin_media_create(
        request: Request, title: str = Form(...), tournament_id: int = Form(None), db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    album = MediaAlbum(title=title, tournament_id=tournament_id)
    db.add(album)
    await db.commit()
    await db.refresh(album)

    return RedirectResponse(url=f"/admin/media/{album.id}", status_code=303)


@router.post("/admin/media/{album_id}/delete")
async def admin_media_delete(album_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    album = await db.get(MediaAlbum, album_id)
    if album:
        await db.delete(album)
        await db.commit()

    return RedirectResponse(url="/admin/media", status_code=303)


@router.get("/admin/media/{album_id}")
async def admin_media_album_detail(album_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    stmt = select(MediaAlbum).where(MediaAlbum.id == album_id).options(selectinload(MediaAlbum.items))
    album = (await db.execute(stmt)).scalar_one_or_none()
    if not album: return RedirectResponse(url="/admin/media", status_code=303)

    return await render_page(request, db, "admin_media_detail.html", context={"user": user, "album": album})


@router.post("/admin/media/{album_id}/upload")
async def admin_media_upload(
        album_id: int, request: Request, photos: list[UploadFile] = File(...), db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    album = await db.get(MediaAlbum, album_id)
    if not album: return RedirectResponse(url="/admin/media", status_code=303)

    for photo in photos:
        photo_url = await save_upload_file(photo, "media")
        if photo_url:
            item = MediaItem(album_id=album_id, media_type="photo", file_url=photo_url)
            db.add(item)

    await db.commit()
    return RedirectResponse(url=f"/admin/media/{album_id}", status_code=303)


@router.post("/admin/media/item/{item_id}/delete")
async def admin_media_item_delete(item_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    item = await db.get(MediaItem, item_id)
    album_id = item.album_id if item else None
    if item:
        await db.delete(item)
        await db.commit()

    return RedirectResponse(url=f"/admin/media/{album_id}", status_code=303)