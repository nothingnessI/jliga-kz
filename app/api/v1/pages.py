from collections import defaultdict
from pathlib import Path
import shutil
import uuid
from typing import Optional


from fastapi import APIRouter, Request, Depends, HTTPException, Form, UploadFile, File
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from jose import jwt, JWTError
from app.db.session import get_db
from app.core.security import create_access_token, verify_password, ALGORITHM
from app.core.config import settings
from app.services.champion_card import generate_champion_card
from datetime import datetime, timezone, timedelta
from app.models.partner import Partner
from app.models.news import News, news_teams
from app.models.team import Team
from app.models.user import User
from app.models.enums import MatchStatus, TournamentStatus, UserRole, TournamentFormat, PlayerPosition, MatchEventType
from app.models.media import MediaAlbum, MediaItem
from app.models.team_player import TeamPlayer
from app.services.match import MatchService
from app.models.tournament_team import TournamentTeam

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
from app.models.player import Player
from app.models.disqualification import Disqualification
from app.services.stats_recalc import StatsRecalculator
from app.services.schedule_generator import generate_round_robin
from app.services.registration_approval import approve_registration
from sqlalchemy.exc import IntegrityError
from urllib.parse import urlsplit
from sqlalchemy.orm import selectinload
from sqlalchemy import func, select, or_, desc, case, update, delete
from app.models.match import Match
from app.models.tournament import Tournament
from app.models.match_event import MatchEvent
from app.models.tournament_standing import TournamentStanding
from app.models.enums import MatchStage
from app.services.standings import StandingsService


router = APIRouter()
BASE_DIR = Path(__file__).resolve().parent.parent.parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")


from types import SimpleNamespace


def calculate_standings_from_matches(matches, teams, is_cup=False):
    stats = {}
    for t in teams:
        stats[t.id] = {
            "team": t, "group_name": None, "games_played": 0, "wins": 0,
            "draws": 0, "losses": 0, "goals_for": 0, "goals_against": 0, "points": 0
        }
    for m in matches:
        if m.status != MatchStatus.FINISHED or m.home_score is None or m.away_score is None:
            continue
        h = stats.get(m.home_team_id)
        a = stats.get(m.away_team_id)
        if not h or not a: continue

        if m.group_name:
            h["group_name"] = m.group_name
            a["group_name"] = m.group_name

        h["games_played"] += 1; a["games_played"] += 1
        h["goals_for"] += m.home_score; h["goals_against"] += m.away_score
        a["goals_for"] += m.away_score; a["goals_against"] += m.home_score

        if m.home_score > m.away_score:
            h["wins"] += 1; h["points"] += 3; a["losses"] += 1
        elif m.home_score < m.away_score:
            a["wins"] += 1; a["points"] += 3; h["losses"] += 1
        else:
            h["draws"] += 1; a["draws"] += 1
            h["points"] += 1; a["points"] += 1

    result = [SimpleNamespace(**v) for v in stats.values()]
    if is_cup:
        result.sort(key=lambda x: (x.group_name or 'ZZZ', -x.points, -x.goals_for))
    else:
        result.sort(key=lambda x: (-x.points, -(x.goals_for - x.goals_against), -x.goals_for))
    return result


def to_local_time(dt):
    """Конвертирует UTC время в часовой пояс Алматы (UTC+5)"""
    if not dt:
        return None
    from datetime import timezone, timedelta
    from zoneinfo import ZoneInfo

    try:
        local_tz = ZoneInfo("Asia/Almaty")
    except Exception:
        local_tz = timezone(timedelta(hours=5))

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(local_tz)


from datetime import datetime, timezone
from sqlalchemy import update
from app.models.enums import TournamentStatus


async def check_and_finish_expired_tournaments(db: AsyncSession):
    """Мгновенно закрывает турниры, у которых прошла дата окончания."""
    now = datetime.now(timezone.utc)
    stmt = (
        update(Tournament)
        .where(Tournament.status.in_([TournamentStatus.ACTIVE, TournamentStatus.ACCEPTING_APPLICATIONS]))
        .where(Tournament.end_at.isnot(None))
        .where(Tournament.end_at < now)
        .values(status=TournamentStatus.FINISHED)
    )
    result = await db.execute(stmt)
    if result.rowcount > 0:
        await db.commit()


def fix_logo_url(url: str | None) -> str | None:
    if url and url.startswith("/"):
        return f"https://jliga.ru{url}"
    return url


async def get_mega_menu_context(db: AsyncSession):
    latest_season_stmt = select(Tournament.season).order_by(Tournament.season.desc()).limit(1)
    latest_season_result = await db.execute(latest_season_stmt)
    current_year = latest_season_result.scalar() or str(datetime.now().year)

    stmt = select(Tournament).where(
        Tournament.season == current_year,
        Tournament.show_in_menu == True,
        Tournament.menu_category.isnot(None),
        Tournament.menu_category != ""
    ).order_by(Tournament.sort_order.asc(), Tournament.name)

    tournaments = (await db.execute(stmt)).scalars().all()

    menu_data = {}
    for t in tournaments:
        category = t.menu_category

        if category not in menu_data:
            menu_data[category] = []

        menu_data[category].append({
            "id": t.id,
            "name": t.short_name if t.short_name else t.name
        })

    return {
        "menu_data": menu_data,
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

    partners = (await db.execute(
        select(Partner).where(Partner.is_active == True).order_by(Partner.sort_order, Partner.id)
    )).scalars().all()
    context["partners"] = partners

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
    """Store images safely, rejecting path traversal, oversized and unsupported uploads."""
    if upload_file is None or not upload_file.filename:
        return None
    allowed_folders = {"teams", "players", "coaches", "news", "media", "partners", "stadiums", "tournaments", "albums", "photos", "videos", "logos"}
    if folder not in allowed_folders:
        raise HTTPException(422, "Недопустимая папка загрузки")
    suffix = Path(upload_file.filename).suffix.lower()
    allowed_types = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}
    if suffix not in allowed_types:
        raise HTTPException(422, "Разрешены только JPG, PNG, WebP и GIF")
    if upload_file.content_type and upload_file.content_type not in (allowed_types[suffix], "application/octet-stream"):
        raise HTTPException(422, "Тип файла не соответствует расширению")
    upload_dir = BASE_DIR / "static" / "uploads" / folder
    upload_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex}{suffix}"
    file_path = upload_dir / filename
    total = 0
    try:
        with file_path.open("wb") as output:
            while chunk := await upload_file.read(1024 * 1024):
                total += len(chunk)
                if total > 10 * 1024 * 1024:
                    raise HTTPException(413, "Размер файла превышает 10 МБ")
                output.write(chunk)
        if total == 0:
            raise HTTPException(422, "Пустой файл")
        return f"/static/uploads/{folder}/{filename}"
    except Exception:
        file_path.unlink(missing_ok=True)
        raise
    finally:
        await upload_file.close()



from sqlalchemy import select, or_, desc, case, update, delete


# Публичные страницы

@router.get("/")
async def home_page(request: Request, tournament_id: int = None, db: AsyncSession = Depends(get_db)):
    await check_and_finish_expired_tournaments(db)

    from datetime import datetime, timezone, timedelta
    from sqlalchemy import func, select, or_
    from sqlalchemy.orm import selectinload
    from app.models.enums import MatchStatus, MatchEventType
    from app.models.match_event import MatchEvent
    from app.models.match import Match
    from app.models.team import Team
    from app.models.tournament_team import TournamentTeam
    from types import SimpleNamespace

    try:
        from zoneinfo import ZoneInfo
        local_tz = ZoneInfo("Asia/Almaty")
    except Exception:
        local_tz = timezone(timedelta(hours=5))

    latest_season = await db.scalar(select(Tournament.season).order_by(Tournament.season.desc()).limit(1))
    if not latest_season:
        latest_season = str(datetime.now().year)

    keywords = ["примера", "сегунда", "терсера", "региональ", "junior", "primera", "segunda", "tersera", "regional"]
    conditions = [Tournament.name.ilike(f"%{kw}%") for kw in keywords]

    stmt_tournaments = select(Tournament).where(
        Tournament.season == latest_season,
        or_(*conditions)
    ).order_by(
        Tournament.sort_order.asc(),
        Tournament.name.asc()
    )
    tournaments = (await db.execute(stmt_tournaments)).scalars().all()

    active_tournament = None
    if tournament_id:
        active_tournament = next((t for t in tournaments if t.id == tournament_id), None)
    if not active_tournament and tournaments:
        active_tournament = tournaments[0]

    now_local = datetime.now(local_tz)
    start_of_day = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_day = start_of_day + timedelta(days=2)  # Захватываем сегодня и завтра

    stmt_upcoming = select(Match).where(
        Match.match_date.isnot(None),
        Match.match_date >= start_of_day,
        Match.match_date < end_of_day,
        Match.status.in_([MatchStatus.SCHEDULED, MatchStatus.LIVE, MatchStatus.FINISHED, MatchStatus.CANCELLED])
    ).options(
        selectinload(Match.home_team),
        selectinload(Match.away_team),
        selectinload(Match.tournament),
        selectinload(Match.events).selectinload(MatchEvent.player)
    ).order_by(
        Match.match_date.asc()
    )

    upcoming_matches = (await db.execute(stmt_upcoming)).scalars().all()

    for m in upcoming_matches:
        if m.match_date:
            m.local_match_date = to_local_time(m.match_date)

    standings = []
    top_scorers = []
    active_disqualifications = []

    if active_tournament:
        stmt_matches_table = select(Match).where(Match.tournament_id == active_tournament.id)
        matches_for_table = (await db.execute(stmt_matches_table)).scalars().all()

        stmt_teams_table = select(Team).join(TournamentTeam, Team.id == TournamentTeam.team_id).where(
            TournamentTeam.tournament_id == active_tournament.id
        )
        teams_for_table = (await db.execute(stmt_teams_table)).scalars().all()

        is_cup = active_tournament.division == "CUP" or any(m.group_name for m in matches_for_table)

        standings_dict = {}
        for t in teams_for_table:
            standings_dict[t.id] = {
                "team": t, "group_name": None, "games_played": 0, "wins": 0,
                "draws": 0, "losses": 0, "goals_for": 0, "goals_against": 0, "points": 0
            }

        for m in matches_for_table:
            if m.status != MatchStatus.FINISHED or m.home_score is None or m.away_score is None: continue
            h = standings_dict.get(m.home_team_id)
            a = standings_dict.get(m.away_team_id)
            if not h or not a: continue

            if m.group_name:
                h["group_name"] = m.group_name
                a["group_name"] = m.group_name

            h["games_played"] += 1;
            a["games_played"] += 1
            h["goals_for"] += m.home_score;
            h["goals_against"] += m.away_score
            a["goals_for"] += m.away_score;
            a["goals_against"] += m.home_score

            if m.home_score > m.away_score:
                h["wins"] += 1;
                h["points"] += 3;
                a["losses"] += 1
            elif m.home_score < m.away_score:
                a["wins"] += 1;
                a["points"] += 3;
                h["losses"] += 1
            else:
                h["draws"] += 1;
                a["draws"] += 1
                h["points"] += 1;
                a["points"] += 1

        standings = [SimpleNamespace(**v) for v in standings_dict.values()]
        if is_cup:
            standings.sort(key=lambda x: (x.group_name or 'ZZZ', -x.points, -x.goals_for))
        else:
            standings.sort(key=lambda x: (-x.points, -(x.goals_for - x.goals_against), -x.goals_for))

        stmt_scorers = select(TeamPlayer).join(Player).join(Team).where(
            TeamPlayer.tournament_id == active_tournament.id,
            TeamPlayer.stats_goals > 0
        ).options(
            selectinload(TeamPlayer.player),
            selectinload(TeamPlayer.team)
        ).order_by(
            TeamPlayer.stats_goals.desc()
        ).limit(5)

        top_scorers_raw = (await db.execute(stmt_scorers)).scalars().all()
        for ts in top_scorers_raw:
            top_scorers.append({
                "id": ts.player.id,
                "name": f"{ts.player.last_name} {ts.player.first_name}",
                "photo_url": ts.player.photo_url,
                "team_name": ts.team.name if ts.team else "Неизвестно",
                "goals": ts.stats_goals
            })

        stmt_disq = select(Disqualification).where(
            Disqualification.tournament_id == active_tournament.id,
            Disqualification.matches_remaining > 0
        ).options(
            selectinload(Disqualification.player),
            selectinload(Disqualification.team)
        ).order_by(
            Disqualification.matches_remaining.desc()
        )
        active_disqualifications = (await db.execute(stmt_disq)).scalars().all()

    stmt_news = select(News).where(News.is_published == True).order_by(
        News.sort_order.asc(),
        News.published_at.desc(),
        News.id.desc()
    ).limit(4)
    latest_news = (await db.execute(stmt_news)).scalars().all()

    matches_dict = {}
    for m in upcoming_matches:
        events_list = []
        if m.events:
            sorted_events = sorted(m.events, key=lambda x: x.minute)
            for e in sorted_events:
                event_str = str(e.event_type).upper()
                if "YELLOW" in event_str:
                    etype = "YELLOW"
                elif "RED" in event_str:
                    etype = "RED"
                elif "OWN" in event_str:
                    etype = "OWN_GOAL"
                elif "PENALTY" in event_str:
                    etype = "PENALTY"
                else:
                    etype = "GOAL"

                events_list.append({
                    "minute": e.minute, "team_id": e.team_id,
                    "player": f"{e.player.last_name} {e.player.first_name[0]}." if e.player else "Игрок",
                    "type": etype
                })

        matches_dict[m.id] = {
            "id": m.id, "home_name": m.home_team.name, "away_name": m.away_team.name,
            "home_team_id": m.home_team_id, "away_team_id": m.away_team_id,
            "home_score": m.home_score if m.home_score is not None else 0,
            "away_score": m.away_score if m.away_score is not None else 0,
            "home_logo": m.home_team.logo_url or f"https://ui-avatars.com/api/?name={m.home_team.name}&background=0f2537&color=fff&size=64&rounded=true",
            "away_logo": m.away_team.logo_url or f"https://ui-avatars.com/api/?name={m.away_team.name}&background=0f2537&color=fff&size=64&rounded=true",
            "home_penalty": m.home_penalty, "away_penalty": m.away_penalty,
            "stage_name": m.stage_name, "tech_loss": m.tech_loss,
            "date": to_local_time(m.match_date).strftime('%d.%m.%Y %H:%M') if m.match_date else 'TBA',
            "stadium": m.stadium or 'Стадион TBA', "events": events_list,
        }

    return await render_page(request, db, "index.html", context={
        "tournaments": tournaments,
        "upcoming_matches": upcoming_matches,
        "today_matches": upcoming_matches,
        "latest_news": latest_news,
        "standings": standings,
        "top_scorers": top_scorers,
        "active_disqualifications": active_disqualifications,
        "active_tournament": active_tournament,
        "selected_tournament_id": active_tournament.id if active_tournament else None,
        "latest_season": latest_season,
        "matches_dict": matches_dict
    })


@router.get("/matches")
async def matches_page(
        request: Request,
        page: int = 1,
        year: str = None,
        tournament_id: str = None,
        date_filter: str = None,
        db: AsyncSession = Depends(get_db)
):
    from datetime import datetime, timezone, timedelta
    from sqlalchemy import case, func
    from sqlalchemy.orm import selectinload
    from app.models.match_event import MatchEvent

    try:
        from zoneinfo import ZoneInfo
        local_tz = ZoneInfo("Asia/Almaty")
    except Exception:
        local_tz = timezone(timedelta(hours=5))

    limit = 20
    skip = (page - 1) * limit

    years_result = await db.execute(select(Tournament.season).distinct().order_by(Tournament.season.desc()))
    available_years = [y[0] for y in years_result.all() if y[0]]

    if not year and available_years:
        year = available_years[0]

    tournaments = (
        await db.execute(select(Tournament).where(Tournament.season == year).order_by(Tournament.name))
    ).scalars().all()

    t_id = int(tournament_id) if tournament_id and tournament_id.strip().isdigit() else None

    parsed_date = None
    d_filter_str = date_filter.strip() if date_filter and date_filter.strip() else None
    if d_filter_str:
        try:
            parsed_date = datetime.strptime(d_filter_str, "%Y-%m-%d").date()
        except ValueError:
            parsed_date = None

    y_filter = year.strip() if year and year.strip() else None

    stmt = select(Match).join(Tournament).options(
        selectinload(Match.home_team),
        selectinload(Match.away_team),
        selectinload(Match.tournament),
        selectinload(Match.events).selectinload(MatchEvent.player)
    )
    count_stmt = select(func.count()).select_from(Match).join(Tournament)

    if y_filter:
        stmt = stmt.where(Tournament.season == y_filter)
        count_stmt = count_stmt.where(Tournament.season == y_filter)

    if t_id is not None:
        stmt = stmt.where(Match.tournament_id == t_id)
        count_stmt = count_stmt.where(Match.tournament_id == t_id)

    if parsed_date:
        stmt = stmt.where(func.date(Match.match_date) == parsed_date)
        count_stmt = count_stmt.where(func.date(Match.match_date) == parsed_date)

    now = datetime.now(timezone.utc)

    time_group = case(
        (Match.match_date >= now, 0),
        (Match.match_date < now, 1),
        else_=2
    )

    stmt = stmt.order_by(
        time_group,
        case((Match.match_date >= now, Match.match_date), else_=None).asc().nullslast(),
        case((Match.match_date < now, Match.match_date), else_=None).desc().nullslast(),
        Match.id.desc()
    )

    total_matches = await db.scalar(count_stmt) or 0
    stmt = stmt.offset(skip).limit(limit)
    matches = (await db.execute(stmt)).scalars().all()

    for m in matches:
        m.local_match_date = to_local_time(m.match_date)

    for m in matches:
        if m.match_date:
            m.local_match_date = to_local_time(m.match_date)

    total_pages = (total_matches + limit - 1) // limit if total_matches > 0 else 1

    matches_dict = {}
    for m in matches:
        events_list = []
        if m.events:
            sorted_events = sorted(m.events, key=lambda x: x.minute)
            for e in sorted_events:
                event_str = str(e.event_type).upper()
                if "YELLOW" in event_str:
                    etype = "YELLOW"
                elif "RED" in event_str:
                    etype = "RED"
                elif "OWN" in event_str:
                    etype = "OWN_GOAL"
                elif "PENALTY" in event_str:
                    etype = "PENALTY"
                else:
                    etype = "GOAL"

                events_list.append({
                    "minute": e.minute, "team_id": e.team_id,
                    "player": f"{e.player.last_name} {e.player.first_name[0]}." if e.player else "Игрок",
                    "type": etype
                })

        matches_dict[m.id] = {
            "id": m.id, "home_name": m.home_team.name, "away_name": m.away_team.name,
            "home_team_id": m.home_team_id, "away_team_id": m.away_team_id,
            "home_score": m.home_score if m.home_score is not None else 0,
            "away_score": m.away_score if m.away_score is not None else 0,
            "home_logo": m.home_team.logo_url or f"https://ui-avatars.com/api/?name={m.home_team.name}&background=0f2537&color=fff&size=64&rounded=true",
            "away_logo": m.away_team.logo_url or f"https://ui-avatars.com/api/?name={m.away_team.name}&background=0f2537&color=fff&size=64&rounded=true",
            "home_penalty": m.home_penalty, "away_penalty": m.away_penalty,
            "stage_name": m.stage_name, "tech_loss": m.tech_loss,
            "date": to_local_time(m.match_date).strftime('%d.%m.%Y %H:%M') if m.match_date else 'TBA',
            "stadium": m.stadium or 'Стадион TBA', "events": events_list,
        }

    return await render_page(request, db, "matches.html", context={
        "matches": matches,
        "page": page,
        "total_pages": total_pages,
        "total_matches": total_matches,
        "available_years": available_years,
        "tournaments": tournaments,
        "selected_year": year,
        "selected_tournament_id": t_id,
        "date_filter": d_filter_str or "",
        "matches_dict": matches_dict
    })


@router.get("/about")
async def about_page(request: Request, db: AsyncSession = Depends(get_db)):
    return await render_page(request, db, "about.html")


@router.get("/tournaments")
async def tournaments_list_page(request: Request, season: str = None, db: AsyncSession = Depends(get_db)):


    await check_and_finish_expired_tournaments(db)
    seasons_result = await db.execute(select(Tournament.season).distinct().order_by(Tournament.season.desc()))
    available_seasons = [s[0] for s in seasons_result.all() if s[0]]

    if not season and available_seasons:
        season = available_seasons[0]

    stmt = select(Tournament).where(Tournament.season == season).order_by(
        Tournament.sort_order.asc(), # <--- ДОБАВИЛИ ЭТО
        Tournament.name.asc()
    )
    tournaments = (await db.execute(stmt)).scalars().all()

    return await render_page(request, db, "tournaments.html", context={
        "tournaments": tournaments,
        "available_seasons": available_seasons,
        "current_season": season
    })


from datetime import datetime, timezone, date
from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.session import get_db
from app.models.tournament import Tournament
from app.models.tournament_standing import TournamentStanding
from app.models.match import Match
from app.models.match_event import MatchEvent
from app.models.enums import MatchStatus, MatchStage
from app.services.standings import StandingsService
from app.services.stats import StatsService

from collections import defaultdict
from datetime import datetime, timezone
from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from types import SimpleNamespace


def calculate_standings(matches: list, teams: list, is_cup: bool = False) -> list:
    stats = {}
    for t in teams:
        stats[t.id] = {
            "team": t,
            "group_name": None,
            "games_played": 0, "wins": 0, "draws": 0, "losses": 0,
            "goals_for": 0, "goals_against": 0, "points": 0
        }

    for m in matches:
        if m.status != MatchStatus.FINISHED or m.home_score is None or m.away_score is None:
            continue

        h = stats.get(m.home_team_id)
        a = stats.get(m.away_team_id)
        if not h or not a:
            continue

        if m.group_name:
            h["group_name"] = m.group_name
            a["group_name"] = m.group_name

        h["games_played"] += 1
        a["games_played"] += 1

        h["goals_for"] += m.home_score
        h["goals_against"] += m.away_score
        a["goals_for"] += m.away_score
        a["goals_against"] += m.home_score

        if m.home_score > m.away_score:
            h["wins"] += 1;
            h["points"] += 3;
            a["losses"] += 1
        elif m.home_score < m.away_score:
            a["wins"] += 1;
            a["points"] += 3;
            h["losses"] += 1
        else:
            h["draws"] += 1;
            a["draws"] += 1
            h["points"] += 1;
            a["points"] += 1

    result = [SimpleNamespace(**v) for v in stats.values()]

    if is_cup:
        result.sort(key=lambda x: (x.group_name or 'ZZZ', -x.points, -x.goals_for))
    else:
        result.sort(key=lambda x: (-x.points, -(x.goals_for - x.goals_against), -x.goals_for))

    return result


from types import SimpleNamespace

@router.get("/tournaments/{tournament_id}")
async def tournament_page(
        tournament_id: int,
        request: Request,
        show_past: bool = True,
        db: AsyncSession = Depends(get_db)
):
    import re
    from collections import defaultdict
    from sqlalchemy import func, or_, select
    from sqlalchemy.orm import selectinload
    from datetime import datetime, timezone
    from app.models.tournament import Tournament
    from app.models.match import Match
    from app.models.match_event import MatchEvent
    from app.models.team_player import TeamPlayer
    from app.models.player import Player
    from app.models.disqualification import Disqualification
    from app.models.news import News
    from app.models.enums import MatchStatus, MatchStage
    from app.models.tournament_team import TournamentTeam
    from app.models.team import Team

    tournament = await db.get(Tournament, tournament_id)
    if not tournament:
        raise HTTPException(status_code=404, detail="Турнир не найден")

    stmt_matches = select(Match).where(Match.tournament_id == tournament_id).options(
        selectinload(Match.home_team),
        selectinload(Match.away_team),
        selectinload(Match.events).selectinload(MatchEvent.player)
    )
    matches_raw = (await db.execute(stmt_matches)).scalars().all()

    stmt_teams = select(Team).join(TournamentTeam, Team.id == TournamentTeam.team_id).where(
        TournamentTeam.tournament_id == tournament_id
    )
    teams_raw = (await db.execute(stmt_teams)).scalars().all()

    standings_dict = {}
    for t in teams_raw:
        standings_dict[t.id] = {
            "team": t,
            "group_name": None,
            "games_played": 0, "wins": 0, "draws": 0, "losses": 0,
            "goals_for": 0, "goals_against": 0, "points": 0
        }

    for m in matches_raw:
        if m.status != MatchStatus.FINISHED or m.home_score is None or m.away_score is None:
            continue

        h = standings_dict.get(m.home_team_id)
        a = standings_dict.get(m.away_team_id)
        if not h or not a:
            continue

        if m.group_name:
            h["group_name"] = m.group_name
            a["group_name"] = m.group_name

        h["games_played"] += 1
        a["games_played"] += 1

        h["goals_for"] += m.home_score
        h["goals_against"] += m.away_score
        a["goals_for"] += m.away_score
        a["goals_against"] += m.home_score

        if m.home_score > m.away_score:
            h["wins"] += 1; h["points"] += 3; a["losses"] += 1
        elif m.home_score < m.away_score:
            a["wins"] += 1; a["points"] += 3; h["losses"] += 1
        else:
            h["draws"] += 1; a["draws"] += 1
            h["points"] += 1; a["points"] += 1

    standings = [SimpleNamespace(**v) for v in standings_dict.values()]

    is_cup = tournament.division == "CUP" or any(s.group_name for s in standings)

    if is_cup:
        standings.sort(key=lambda x: (x.group_name or 'ZZZ', -x.points, -x.goals_for))
    else:
        standings.sort(key=lambda x: (-x.points, -(x.goals_for - x.goals_against), -x.goals_for))

    grouped_standings = {}
    for s in standings:
        group_name = s.group_name if s.group_name else "Общая таблица"
        if group_name not in grouped_standings:
            grouped_standings[group_name] = []
        grouped_standings[group_name].append(s)

    teams_list = [s.team for s in standings]

    for m in matches_raw:
        m.local_match_date = to_local_time(m.match_date)

    fallback_date = datetime.min.replace(tzinfo=timezone.utc)
    matches = sorted(
        matches_raw,
        key=lambda x: (
            int(x.round) if x.round and str(x.round).isdigit() else 0,
            x.match_date.timestamp() if x.match_date else fallback_date.timestamp()
        )
    )

    playoff_matches = [m for m in matches if m.stage in [MatchStage.PLAYOFF, MatchStage.FINAL]]

    brackets_data = {}
    for m in playoff_matches:
        bracket_name = m.group_name if m.group_name else "Плей-офф"
        if bracket_name not in brackets_data:
            brackets_data[bracket_name] = {}

        s_name = (m.stage_name or "").upper()
        if "1/8" in s_name or "ВОСЬМ" in s_name:
            r_num = 1
        elif "1/4" in s_name or "ЧЕТВЕРТЬ" in s_name:
            r_num = 2
        elif "1/2" in s_name or "ПОЛУ" in s_name:
            r_num = 3
        elif "ФИНАЛ" in s_name and "ПОЛУ" not in s_name and "ЧЕТВЕРТЬ" not in s_name:
            r_num = 4
        elif ("3" in s_name and ("МЕСТО" in s_name or "ТРЕТЬ" in s_name or "БРОНЗ" in s_name)):
            r_num = 5
        else:
            r_num = m.round if m.round else 1

        if r_num not in brackets_data[bracket_name]:
            brackets_data[bracket_name][r_num] = []
        brackets_data[bracket_name][r_num].append(m)

    for b_name in brackets_data:
        brackets_data[b_name] = dict(sorted(brackets_data[b_name].items()))

    round_names = {1: "1/8 ФИНАЛА", 2: "1/4 ФИНАЛА", 3: "1/2 ФИНАЛА", 4: "ФИНАЛ", 5: "ЗА 3 МЕСТО"}

    matrix_data = {}
    for m in matches:
        is_finished = m.home_score is not None and m.away_score is not None

        score_home = f"{m.home_score}-{m.away_score}" if is_finished else "-:-"
        score_away = f"{m.away_score}-{m.home_score}" if is_finished else "-:-"

        if m.tech_loss:
            score_home += "*"
            score_away += "*"

        if is_finished:
            res_home = "W" if m.home_score > m.away_score else ("L" if m.home_score < m.away_score else "D")
            res_away = "W" if m.away_score > m.home_score else ("L" if m.away_score < m.home_score else "D")
        else:
            res_home = res_away = ""

        matrix_data[(m.home_team_id, m.away_team_id)] = {
            "score": score_home, "match_id": m.id, "result": res_home
        }
        matrix_data[(m.away_team_id, m.home_team_id)] = {
            "score": score_away, "match_id": m.id, "result": res_away
        }

    progress_data = {}
    for team in teams_list:
        team_matches = [m for m in matches if m.home_team_id == team.id or m.away_team_id == team.id]
        team_matches.sort(key=lambda x: x.match_date.timestamp() if x.match_date else 0, reverse=False)

        form_all = []
        for m in team_matches:
            if m.home_score is not None and m.away_score is not None:
                if m.home_team_id == team.id:
                    res = 'В' if m.home_score > m.away_score else ('П' if m.home_score < m.away_score else 'Н')
                else:
                    res = 'В' if m.away_score > m.home_score else ('П' if m.away_score < m.home_score else 'Н')
                form_all.append({"result": res, "match_id": m.id})
        progress_data[team.id] = form_all

    matches_dict = {}
    for m in matches:
        events_list = []
        if m.events:
            sorted_events = sorted(m.events, key=lambda x: x.minute)
            for e in sorted_events:
                event_str = str(e.event_type).upper()
                if "YELLOW" in event_str: etype = "YELLOW"
                elif "RED" in event_str: etype = "RED"
                elif "OWN" in event_str: etype = "OWN_GOAL"
                elif "PENALTY" in event_str: etype = "PENALTY"
                else: etype = "GOAL"

                events_list.append({
                    "minute": e.minute, "team_id": e.team_id,
                    "player": f"{e.player.last_name} {e.player.first_name[0]}." if e.player else "Игрок",
                    "type": etype
                })

        matches_dict[m.id] = {
            "id": m.id,
            "home_name": m.home_team.name,
            "away_name": m.away_team.name,
            "home_team_id": m.home_team_id,
            "away_team_id": m.away_team_id,
            "home_score": m.home_score if m.home_score is not None else 0,
            "away_score": m.away_score if m.away_score is not None else 0,
            "home_logo": m.home_team.logo_url or f"https://ui-avatars.com/api/?name={m.home_team.name}&background=0f2537&color=fff&size=64&rounded=true",
            "away_logo": m.away_team.logo_url or f"https://ui-avatars.com/api/?name={m.away_team.name}&background=0f2537&color=fff&size=64&rounded=true",
            "home_penalty": m.home_penalty,
            "away_penalty": m.away_penalty,
            "stage_name": m.stage_name,
            "tech_loss": m.tech_loss,
            "date": to_local_time(m.match_date).strftime('%d.%m.%Y %H:%M') if m.match_date else 'TBA',
            "stadium": m.stadium or 'Стадион TBA',
            "events": events_list,
            "status": m.status.value if hasattr(m.status, 'value') else str(m.status)
        }

    matches_by_round = defaultdict(list)
    matches_by_date = defaultdict(list)

    calendar_matches = matches
    if not show_past:
        calendar_matches = [m for m in matches if m.status in [MatchStatus.SCHEDULED, MatchStatus.LIVE]]

    calendar_matches.sort(key=lambda x: (
        0 if x.status == MatchStatus.SCHEDULED else 1,
        x.match_date.timestamp() if x.status == MatchStatus.SCHEDULED and x.match_date else 9999999999,
        -x.match_date.timestamp() if x.status != MatchStatus.SCHEDULED and x.match_date else 0
    ))

    for m in calendar_matches:
        if m.status == MatchStatus.SCHEDULED:
            group_key = "БЛИЖАЙШИЕ МАТЧИ"
        else:
            if m.stage_name:
                group_key = m.stage_name.upper()
            elif m.round and str(m.round).isdigit() and int(m.round) > 0:
                group_key = f"{m.round} ТУР"
            else:
                group_key = "ГРУППОВОЙ ЭТАП"

        matches_by_round[group_key].append(m)

        if m.match_date:
            months_ru = {1: "ЯНВАРЯ", 2: "ФЕВРАЛЯ", 3: "МАРТА", 4: "АПРЕЛЯ", 5: "МАЯ", 6: "ИЮНЯ",
                         7: "ИЮЛЯ", 8: "АВГУСТА", 9: "СЕНТЯБРЯ", 10: "ОКТЯБРЯ", 11: "НОЯБРЯ", 12: "ДЕКАБРЯ"}
            weekdays_ru = {0: "ПОНЕДЕЛЬНИК", 1: "ВТОРНИК", 2: "СРЕДА", 3: "ЧЕТВЕРГ", 4: "ПЯТНИЦА", 5: "СУББОТА",
                           6: "ВОСКРЕСЕНЬЕ"}
            dt = m.match_date
            date_str = f"{dt.day} {months_ru[dt.month]}, {weekdays_ru[dt.weekday()]}"
            matches_by_date[date_str].append(m)
        else:
            matches_by_date['ДАТА TBA'].append(m)

    def group_sort_key(item):
        key = item[0]
        if key == "БЛИЖАЙШИЕ МАТЧИ": return (0, 0)
        if "ПЛЕЙ-ОФФ" in key or "ФИНАЛ" in key or "1/" in key: return (2, 0)
        match = re.search(r'\d+', key)
        if match: return (1, int(match.group()))
        return (1, 999)

    matches_by_round = dict(sorted(matches_by_round.items(), key=group_sort_key))

    stmt_stats_goals = select(TeamPlayer).join(Player).where(
        TeamPlayer.tournament_id == tournament_id, func.coalesce(TeamPlayer.stats_goals, 0) > 0
    ).options(selectinload(TeamPlayer.player), selectinload(TeamPlayer.team)).order_by(
        func.coalesce(TeamPlayer.stats_goals, 0).desc(), func.coalesce(TeamPlayer.stats_games, 0).asc()
    )
    tournament_stats_goals = (await db.execute(stmt_stats_goals)).scalars().all()

    stmt_stats_cards = select(TeamPlayer).join(Player).where(
        TeamPlayer.tournament_id == tournament_id,
        or_(func.coalesce(TeamPlayer.stats_yellow_cards, 0) > 0, func.coalesce(TeamPlayer.stats_red_cards, 0) > 0)
    ).options(selectinload(TeamPlayer.player), selectinload(TeamPlayer.team)).order_by(
        func.coalesce(TeamPlayer.stats_yellow_cards, 0).desc(), func.coalesce(TeamPlayer.stats_red_cards, 0).desc()
    )
    tournament_stats_cards = (await db.execute(stmt_stats_cards)).scalars().all()

    stmt_disq = select(Disqualification).where(Disqualification.tournament_id == tournament_id).options(
        selectinload(Disqualification.player), selectinload(Disqualification.team)
    ).order_by(Disqualification.decision_date.desc())
    all_disq = (await db.execute(stmt_disq)).scalars().all()
    active_disq = [d for d in all_disq if d.matches_remaining and d.matches_remaining > 0]
    completed_disq = [d for d in all_disq if not d.matches_remaining or d.matches_remaining == 0]

    stmt_news = select(News).where(
        News.is_published == True, News.tournament_id == tournament_id
    ).order_by(News.published_at.desc()).limit(9)
    tournament_news = (await db.execute(stmt_news)).scalars().all()

    return await render_page(request, db, "tournament_detail.html", context={
        "tournament": tournament,
        "grouped_standings": grouped_standings,
        "is_cup": is_cup,
        "brackets_data": brackets_data,
        "round_names": round_names,
        "matches": matches,
        "standings": standings,
        "teams_list": teams_list,
        "matrix": matrix_data,
        "progress": progress_data,
        "matches_dict": matches_dict,
        "matches_by_round": matches_by_round,
        "matches_by_date": matches_by_date,
        "tournament_stats_goals": tournament_stats_goals,
        "tournament_stats_cards": tournament_stats_cards,
        "active_disq": active_disq,
        "completed_disq": completed_disq,
        "tournament_news": tournament_news,
        "show_past": show_past,
        "now": datetime.now(timezone.utc)
    })


@router.get("/participants/teams")
async def participants_teams_page(
        request: Request,
        page: int = 1,
        season: str = None,
        search: str = None,
        db: AsyncSession = Depends(get_db)
):
    limit = 20
    skip = (page - 1) * limit

    seasons_result = await db.execute(select(Tournament.season).distinct().order_by(Tournament.season.desc()))
    available_seasons = [s[0] for s in seasons_result.all() if s[0]]

    if not season and available_seasons:
        season = available_seasons[0]
    elif not season:
        season = "all"

    stmt = select(Match).where(Match.status == MatchStatus.FINISHED)
    if season != "all":
        stmt = stmt.join(Tournament).where(Tournament.season == season)
    all_matches = (await db.execute(stmt)).scalars().all()

    team_stats = {}
    for m in all_matches:
        if m.home_team_id not in team_stats:
            team_stats[m.home_team_id] = {'games': 0, 'wins': 0, 'clean_sheets': 0, 'goals': 0}
        if m.away_team_id not in team_stats:
            team_stats[m.away_team_id] = {'games': 0, 'wins': 0, 'clean_sheets': 0, 'goals': 0}

        h = team_stats[m.home_team_id]
        a = team_stats[m.away_team_id]
        h['games'] += 1
        a['games'] += 1
        h['goals'] += (m.home_score or 0)
        a['goals'] += (m.away_score or 0)

        if m.away_score == 0: h['clean_sheets'] += 1
        if m.home_score == 0: a['clean_sheets'] += 1

        if (m.home_score or 0) > (m.away_score or 0):
            h['wins'] += 1
        elif (m.away_score or 0) > (m.home_score or 0):
            a['wins'] += 1

    stmt_teams = select(Team)
    if search:
        stmt_teams = stmt_teams.where(Team.name.ilike(f"%{search}%"))
    all_teams = (await db.execute(stmt_teams)).scalars().all()

    teams_data = []
    for t in all_teams:
        if t.id in team_stats and team_stats[t.id]['games'] > 0:
            stats = team_stats[t.id]
            teams_data.append({
                "team": t,
                "games": stats['games'],
                "wins": stats['wins'],
                "clean_sheets": stats['clean_sheets'],
                "goals": stats['goals']
            })

    teams_data = sorted(teams_data, key=lambda x: x['team'].name.lower())

    total_teams = len(teams_data)
    total_pages = (total_teams + limit - 1) // limit if total_teams > 0 else 1
    paginated_teams = teams_data[skip:skip + limit]

    return await render_page(request, db, "participants.html", context={
        "teams_data": paginated_teams,
        "available_seasons": available_seasons,
        "current_season": season,
        "current_search": search or "",
        "page": page,
        "total_pages": total_pages
    })


@router.get("/participants/players")
async def participants_players_page(
        request: Request,
        page: int = 1,
        search: str = None,
        db: AsyncSession = Depends(get_db)
):
    limit = 50  # По сколько игроков выводить на страницу
    skip = (page - 1) * limit

    stats_subq = select(
        TeamPlayer.player_id,
        func.coalesce(func.sum(TeamPlayer.stats_games), 0).label('games'),
        func.coalesce(func.sum(TeamPlayer.stats_goals), 0).label('goals'),
        func.coalesce(func.sum(TeamPlayer.stats_assists), 0).label('assists'),
        func.coalesce(func.sum(TeamPlayer.stats_yellow_cards), 0).label('yc'),
        func.coalesce(func.sum(TeamPlayer.stats_red_cards), 0).label('rc')
    ).group_by(TeamPlayer.player_id).subquery()

    pen_subq = select(
        MatchEvent.player_id,
        func.count(MatchEvent.id).label('pen_goals')
    ).where(MatchEvent.event_type == MatchEventType.PENALTY_GOAL).group_by(MatchEvent.player_id).subquery()

    count_stmt = select(func.count()).select_from(Player)
    if search:
        count_stmt = count_stmt.where(
            or_(Player.last_name.ilike(f"%{search}%"), Player.first_name.ilike(f"%{search}%")))

    total_players = await db.scalar(count_stmt)
    total_pages = (total_players + limit - 1) // limit if total_players > 0 else 1

    stmt = select(
        Player,
        stats_subq.c.games,
        stats_subq.c.goals,
        stats_subq.c.assists,
        stats_subq.c.yc,
        stats_subq.c.rc,
        func.coalesce(pen_subq.c.pen_goals, 0).label('pen_goals')
    ).outerjoin(
        stats_subq, Player.id == stats_subq.c.player_id
    ).outerjoin(
        pen_subq, Player.id == pen_subq.c.player_id
    )

    if search:
        stmt = stmt.where(or_(Player.last_name.ilike(f"%{search}%"), Player.first_name.ilike(f"%{search}%")))

    stmt = stmt.order_by(func.coalesce(stats_subq.c.games, 0).desc()).offset(skip).limit(limit)

    results = (await db.execute(stmt)).all()

    players_data = []
    for row in results:
        p = row[0]
        g = row[1] or 0
        goals = row[2] or 0
        assists = row[3] or 0
        yc = row[4] or 0
        rc = row[5] or 0
        pen_goals = row[6] or 0

        players_data.append({
            "player": p,
            "games": g,
            "goals": goals,
            "assists": assists,
            "yc": yc,
            "rc": rc,
            "pen_goals": pen_goals,
            "avg_goals": round(goals / g, 2) if g > 0 else 0.0,
            "avg_assists": round(assists / g, 2) if g > 0 else 0.0,
            "goal_assist": goals + assists,
            "best_player": 0
        })

    return await render_page(request, db, "players_list.html", context={
        "players_data": players_data,
        "current_search": search or "",
        "page": page,
        "total_pages": total_pages
    })


@router.get("/player/{player_id}")
async def player_profile_page(player_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    player = await db.get(Player, player_id)
    if not player:
        raise HTTPException(status_code=404, detail="Игрок не найден")

    stmt_tps = select(TeamPlayer).where(TeamPlayer.player_id == player_id)
    tps = (await db.execute(stmt_tps)).scalars().all()

    total_games = sum(tp.stats_games or 0 for tp in tps)
    total_goals = sum(tp.stats_goals or 0 for tp in tps)
    total_assists = sum(tp.stats_assists or 0 for tp in tps)
    total_yc = sum(tp.stats_yellow_cards or 0 for tp in tps)
    total_rc = sum(tp.stats_red_cards or 0 for tp in tps)

    team_ids = list(set(tp.team_id for tp in tps))
    player_teams = []
    if team_ids:
        stmt_teams = select(Team).where(Team.id.in_(team_ids)).order_by(Team.name)
        player_teams = (await db.execute(stmt_teams)).scalars().all()

    history_data = []

    for tp in tps:
        tournament = await db.get(Tournament, tp.tournament_id)
        team = await db.get(Team, tp.team_id)
        if not tournament or not team:
            continue

        stmt_matches = select(Match).where(
            Match.tournament_id == tournament.id,
            or_(Match.home_team_id == team.id, Match.away_team_id == team.id),
            Match.status == MatchStatus.FINISHED
        ).options(
            selectinload(Match.home_team),
            selectinload(Match.away_team)
        )
        matches = (await db.execute(stmt_matches)).scalars().all()

        if not matches:
            continue

        stmt_events = select(MatchEvent).where(MatchEvent.player_id == player_id)
        events = (await db.execute(stmt_events)).scalars().all()
        events_by_match = {}
        for e in events:
            events_by_match.setdefault(e.match_id, []).append(e)

        tournament_matches = []
        for m in matches:
            is_home = m.home_team_id == team.id
            team_score = m.home_score if is_home else m.away_score
            opp_score = m.away_score if is_home else m.home_score

            if team_score is not None and opp_score is not None:
                res = "В" if team_score > opp_score else ("П" if team_score < opp_score else "Н")
                score_str = f"{team_score} - {opp_score}"
            else:
                res = "-"
                score_str = "- : -"

            tournament_matches.append({
                "match": m,
                "opponent": m.away_team if is_home else m.home_team,
                "result": res,
                "score": score_str,
                "is_home": is_home,
                "events": events_by_match.get(m.id, [])
            })

        tournament_matches.sort(
            key=lambda x: x["match"].match_date.timestamp() if x["match"].match_date else 0,
            reverse=True
        )

        history_data.append({
            "tournament": tournament,
            "team": team,
            "matches": tournament_matches
        })

    history_data.sort(
        key=lambda x: x["matches"][0]["match"].match_date.timestamp() if x["matches"][0]["match"].match_date else 0,
        reverse=True
    )

    return await render_page(request, db, "player_detail.html", context={
        "player": player,
        "player_teams": player_teams,  # <--- Передаем команды
        "history_data": history_data,
        "stats": {
            "games": total_games,
            "goals": total_goals,
            "assists": total_assists,
            "yellow_cards": total_yc,
            "red_cards": total_rc
        }
    })


from app.models.coach import Coach
@router.get("/participants/coaches")
async def participants_coaches_page(request: Request, search: str = None, db: AsyncSession = Depends(get_db)):
    stmt = select(Coach).options(selectinload(Coach.team)).where(Coach.is_active == True)
    if search:
        stmt = stmt.where(or_(
            Coach.last_name.ilike(f"%{search}%"),
            Coach.first_name.ilike(f"%{search}%")
        ))
    stmt = stmt.order_by(Coach.last_name)
    coaches = (await db.execute(stmt)).scalars().all()

    coaches_data = []
    for c in coaches:
        if c.team:
            matches = (await db.execute(select(Match).where(
                or_(Match.home_team_id == c.team_id, Match.away_team_id == c.team_id),
                Match.status == MatchStatus.FINISHED
            ))).scalars().all()

            games = len(matches)
            wins = sum(1 for m in matches if (
                    (m.home_team_id == c.team_id and m.home_score > m.away_score) or
                    (m.away_team_id == c.team_id and m.away_score > m.home_score)
            ))
            draws = sum(1 for m in matches if m.home_score == m.away_score)
            losses = games - wins - draws
        else:
            games = wins = draws = losses = 0

        coaches_data.append({
            "coach": c,
            "games": games,
            "wins": wins,
            "draws": draws,
            "losses": losses
        })

    return await render_page(request, db, "coaches.html", context={
        "coaches_data": coaches_data,
        "current_search": search or ""
    })

@router.get("/news")
async def news_page(
    request: Request,
    page: int = 1,
    tournament_id: str = None,
    team_id: str = None,
    db: AsyncSession = Depends(get_db)
):
    t_id = int(tournament_id) if tournament_id and tournament_id.isdigit() else None
    tm_id = int(team_id) if team_id and team_id.isdigit() else None

    limit = 10
    skip = (page - 1) * limit

    tournaments = (await db.execute(select(Tournament).order_by(Tournament.name))).scalars().all()
    teams = (await db.execute(select(Team).order_by(Team.name))).scalars().all()

    stmt = select(News).where(News.is_published == True)
    count_stmt = select(func.count()).select_from(News).where(News.is_published == True)

    if t_id:
        stmt = stmt.where(News.tournament_id == t_id)
        count_stmt = count_stmt.where(News.tournament_id == t_id)

    if tm_id:
        stmt = stmt.join(News.teams).where(Team.id == tm_id)
        count_stmt = count_stmt.join(News.teams).where(Team.id == tm_id)

    stmt = stmt.options(
        selectinload(News.teams),
        selectinload(News.tournament)
    ).order_by(
        News.sort_order.asc(),
        News.published_at.desc(),
        News.id.desc()
    ).offset(skip).limit(limit)

    total_news = await db.scalar(count_stmt)
    news_list = (await db.execute(stmt)).scalars().all()
    total_pages = (total_news + limit - 1) // limit if total_news > 0 else 1

    return await render_page(request, db, "news.html", context={
        "news_list": news_list,
        "page": page,
        "total_pages": total_pages,
        "tournaments": tournaments,
        "teams": teams,
        "selected_tournament": t_id, # Передаем уже преобразованный ID
        "selected_team": tm_id       # Передаем уже преобразованный ID
    })


@router.get("/news/{news_id}")
async def news_detail_page(news_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    stmt = select(News).where(News.id == news_id).options(
        selectinload(News.teams),
        selectinload(News.tournament)
    )
    news_item = (await db.execute(stmt)).scalar_one_or_none()

    if not news_item:
        raise HTTPException(status_code=404, detail="Новость не найдена")

    return await render_page(request, db, "news_detail.html", context={"news": news_item})


@router.get("/media")
async def media_page(
        request: Request,
        tournament_id: int = None,  # <--- Принимаем ID из URL
        db: AsyncSession = Depends(get_db)
):
    stmt = select(MediaAlbum).options(selectinload(MediaAlbum.items)).order_by(MediaAlbum.created_at.desc())

    tournament = None
    if tournament_id:
        stmt = stmt.where(MediaAlbum.tournament_id == tournament_id)
        tournament = await db.get(Tournament, tournament_id)

    albums = (await db.execute(stmt)).scalars().all()

    return await render_page(request, db, "media.html", context={
        "albums": albums,
        "tournament": tournament  # Передаем турнир, чтобы вывести его название
    })


@router.get("/matches/{match_id}")
async def match_protocol_page(match_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    stmt = select(Match).where(Match.id == match_id).options(
        selectinload(Match.home_team),
        selectinload(Match.away_team),
        selectinload(Match.tournament),
        selectinload(Match.events).selectinload(MatchEvent.player),
        selectinload(Match.events).selectinload(MatchEvent.team)
    )
    match = (await db.execute(stmt)).scalar_one_or_none()

    if not match:
        raise HTTPException(status_code=404, detail="Матч не найден")

    if match.events:
        match.events.sort(key=lambda e: e.minute)

    home_roster = await crud_team_player.get_by_tournament_and_team(
        db, tournament_id=match.tournament_id, team_id=match.home_team_id
    )
    away_roster = await crud_team_player.get_by_tournament_and_team(
        db, tournament_id=match.tournament_id, team_id=match.away_team_id
    )

    stmt_standings = select(TournamentStanding).where(
        TournamentStanding.tournament_id == match.tournament_id
    ).order_by(TournamentStanding.points.desc(), TournamentStanding.goals_for.desc())
    standings = (await db.execute(stmt_standings)).scalars().all()

    home_pos = next((i + 1 for i, s in enumerate(standings) if s.team_id == match.home_team_id), None)
    away_pos = next((i + 1 for i, s in enumerate(standings) if s.team_id == match.away_team_id), None)
    home_standing = next((s for s in standings if s.team_id == match.home_team_id), None)
    away_standing = next((s for s in standings if s.team_id == match.away_team_id), None)

    top_scorers = await StatsService.get_top_scorers(db, match.tournament_id, limit=1)
    top_scorer = top_scorers[0] if top_scorers else None

    h2h_stmt = select(Match).where(
        or_(
            and_(Match.home_team_id == match.home_team_id, Match.away_team_id == match.away_team_id),
            and_(Match.home_team_id == match.away_team_id, Match.away_team_id == match.home_team_id)
        ),
        Match.id != match.id,
        Match.status == MatchStatus.FINISHED
    ).options(
        selectinload(Match.home_team),
        selectinload(Match.away_team)
    ).order_by(Match.match_date.desc()).limit(5)
    h2h_matches = (await db.execute(h2h_stmt)).scalars().all()

    home_recent_query = select(Match).where(
        or_(Match.home_team_id == match.home_team_id, Match.away_team_id == match.home_team_id),
        Match.status == MatchStatus.FINISHED,
        Match.id != match.id
    )
    if match.match_date:
        home_recent_query = home_recent_query.where(Match.match_date < match.match_date)

    home_recent_stmt = home_recent_query.options(
        selectinload(Match.home_team),
        selectinload(Match.away_team)
    ).order_by(Match.match_date.desc()).limit(5)
    home_recent_matches = (await db.execute(home_recent_stmt)).scalars().all()

    away_recent_query = select(Match).where(
        or_(Match.home_team_id == match.away_team_id, Match.away_team_id == match.away_team_id),
        Match.status == MatchStatus.FINISHED,
        Match.id != match.id
    )
    if match.match_date:
        away_recent_query = away_recent_query.where(Match.match_date < match.match_date)

    away_recent_stmt = away_recent_query.options(
        selectinload(Match.home_team),
        selectinload(Match.away_team)
    ).order_by(Match.match_date.desc()).limit(5)
    away_recent_matches = (await db.execute(away_recent_stmt)).scalars().all()

    home_players_subq = select(TeamPlayer.player_id).where(TeamPlayer.team_id == match.home_team_id)
    away_players_subq = select(TeamPlayer.player_id).where(TeamPlayer.team_id == match.away_team_id)

    common_players_stmt = select(Player).where(
        Player.id.in_(home_players_subq),
        Player.id.in_(away_players_subq)
    ).order_by(Player.last_name)
    common_players = (await db.execute(common_players_stmt)).scalars().all()

    events_by_player = {}
    if match.events:
        for e in match.events:
            if e.player_id:
                if e.player_id not in events_by_player:
                    events_by_player[e.player_id] = []
                events_by_player[e.player_id].append(e)

    return await render_page(request, db, "match_detail.html", context={
        "match": match,
        "home_roster": home_roster,
        "away_roster": away_roster,
        "home_standing": home_standing,
        "away_standing": away_standing,
        "home_pos": home_pos,
        "away_pos": away_pos,
        "top_scorer": top_scorer,
        "h2h_matches": h2h_matches,
        "events_by_player": events_by_player,
        "home_recent_matches": home_recent_matches,
        "away_recent_matches": away_recent_matches,
        "common_players": common_players,  # <--- Передаем общих игроков
        "active_tab": request.query_params.get("tab", "preview")
    })


@router.get("/stadiums")
async def stadiums_page(request: Request, db: AsyncSession = Depends(get_db)):
    stadiums = await crud_stadium.get_multi(db, limit=100)
    return await render_page(request, db, "stadiums.html", context={"stadiums": stadiums})


from app.schemas.stadium import StadiumCreate, StadiumUpdate # используем как референс для форм
from app.models.staff import Staff
from collections import OrderedDict


@router.get("/management")
async def management_page(request: Request, db: AsyncSession = Depends(get_db)):
    staff_list = (await db.execute(
        select(Staff).where(Staff.is_active == True).order_by(Staff.sort_order.asc(), Staff.id.asc())
    )).scalars().all()

    grouped_staff = OrderedDict()
    for person in staff_list:
        if person.rank not in grouped_staff:
            grouped_staff[person.rank] = []
        grouped_staff[person.rank].append(person)

    return await render_page(request, db, "management.html", context={"grouped_staff": grouped_staff})


# Сотрудники

@router.get("/admin/staff")
async def admin_staff_list(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    staff_list = (await db.execute(
        select(Staff).order_by(Staff.sort_order.asc(), Staff.id.asc())
    )).scalars().all()

    all_ranks = sorted(list(set(s.rank for s in staff_list)))
    all_positions = sorted(list(set(s.position for s in staff_list)))

    return await render_page(request, db, "admin_staff.html", context={
        "user": user,
        "staff_list": staff_list,
        "all_ranks": all_ranks,
        "all_positions": all_positions
    })


@router.get("/admin/staff/create")
async def admin_staff_create_form(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    staff_list = (await db.execute(select(Staff))).scalars().all()
    all_ranks = sorted(list(set(s.rank for s in staff_list)))
    all_positions = sorted(list(set(s.position for s in staff_list)))

    return await render_page(request, db, "admin_staff_form.html", context={
        "user": user, "staff": None, "all_ranks": all_ranks, "all_positions": all_positions
    })


@router.post("/admin/staff/create")
async def admin_staff_create(
        request: Request,
        full_name: str = Form(...),
        rank: str = Form(...),  # Новое поле
        position: str = Form(...),
        social_url: str = Form(None),
        is_active: bool = Form(False),
        photo: UploadFile = File(None),
        db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    photo_url = await save_upload_file(photo, "staff")

    new_staff = Staff(
        full_name=full_name,
        rank=rank,
        position=position,
        social_url=social_url,
        photo_url=photo_url,
        is_active=is_active
    )
    db.add(new_staff)
    await db.commit()
    return RedirectResponse(url="/admin/staff", status_code=303)


@router.get("/admin/staff/{staff_id}/edit")
async def admin_staff_edit_form(staff_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    staff = await db.get(Staff, staff_id)
    if not staff: return RedirectResponse(url="/admin/staff", status_code=303)

    staff_list = (await db.execute(select(Staff))).scalars().all()
    all_ranks = sorted(list(set(s.rank for s in staff_list)))
    all_positions = sorted(list(set(s.position for s in staff_list)))

    return await render_page(request, db, "admin_staff_form.html", context={
        "user": user, "staff": staff, "all_ranks": all_ranks, "all_positions": all_positions
    })


@router.post("/admin/staff/{staff_id}/edit")
async def admin_staff_edit(
        staff_id: int,
        request: Request,
        full_name: str = Form(...),
        rank: str = Form(...),  # Новое поле
        position: str = Form(...),
        social_url: str = Form(None),
        is_active: bool = Form(False),
        photo: UploadFile = File(None),
        db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    staff = await db.get(Staff, staff_id)
    if not staff: return RedirectResponse(url="/admin/staff", status_code=303)

    new_photo_url = await save_upload_file(photo, "staff")
    if new_photo_url:
        staff.photo_url = new_photo_url

    staff.full_name = full_name
    staff.rank = rank
    staff.position = position
    staff.social_url = social_url
    staff.is_active = is_active

    db.add(staff)
    await db.commit()
    return RedirectResponse(url="/admin/staff", status_code=303)


@router.post("/admin/staff/{staff_id}/delete")
async def admin_staff_delete(staff_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    staff = await db.get(Staff, staff_id)
    if staff:
        await db.delete(staff)
        await db.commit()
    return RedirectResponse(url="/admin/staff", status_code=303)


# Публичные страницы

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
    from app.models.disqualification import Disqualification
    from sqlalchemy.orm import selectinload
    from sqlalchemy import select

    stmt = select(Disqualification).where(
        Disqualification.matches_remaining > 0
    ).options(
        selectinload(Disqualification.player),
        selectinload(Disqualification.team),
        selectinload(Disqualification.tournament)
    ).order_by(
        Disqualification.matches_remaining.desc(),
        Disqualification.decision_date.desc()
    )

    active_disq = (await db.execute(stmt)).scalars().all()

    return await render_page(request, db, "disciplinary.html", context={
        "active_disq": active_disq
    })


# Авторизация

@router.get("/login")
async def login_page(request: Request, db: AsyncSession = Depends(get_db)):
    return await render_page(request, db, "login.html")


@router.post("/web-login")
async def web_login(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        db: AsyncSession = Depends(get_db),
):
    user = await crud_user.get_by_email(db, email=username)

    if not user or not verify_password(password, user.hashed_password) or user.role != UserRole.ADMIN:
        return RedirectResponse(url="/login?error=1", status_code=303)

    access_token = create_access_token(subject=user.email)
    response = RedirectResponse(url="/admin", status_code=303)

    is_secure = request.url.scheme == "https"

    response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        samesite="lax",
        secure=is_secure,
        max_age=60 * 60 * 24 * 7,
    )

    return response


@router.get("/logout")
async def logout():
    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie("access_token")
    return response


# Административная панель

@router.get("/admin")
async def admin_panel(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    from datetime import date
    from sqlalchemy import func
    from app.models.disqualification import Disqualification

    total_tournaments = await db.scalar(select(func.count()).select_from(Tournament))
    total_teams = await db.scalar(select(func.count()).select_from(Team))

    today = date.today()
    today_matches_count = await db.scalar(
        select(func.count()).select_from(Match).where(func.date(Match.match_date) == today)
    )

    active_disq_count = await db.scalar(
        select(func.count()).select_from(Disqualification).where(Disqualification.matches_remaining > 0)
    )

    stmt_matches = select(Match).where(
        Match.status.in_([MatchStatus.SCHEDULED, MatchStatus.LIVE]),
        func.date(Match.match_date) >= today
    ).options(
        selectinload(Match.home_team),
        selectinload(Match.away_team),
        selectinload(Match.tournament)
    ).order_by(Match.match_date.asc()).limit(6)

    upcoming_matches = (await db.execute(stmt_matches)).scalars().all()

    stmt_tours = select(Tournament).where(
        Tournament.status == TournamentStatus.ACTIVE
    ).order_by(Tournament.sort_order).limit(5)
    active_tournaments = (await db.execute(stmt_tours)).scalars().all()

    return await render_page(request, db, "admin.html", context={
        "user": user,
        "total_tournaments": total_tournaments,
        "total_teams": total_teams,
        "today_matches_count": today_matches_count,
        "active_disq_count": active_disq_count,
        "upcoming_matches": upcoming_matches,
        "active_tournaments": active_tournaments
    })


# Партнёры

@router.get("/admin/partners")
async def admin_partners_list(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    partners = (await db.execute(select(Partner).order_by(Partner.sort_order, Partner.id))).scalars().all()
    return await render_page(request, db, "admin_partners.html", context={"user": user, "partners": partners})


@router.get("/admin/partners/create")
async def admin_partner_create_form(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user
    return await render_page(request, db, "admin_partner_form.html", context={"user": user, "partner": None})


@router.post("/admin/partners/create")
async def admin_partner_create(
        request: Request, name: str = Form(...), url: str = Form(None),
        sort_order: int = Form(0), is_active: bool = Form(False),
        logo: UploadFile = File(...), db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    logo_url = await save_upload_file(logo, "partners")

    new_partner = Partner(
        name=name, url=url, logo_url=logo_url, sort_order=sort_order, is_active=is_active
    )
    db.add(new_partner)
    await db.commit()
    return RedirectResponse(url="/admin/partners", status_code=303)


@router.get("/admin/partners/{partner_id}/edit")
async def admin_partner_edit_form(partner_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    partner = await db.get(Partner, partner_id)
    if not partner: return RedirectResponse(url="/admin/partners", status_code=303)
    return await render_page(request, db, "admin_partner_form.html", context={"user": user, "partner": partner})


@router.post("/admin/partners/{partner_id}/edit")
async def admin_partner_edit(
        partner_id: int, request: Request, name: str = Form(...), url: str = Form(None),
        sort_order: int = Form(0), is_active: bool = Form(False),
        logo: UploadFile = File(None), db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    partner = await db.get(Partner, partner_id)
    if not partner: return RedirectResponse(url="/admin/partners", status_code=303)

    new_logo_url = await save_upload_file(logo, "partners")
    if new_logo_url:
        partner.logo_url = new_logo_url

    partner.name = name
    partner.url = url
    partner.sort_order = sort_order
    partner.is_active = is_active

    db.add(partner)
    await db.commit()
    return RedirectResponse(url="/admin/partners", status_code=303)


@router.post("/admin/partners/{partner_id}/delete")
async def admin_partner_delete(partner_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    partner = await db.get(Partner, partner_id)
    if partner:
        await db.delete(partner)
        await db.commit()

    return RedirectResponse(url="/admin/partners", status_code=303)


# Стадионы

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


# Команды

@router.get("/admin/teams")
async def admin_teams_list(
        request: Request,
        show_all: bool = False,  # По умолчанию показываем только активных
        db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    from app.models.tournament_team import TournamentTeam
    from app.models.tournament import Tournament
    from app.models.enums import TournamentStatus

    active_tt_stmt = select(TournamentTeam.team_id).join(Tournament).where(
        Tournament.status == TournamentStatus.ACTIVE
    )
    active_team_ids = (await db.execute(active_tt_stmt)).scalars().all()

    await db.execute(update(Team).values(is_active=False))
    if active_team_ids:
        await db.execute(update(Team).where(Team.id.in_(active_team_ids)).values(is_active=True))
    await db.commit()

    stmt = select(Team)

    if not show_all:
        stmt = stmt.where(Team.is_active == True)

    stmt = stmt.order_by(Team.name).limit(100)
    teams = (await db.execute(stmt)).scalars().all()

    return await render_page(request, db, "admin_teams.html", context={
        "user": user,
        "teams": teams,
        "show_all": show_all
    })


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


@router.get("/admin/teams/merge")
async def admin_teams_merge_form(
        request: Request,
        source_id: int = None,
        target_id: int = None,
        db: AsyncSession = Depends(get_db),
):
    """Форма объединения команд + предпросмотр переноса."""
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse):
        return user

    teams = (await db.execute(select(Team).order_by(Team.name))).scalars().all()

    preview = None
    if source_id and target_id and source_id != target_id:
        source = await db.get(Team, source_id)
        target = await db.get(Team, target_id)

        if source and target:
            matches_count = await db.scalar(
                select(func.count()).select_from(Match).where(
                    or_(Match.home_team_id == source_id, Match.away_team_id == source_id)
                )
            )
            events_count = await db.scalar(
                select(func.count()).select_from(MatchEvent).where(MatchEvent.team_id == source_id)
            )
            tournaments_count = await db.scalar(
                select(func.count()).select_from(TournamentTeam).where(TournamentTeam.team_id == source_id)
            )
            players_count = await db.scalar(
                select(func.count()).select_from(TeamPlayer).where(TeamPlayer.team_id == source_id)
            )
            news_count = await db.scalar(
                select(func.count()).select_from(news_teams).where(news_teams.c.team_id == source_id)
            )

            preview = {
                "source": source,
                "target": target,
                "matches": matches_count or 0,
                "events": events_count or 0,
                "tournaments": tournaments_count or 0,
                "players": players_count or 0,
                "news": news_count or 0,
            }

    return await render_page(request, db, "admin_teams_merge.html", context={
        "user": user,
        "teams": teams,
        "preview": preview,
        "selected_source": source_id,
        "selected_target": target_id,
    })


@router.post("/admin/teams/merge")
async def admin_teams_merge(
        request: Request,
        source_id: int = Form(...),
        target_id: int = Form(...),
        db: AsyncSession = Depends(get_db),
):
    """Слить команду source_id в target_id (source удаляется)."""
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse):
        return user

    if source_id == target_id:
        raise HTTPException(status_code=400, detail="Нельзя объединить команду саму с собой")

    source = await db.get(Team, source_id)
    target = await db.get(Team, target_id)
    if not source or not target:
        raise HTTPException(status_code=404, detail="Команда не найдена")

    await db.execute(update(Match).where(Match.home_team_id == source_id).values(home_team_id=target_id))
    await db.execute(update(Match).where(Match.away_team_id == source_id).values(away_team_id=target_id))

    await db.execute(update(MatchEvent).where(MatchEvent.team_id == source_id).values(team_id=target_id))

    await db.execute(
        update(Disqualification).where(Disqualification.team_id == source_id).values(team_id=target_id)
    )

    source_tps = (
        await db.execute(select(TeamPlayer).where(TeamPlayer.team_id == source_id))
    ).scalars().all()

    for tp in source_tps:
        existing = await db.scalar(select(TeamPlayer).where(
            TeamPlayer.player_id == tp.player_id,
            TeamPlayer.team_id == target_id,
            TeamPlayer.tournament_id == tp.tournament_id,
        ))
        if existing:
            existing.stats_games += tp.stats_games or 0
            existing.stats_goals += tp.stats_goals or 0
            existing.stats_assists += tp.stats_assists or 0
            existing.stats_yellow_cards += tp.stats_yellow_cards or 0
            existing.stats_red_cards += tp.stats_red_cards or 0
            db.add(existing)
            await db.delete(tp)
        else:
            tp.team_id = target_id

    source_tts = (
        await db.execute(select(TournamentTeam).where(TournamentTeam.team_id == source_id))
    ).scalars().all()
    affected_tournaments = set()

    for tt in source_tts:
        affected_tournaments.add(tt.tournament_id)
        existing = await db.scalar(select(TournamentTeam).where(
            TournamentTeam.tournament_id == tt.tournament_id,
            TournamentTeam.team_id == target_id,
        ))
        if existing:
            await db.delete(tt)  # уже есть — дубль убираем
        else:
            tt.team_id = target_id

    await db.execute(update(Coach).where(Coach.team_id == source_id).values(team_id=target_id))

    await db.execute(update(User).where(User.team_id == source_id).values(team_id=target_id))

    existing_news_ids = {
        row[0] for row in (
            await db.execute(select(news_teams.c.news_id).where(news_teams.c.team_id == target_id))
        ).all()
    }
    source_news_ids = [
        row[0] for row in (
            await db.execute(select(news_teams.c.news_id).where(news_teams.c.team_id == source_id))
        ).all()
    ]
    for news_id in source_news_ids:
        if news_id in existing_news_ids:
            await db.execute(news_teams.delete().where(
                (news_teams.c.news_id == news_id) & (news_teams.c.team_id == source_id)
            ))
        else:
            await db.execute(news_teams.update().where(
                (news_teams.c.news_id == news_id) & (news_teams.c.team_id == source_id)
            ).values(team_id=target_id))

    await db.execute(delete(TournamentStanding).where(TournamentStanding.team_id == source_id))

    await db.execute(delete(Team).where(Team.id == source_id))

    await db.commit()

    from app.services.stats_recalc import StatsRecalculator
    for t_id in affected_tournaments:
        await StatsRecalculator.recalculate_all(db, t_id)

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


# Турниры

@router.get("/admin/tournaments")
async def admin_tournaments_list(
        request: Request,
        search: str = None,
        season: str = None,
        db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    await check_and_finish_expired_tournaments(db)

    from sqlalchemy.orm import selectinload
    stmt = select(Tournament).options(
        selectinload(Tournament.matches).selectinload(Match.home_team),
        selectinload(Tournament.matches).selectinload(Match.away_team)
    )

    if search:
        stmt = stmt.where(Tournament.name.ilike(f"%{search}%"))
    if season:
        stmt = stmt.where(Tournament.season == season)

    tournaments = (await db.execute(stmt)).unique().scalars().all()

    def sort_key(t):
        try:
            season_num = int(t.season)
        except (ValueError, TypeError):
            season_num = 0
        return (-season_num, t.sort_order, t.name)

    tournaments = sorted(tournaments, key=sort_key)

    from datetime import datetime, timezone

    def safe_timestamp(dt):
        """Конвертирует naive и aware даты в единый timestamp, чтобы избежать TypeError."""
        if dt is None:
            return 0
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc).timestamp()
        return dt.timestamp()

    for t in tournaments:
        t.matches.sort(
            key=lambda m: (
                safe_timestamp(m.match_date),
                m.round or 0,
                m.id
            ),
            reverse=True
        )

    seasons_result = await db.execute(
        select(Tournament.season).distinct().order_by(Tournament.season.desc())
    )
    available_seasons = [s[0] for s in seasons_result.all() if s[0]]

    return await render_page(request, db, "admin_tournaments.html", context={
        "user": user,
        "tournaments": tournaments,
        "available_seasons": available_seasons,
        "current_search": search or "",
        "current_season": season or ""
    })


@router.get("/admin/tournaments/create")
async def admin_tournament_create_form(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    stmt_cats = select(Tournament.menu_category).where(
        Tournament.menu_category.isnot(None),
        Tournament.menu_category != ""
    ).distinct()
    existing_categories = (await db.execute(stmt_cats)).scalars().all()

    return await render_page(request, db, "admin_tournament_form.html", context={
        "user": user,
        "tournament": None,
        "statuses": TournamentStatus,
        "formats": TournamentFormat,
        "existing_categories": existing_categories
    })


@router.post("/admin/tournaments/create")
async def admin_tournament_create(
        request: Request,
        name: str = Form(...),
        short_name: str = Form(None),
        menu_category: str = Form(None),
        show_in_menu: bool = Form(False),
        description: str = Form(None),
        start_at: str = Form(None),
        end_at: str = Form(None),
        division: str = Form(...),
        season: str = Form(...),
        age_category: str = Form(...),
        status: TournamentStatus = Form(...),
        max_teams: int = Form(...),
        tournament_format: str = Form(None),
        sort_order: int = Form(0),
        stats_style: str = Form("default"),
        logo: UploadFile = File(None),
        db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    logo_url = await save_upload_file(logo, "tournaments")

    try:
        from zoneinfo import ZoneInfo
        local_tz = ZoneInfo("Asia/Almaty")
    except Exception:
        local_tz = timezone(timedelta(hours=5))

    start_date = None
    if start_at and start_at.strip():
        dt = datetime.strptime(start_at, "%Y-%m-%d")
        start_date = dt.replace(tzinfo=local_tz).astimezone(timezone.utc)

    end_date = None
    if end_at and end_at.strip():
        dt = datetime.strptime(end_at, "%Y-%m-%d")
        end_date = dt.replace(tzinfo=local_tz).astimezone(timezone.utc)

    fmt = None
    if tournament_format:
        try:
            fmt = TournamentFormat(tournament_format)
        except ValueError:
            pass

    tournament_in = TournamentCreate(
        name=name, short_name=short_name, menu_category=menu_category,
        show_in_menu=show_in_menu, description=description,
        start_at=start_date, end_at=end_date,
        division=division, season=season, age_category=age_category,
        status=status, max_teams=max_teams, logo_url=logo_url,
        tournament_format=fmt, sort_order=sort_order,
        stats_style=stats_style
    )

    await crud_tournament.create(db, obj_in=tournament_in)
    return RedirectResponse(url="/admin/tournaments", status_code=303)


@router.get("/admin/tournaments/{tournament_id}/edit")
async def admin_tournament_edit_form(tournament_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    tournament = await crud_tournament.get(db, tournament_id)
    if not tournament:
        return RedirectResponse(url="/admin/tournaments", status_code=303)

    stmt_cats = select(Tournament.menu_category).where(
        Tournament.menu_category.isnot(None),
        Tournament.menu_category != ""
    ).distinct()
    existing_categories = (await db.execute(stmt_cats)).scalars().all()

    return await render_page(request, db, "admin_tournament_form.html", context={
        "user": user,
        "tournament": tournament,
        "statuses": TournamentStatus,
        "formats": TournamentFormat,
        "existing_categories": existing_categories
    })


@router.post("/admin/tournaments/{tournament_id}/edit")
async def admin_tournament_edit(
        tournament_id: int, request: Request,
        name: str = Form(...),
        short_name: str = Form(None),
        menu_category: str = Form(None),
        show_in_menu: bool = Form(False),
        description: str = Form(None),
        start_at: str = Form(None),
        end_at: str = Form(None),
        division: str = Form(...),
        season: str = Form(...),
        age_category: str = Form(...),
        status: TournamentStatus = Form(...),
        max_teams: int = Form(...),
        tournament_format: str = Form(None),
        sort_order: int = Form(0),
        stats_style: str = Form("default"),
        logo: UploadFile = File(None),
        db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    tournament = await crud_tournament.get(db, tournament_id)
    if not tournament:
        return RedirectResponse(url="/admin/tournaments", status_code=303)

    new_logo_url = await save_upload_file(logo, "tournaments")
    final_logo_url = new_logo_url if new_logo_url else tournament.logo_url

    start_date = datetime.fromisoformat(start_at) if start_at else None
    end_date = datetime.fromisoformat(end_at) if end_at else None

    fmt = None
    if tournament_format:
        try:
            fmt = TournamentFormat(tournament_format)
        except ValueError:
            pass

    tournament_in = TournamentUpdate(
        name=name, short_name=short_name, menu_category=menu_category,
        show_in_menu=show_in_menu, description=description,
        start_at=start_date, end_at=end_date,
        division=division, season=season, age_category=age_category,
        status=status, max_teams=max_teams, logo_url=final_logo_url,
        tournament_format=fmt, sort_order=sort_order,
        stats_style=stats_style  # <--- ПЕРЕДАЕМ В БАЗУ
    )
    await crud_tournament.update(db, db_obj=tournament, obj_in=tournament_in)

    if status == TournamentStatus.FINISHED:
        existing_news = await db.scalar(
            select(News).where(News.tournament_id == tournament_id, News.title.ilike("%Чемпион%"))
        )

        if not existing_news:
            stmt = select(TournamentStanding).where(
                TournamentStanding.tournament_id == tournament_id
            ).options(selectinload(TournamentStanding.team)).order_by(
                TournamentStanding.points.desc(),
                (TournamentStanding.goals_for - TournamentStanding.goals_against).desc(),
                TournamentStanding.goals_for.desc()
            ).limit(1)

            winner_standing = (await db.execute(stmt)).scalar_one_or_none()

            if winner_standing and winner_standing.team:
                winner_team = winner_standing.team
                card_url = None

                try:
                    from app.services.champion_card import generate_champion_card
                    card_url = await generate_champion_card(db, tournament_id, prompt_index=0)
                except Exception as e:
                    print(f"  ⚠️ AI-карточка не сгенерирована: {e}")

                news_title = f"{winner_team.name} - Чемпион {tournament.name}"

                import re
                slug = re.sub(r'[^a-z0-9]+', '-', news_title.lower()).strip('-')
                if not slug:
                    slug = f"champion-{tournament_id}-{int(datetime.now().timestamp())}"

                hero_image = card_url or winner_team.logo_url or '/static/default_team.png'

                news_content = f"""
                <div class="text-center mb-4">
                    <img src="{winner_team.logo_url or '/static/default_team.png'}" style="max-height: 150px; object-fit: contain;" class="mb-3">
                    <h3 class="fw-bold">Поздравляем <span class="text-primary">{winner_team.name}</span> с победой!</h3>
                    <p class="lead">Команда становится победителем турнира <b>{tournament.name}</b> в сезоне {tournament.season}.</p>
                </div>
                <div class="alert alert-success text-center">
                    <h4 class="mb-0">Очки: {winner_standing.points} | Победы: {winner_standing.wins} | Забито: {winner_standing.goals_for}</h4>
                </div>
                """

                new_news = News(
                    title=news_title, slug=slug, content=news_content,
                    image_url=hero_image, published_at=datetime.now(timezone.utc),
                    is_published=True, tournament_id=tournament_id
                )

                new_news.teams.append(winner_team)
                db.add(new_news)
                await db.commit()

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


# Матчи

@router.get("/admin/matches")
async def admin_matches_list(
        request: Request,
        search: str = None,
        tournament_id: str = None,
        date_from: str = None,
        date_to: str = None,
        db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    from sqlalchemy import case, or_
    from datetime import datetime, timezone, timedelta
    from zoneinfo import ZoneInfo

    now = datetime.now(timezone.utc)

    stmt = select(Match).join(Tournament).options(
        selectinload(Match.home_team),
        selectinload(Match.away_team),
        selectinload(Match.tournament)
    )

    if search:
        stmt = stmt.where(
            or_(
                Match.home_team.has(Team.name.ilike(f"%{search}%")),
                Match.away_team.has(Team.name.ilike(f"%{search}%"))
            )
        )

    if tournament_id and tournament_id.strip().isdigit():
        stmt = stmt.where(Match.tournament_id == int(tournament_id))

    try:
        local_tz = ZoneInfo("Asia/Almaty")
    except Exception:
        local_tz = timezone(timedelta(hours=5))

    if date_from:
        try:
            dt_from = datetime.strptime(date_from, "%Y-%m-%d").replace(hour=0, minute=0, second=0, tzinfo=local_tz)
            stmt = stmt.where(Match.match_date >= dt_from.astimezone(timezone.utc))
        except ValueError:
            pass

    if date_to:
        try:
            dt_to = datetime.strptime(date_to, "%Y-%m-%d").replace(hour=23, minute=59, second=59, tzinfo=local_tz)
            stmt = stmt.where(Match.match_date <= dt_to.astimezone(timezone.utc))
        except ValueError:
            pass


    status_group = case(
        (Match.status.in_([MatchStatus.SCHEDULED, MatchStatus.LIVE]), 0),
        else_=1
    )

    date_group = case(
        (Match.match_date.is_(None), 0),
        else_=1
    )

    active_date_asc = case(
        (Match.status.in_([MatchStatus.SCHEDULED, MatchStatus.LIVE]), Match.match_date),
        else_=None
    ).asc().nullslast()

    finished_date_desc = case(
        (Match.match_date.isnot(None) & ~Match.status.in_([MatchStatus.SCHEDULED, MatchStatus.LIVE]), Match.match_date),
        else_=None
    ).desc().nullslast()

    stmt = stmt.order_by(
        status_group,
        date_group,
        active_date_asc,
        finished_date_desc,
        Match.id.desc()
    )

    stmt = stmt.limit(100)

    matches = (await db.execute(stmt)).unique().scalars().all()

    for m in matches:
        m.local_match_date = to_local_time(m.match_date)

    tournaments = (await db.execute(select(Tournament).order_by(Tournament.name))).scalars().all()

    return await render_page(request, db, "admin_matches.html", context={
        "user": user,
        "matches": matches,
        "tournaments": tournaments,
        "current_search": search or "",
        "current_tournament_id": tournament_id or "",
        "current_date_from": date_from or "",
        "current_date_to": date_to or ""
    })


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
        stage: str = Form(None), stage_name: str = Form(None),
        db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    if home_team_id == away_team_id:
        raise HTTPException(status_code=400, detail="Команда не может играть сама с собой")

    from zoneinfo import ZoneInfo
    from datetime import datetime, timezone, timedelta

    try:
        local_tz = ZoneInfo("Asia/Almaty")
    except Exception:
        local_tz = timezone(timedelta(hours=5))

    if match_date:
        parsed_date = datetime.fromisoformat(match_date).replace(tzinfo=local_tz)
    else:
        parsed_date = None

    match_stage = None
    if stage:
        from app.models.enums import MatchStage
        try:
            match_stage = MatchStage(stage)
        except ValueError:
            pass

    match_in = MatchCreate(
        tournament_id=tournament_id, home_team_id=home_team_id, away_team_id=away_team_id,
        match_date=parsed_date, stadium=stadium, status=status, round=round,
        group_name=group_name, stage=match_stage, stage_name=stage_name
    )
    await crud_match.create(db, obj_in=match_in)

    if status == MatchStatus.FINISHED:
        from app.services.standings import StandingsService
        await StandingsService.recalculate_standings(db, tournament_id)

    return RedirectResponse(url="/admin/matches", status_code=303)


@router.get("/admin/matches/{match_id}/edit")
async def admin_match_edit_form(match_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    match = await crud_match.get(db, match_id)
    if not match: return RedirectResponse(url="/admin/matches", status_code=303)

    match.match_date = to_local_time(match.match_date)

    tournaments = (await db.execute(select(Tournament).order_by(Tournament.name))).scalars().all()
    teams = (await db.execute(select(Team).order_by(Team.name))).scalars().all()
    stadiums = await crud_stadium.get_multi(db, limit=100)

    return await render_page(request, db, "admin_match_form.html", context={
        "user": user, "match": match, "tournaments": tournaments,
        "teams": teams, "stadiums": stadiums, "statuses": MatchStatus
    })


@router.post("/admin/matches/{match_id}/edit")
async def admin_match_edit(
        match_id: int,
        request: Request,
        tournament_id: int = Form(...),
        home_team_id: int = Form(...),
        away_team_id: int = Form(...),
        match_date: str = Form(None),
        stadium: str = Form(None),
        home_score: int = Form(None),
        away_score: int = Form(None),
        home_penalty: int = Form(None),
        away_penalty: int = Form(None),
        stage: str = Form(None),
        stage_name: str = Form(None),
        status: MatchStatus = Form(...),
        round: int = Form(None),
        group_name: str = Form(None),
        tech_loss: bool = Form(False),
        db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    match = await crud_match.get(db, match_id)
    if not match:
        return RedirectResponse(url="/admin/matches", status_code=303)

    if home_team_id == away_team_id:
        raise HTTPException(status_code=400, detail="Команда не может играть сама с собой")

    old_status = match.status

    from zoneinfo import ZoneInfo
    from datetime import datetime, timezone, timedelta

    try:
        local_tz = ZoneInfo("Asia/Almaty")
    except Exception:
        local_tz = timezone(timedelta(hours=5))

    if match_date:
        parsed_date = datetime.fromisoformat(match_date).replace(tzinfo=local_tz)
    else:
        parsed_date = None

    match_stage = None
    if stage:
        from app.models.enums import MatchStage
        try:
            match_stage = MatchStage(stage)
        except ValueError:
            pass

    match_in = MatchUpdate(
        tournament_id=tournament_id, home_team_id=home_team_id, away_team_id=away_team_id,
        match_date=parsed_date, stadium=stadium, home_score=home_score, away_score=away_score,
        home_penalty=home_penalty, away_penalty=away_penalty,
        stage=match_stage, stage_name=stage_name, tech_loss=tech_loss,
        status=status, round=round, group_name=group_name
    )
    await crud_match.update(db, db_obj=match, obj_in=match_in)

    from app.services.stats_recalc import StatsRecalculator
    from app.services.disqualifications import DisqualificationService

    is_finished_now = (status == MatchStatus.FINISHED) or (home_score is not None and away_score is not None)

    if is_finished_now:
        if match.status != MatchStatus.FINISHED:
            match.status = MatchStatus.FINISHED
            db.add(match)
            await db.commit()
            await db.refresh(match)

        if old_status != MatchStatus.FINISHED:
            await DisqualificationService.process_match_finished(db, match)
            await DisqualificationService.auto_suspend_by_cards(db, match)

        await StatsRecalculator.recalculate_all(db, tournament_id)
    elif old_status == MatchStatus.FINISHED and not is_finished_now:
        await StatsRecalculator.recalculate_all(db, tournament_id)

    return RedirectResponse(url="/admin/matches", status_code=303)


@router.post("/admin/matches/{match_id}/delete")
async def admin_match_delete(match_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    match = await crud_match.get(db, match_id)
    if match: await crud_match.delete(db, db_obj=match)

    return RedirectResponse(url="/admin/matches", status_code=303)


# Игроки

@router.get("/admin/players")
async def admin_players_list(request: Request, search: str = None, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    stmt = select(Player).order_by(Player.last_name, Player.first_name)

    if search:
        stmt = stmt.where(
            or_(
                Player.last_name.ilike(f"%{search}%"),
                Player.first_name.ilike(f"%{search}%")
            )
        )
    stmt = stmt.limit(100)

    players = (await db.execute(stmt)).scalars().all()

    return await render_page(request, db, "admin_players.html", context={
        "user": user,
        "players": players,
        "current_search": search or ""
    })


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


# Команды

@router.get("/admin/teams/{team_id}/roster")
async def admin_team_roster(
        request: Request,
        team_id: int,
        tournament_id: int = None,
        db: AsyncSession = Depends(get_db),
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    team = await crud_team.get(db, team_id)
    if not team:
        raise HTTPException(404, "Команда не найдена")

    all_tournaments = (await db.execute(
        select(Tournament).order_by(Tournament.season.desc(), Tournament.name)
    )).scalars().all()

    stmt = select(TeamPlayer, Tournament).join(
        Tournament, TeamPlayer.tournament_id == Tournament.id
    ).where(TeamPlayer.team_id == team_id).options(
        selectinload(TeamPlayer.player)
    ).order_by(
        TeamPlayer.tournament_id.desc(),
        TeamPlayer.jersey_number.asc().nullslast()
    )

    if tournament_id:
        stmt = stmt.where(TeamPlayer.tournament_id == tournament_id)

    result = (await db.execute(stmt)).all()

    roster = []
    for tp, t in result:
        roster.append({
            "id": tp.id,
            "jersey_number": tp.jersey_number,
            "is_captain": tp.is_captain,
            "player": tp.player,
            "tournament": t,  # <--- передаем объект турнира сюда
            "tournament_id": tp.tournament_id,
        })

    return await render_page(request, db, "admin_roster.html", context={
        "team": team,
        "all_tournaments": all_tournaments,
        "roster": roster,
        "positions": list(PlayerPosition),
        "selected_tournament_id": tournament_id,
    })


from fastapi import UploadFile, File  # Убедись, что это импортировано


@router.post("/admin/teams/{team_id}/roster/add")
async def admin_team_roster_add(
        team_id: int,
        request: Request,
        tournament_id: int = Form(...),
        first_name: str = Form(...),
        last_name: str = Form(...),
        jersey_number: int = Form(None),
        position: str = Form(None),
        date_of_birth: str = Form(None),
        photo: UploadFile = File(None),  # <-- ДОБАВИЛИ ФОТО
        db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    dob = None
    if date_of_birth:
        try:
            dob = datetime.strptime(date_of_birth, "%Y-%m-%d").date()
        except ValueError:
            pass

    photo_url = None
    if photo and photo.filename:
        photo_url = await save_upload_file(photo, "players")

    new_player = Player(
        first_name=first_name.strip(),
        last_name=last_name.strip(),
        position=position if position else None,
        date_of_birth=dob,
        photo_url=photo_url  # <-- СОХРАНЯЕМ ССЫЛКУ
    )
    db.add(new_player)
    await db.flush()

    obj_in = TeamPlayerCreate(
        team_id=team_id,
        tournament_id=tournament_id,
        player_id=new_player.id,
        jersey_number=jersey_number,
        is_captain=False,
        is_active=True
    )
    await crud_team_player.create(db, obj_in=obj_in)

    return RedirectResponse(url=f"/admin/teams/{team_id}/roster?tournament_id={tournament_id}", status_code=303)


@router.post("/admin/teams/{team_id}/roster/edit/{roster_id}")
async def admin_team_roster_edit(
        team_id: int,
        roster_id: int,
        request: Request,
        first_name: str = Form(...),
        last_name: str = Form(...),
        jersey_number: int = Form(None),
        position: str = Form(None),
        date_of_birth: str = Form(None),
        photo: UploadFile = File(None),
        remove_photo: str = Form(None),
        db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    roster_item = await crud_team_player.get(db, roster_id)
    if roster_item:
        player = await db.get(Player, roster_item.player_id)
        if player:
            player.first_name = first_name.strip()
            player.last_name = last_name.strip()
            player.position = position if position else None

            if date_of_birth:
                try:
                    player.date_of_birth = datetime.strptime(date_of_birth, "%Y-%m-%d").date()
                except ValueError:
                    pass
            else:
                player.date_of_birth = None

            if photo and photo.filename:
                player.photo_url = await save_upload_file(photo, "players")
            elif remove_photo == "1":
                player.photo_url = None  # Удалить фото

        roster_item.jersey_number = jersey_number
        await db.commit()

    t_id = request.query_params.get("tournament_id")
    url = f"/admin/teams/{team_id}/roster" + (f"?tournament_id={t_id}" if t_id else "")
    return RedirectResponse(url=url, status_code=303)


# Административная панель

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

    await StandingsService.recalculate_standings(db, match.tournament_id)


# Матчи

@router.get("/admin/matches/{match_id}/events")
async def admin_match_events(match_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    stmt = select(Match).where(Match.id == match_id).options(
        selectinload(Match.home_team),
        selectinload(Match.away_team),
        selectinload(Match.tournament)
    )
    match = (await db.execute(stmt)).scalar_one_or_none()

    if not match:
        return RedirectResponse(url="/admin/matches", status_code=303)

    stmt_events = select(MatchEvent).where(MatchEvent.match_id == match_id).options(
        selectinload(MatchEvent.player),
        selectinload(MatchEvent.team)
    ).order_by(MatchEvent.minute)

    events = (await db.execute(stmt_events)).scalars().all()

    home_players = await crud_team_player.get_by_tournament_and_team(
        db, tournament_id=match.tournament_id, team_id=match.home_team_id
    )
    away_players = await crud_team_player.get_by_tournament_and_team(
        db, tournament_id=match.tournament_id, team_id=match.away_team_id
    )

    return await render_page(request, db, "admin_match_events.html", context={
        "user": user,
        "match": match,
        "events": events,
        "home_players": home_players,
        "away_players": away_players,
        "event_types": MatchEventType
    })


@router.post("/admin/matches/{match_id}/events/add")
async def admin_match_event_add(
        match_id: int,
        request: Request,
        team_id: int = Form(...),
        player_id: int = Form(...),
        event_type: str = Form(...),  # <-- ИЗМЕНЕНО НА STR
        minute: int = Form(...),
        assist_player_id: int = Form(None),
        comment: str = Form(None),
        db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    match = await db.get(Match, match_id)
    if not match:
        return RedirectResponse(url="/admin/matches", status_code=303)

    try:
        try:
            e_type = MatchEventType(event_type.lower())
        except ValueError:
            e_type = MatchEventType[event_type.upper()]
    except (ValueError, KeyError):
        raise HTTPException(status_code=400, detail=f"Неверный тип события: {event_type}")

    event_in = MatchEventCreate(
        match_id=match_id, team_id=team_id, player_id=player_id,
        event_type=e_type, minute=minute, comment=comment
    )
    await crud_match_event.create(db, obj_in=event_in)

    if e_type in [MatchEventType.GOAL, MatchEventType.PENALTY_GOAL] and assist_player_id:
        assist_in = MatchEventCreate(
            match_id=match_id, team_id=team_id, player_id=assist_player_id,
            event_type=MatchEventType.ASSIST, minute=minute
        )
        await crud_match_event.create(db, obj_in=assist_in)

    await recalculate_match_score(match, db)

    from app.services.stats_recalc import StatsRecalculator
    await StatsRecalculator.recalculate_all(db, match.tournament_id)

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

            from app.services.stats_recalc import StatsRecalculator
            await StatsRecalculator.recalculate_all(db, match.tournament_id)

    return RedirectResponse(url=f"/admin/matches/{match_id}/events", status_code=303)


# Новости

@router.get("/admin/news")
async def admin_news_list(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    stmt = select(News).order_by(News.sort_order.asc(), News.published_at.desc(), News.id.desc())
    news_list = (await db.execute(stmt)).scalars().all()

    return await render_page(request, db, "admin_news.html", context={
        "user": user,
        "news_list": news_list
    })


@router.post("/admin/news/reorder/save")
async def admin_news_reorder_save(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    try:
        data = await request.json()
        for item in data:
            n_id = item.get("id")
            new_order = item.get("sort_order")
            if n_id is not None and new_order is not None:
                n = await db.get(News, n_id)
                if n:
                    n.sort_order = new_order
                    db.add(n)
        await db.commit()
        return {"status": "ok", "message": "Порядок сохранен"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


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


from datetime import datetime


# Публичные страницы

@router.get("/matches/{match_id}/print")
async def match_protocol_print(match_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    stmt = select(Match).where(Match.id == match_id).options(
        selectinload(Match.home_team),
        selectinload(Match.away_team),
        selectinload(Match.tournament),
        selectinload(Match.events).selectinload(MatchEvent.player),
        selectinload(Match.events).selectinload(MatchEvent.team)
    )
    match = (await db.execute(stmt)).scalar_one_or_none()

    if not match:
        raise HTTPException(status_code=404, detail="Матч не найден")

    months_ru = {1: "января", 2: "февраля", 3: "марта", 4: "апреля", 5: "мая", 6: "июня",
                 7: "июля", 8: "августа", 9: "сентября", 10: "октября", 11: "ноября", 12: "декабря"}
    if match.match_date:
        dt = match.match_date
        date_ru = f"{dt.day} {months_ru[dt.month]} {dt.year} г. {dt.strftime('%H:%M')}"
    else:
        date_ru = "Дата не назначена"

    stmt_home = select(TeamPlayer).where(
        TeamPlayer.team_id == match.home_team_id,
        TeamPlayer.tournament_id == match.tournament_id,
        TeamPlayer.is_active == True
    ).options(selectinload(TeamPlayer.player)).order_by(TeamPlayer.jersey_number)

    stmt_away = select(TeamPlayer).where(
        TeamPlayer.team_id == match.away_team_id,
        TeamPlayer.tournament_id == match.tournament_id,
        TeamPlayer.is_active == True
    ).options(selectinload(TeamPlayer.player)).order_by(TeamPlayer.jersey_number)

    home_roster = (await db.execute(stmt_home)).scalars().all()
    away_roster = (await db.execute(stmt_away)).scalars().all()

    events_data = {}
    cards_data = []
    own_goals_data = []

    if match.events:
        for e in match.events:
            if e.player_id:
                if e.player_id not in events_data:
                    events_data[e.player_id] = {'goals': [], 'assists': []}

                if e.event_type in [MatchEventType.GOAL, MatchEventType.PENALTY_GOAL]:
                    events_data[e.player_id]['goals'].append(e.minute)
                elif e.event_type == MatchEventType.ASSIST:
                    events_data[e.player_id]['assists'].append(e.minute)

            if e.event_type == MatchEventType.OWN_GOAL and e.player:
                own_goals_data.append({
                    "minute": e.minute,
                    "player_name": f"{e.player.last_name} {e.player.first_name}",
                    "team_name": e.team.name if e.team else "Неизвестно"
                })

            if e.event_type in [MatchEventType.YELLOW_CARD, MatchEventType.RED_CARD]:
                cards_data.append({
                    "minute": e.minute,
                    "type": "Пр" if e.event_type == MatchEventType.YELLOW_CARD else "Уд",
                    "player_name": f"{e.player.last_name} {e.player.first_name}" if e.player else "Неизвестно",
                    "team_name": e.team.name if e.team else "Неизвестно"
                })

    for pid in events_data:
        events_data[pid]['goals'].sort()
        events_data[pid]['assists'].sort()
    cards_data.sort(key=lambda x: x['minute'])
    own_goals_data.sort(key=lambda x: x['minute'])

    return templates.TemplateResponse(
        request=request,
        name="protocol_print.html",
        context={
            "match": match,
            "date_ru": date_ru,
            "home_roster": home_roster,
            "away_roster": away_roster,
            "events_data": events_data,
            "cards_data": cards_data,
            "own_goals_data": own_goals_data  # <--- ДОБАВИЛИ
        }
    )


# Медиа

@router.get("/admin/media")
async def admin_media_list(
        request: Request,
        tournament_id: int = None,  # <--- Добавили фильтр
        db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    stmt = select(MediaAlbum).options(selectinload(MediaAlbum.items)).order_by(MediaAlbum.created_at.desc())

    current_tournament = None
    if tournament_id:
        stmt = stmt.where(MediaAlbum.tournament_id == tournament_id)
        current_tournament = await db.get(Tournament, tournament_id)

    albums = (await db.execute(stmt)).scalars().all()
    all_tournaments = (await db.execute(select(Tournament).order_by(Tournament.name))).scalars().all()

    return await render_page(request, db, "admin_media.html", context={
        "user": user,
        "albums": albums,
        "tournaments": all_tournaments,
        "current_tournament": current_tournament,
        "selected_tournament_id": tournament_id
    })


@router.get("/admin/media/create")
async def admin_media_create_form(
        request: Request,
        tournament_id: int = None,  # <--- Принимаем ID турнира из URL
        db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    tournaments = (await db.execute(select(Tournament).order_by(Tournament.name))).scalars().all()

    return await render_page(request, db, "admin_media_form.html", context={
        "user": user,
        "album": None,
        "tournaments": tournaments,
        "preselected_tournament_id": tournament_id  # <--- Передаем в шаблон
    })


@router.post("/admin/media/create")
async def admin_media_create(
        request: Request,
        title: str = Form(...),
        tournament_id: int = Form(None),
        db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    album = MediaAlbum(title=title, tournament_id=tournament_id)
    db.add(album)
    await db.commit()
    await db.refresh(album)

    if tournament_id:
        return RedirectResponse(url=f"/admin/media?tournament_id={tournament_id}", status_code=303)

    return RedirectResponse(url="/admin/media", status_code=303)


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


from sqlalchemy import select, func, or_, and_, case
from sqlalchemy.orm import selectinload
from datetime import datetime


# Публичные страницы

@router.get("/teams/{team_id}")
async def team_page(team_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    team = await db.get(Team, team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Команда не найдена")

    stmt_news = select(News).where(
        News.is_published == True,
        News.teams.any(id=team.id)
    ).order_by(News.published_at.desc()).limit(6)
    team_news = (await db.execute(stmt_news)).scalars().all()

    stmt = select(Match).where(
        or_(Match.home_team_id == team.id, Match.away_team_id == team.id)
    ).options(
        selectinload(Match.home_team),
        selectinload(Match.away_team),
        selectinload(Match.tournament)
    ).order_by(Match.match_date.desc())
    all_matches = (await db.execute(stmt)).scalars().all()
    all_matches = sorted(all_matches, key=lambda x: x.match_date.timestamp() if x.match_date else 0, reverse=True)

    finished_matches = [m for m in all_matches if m.status == MatchStatus.FINISHED]
    total_games = len(finished_matches)
    total_wins = 0
    total_goals = 0
    tournaments_set = set()

    for m in all_matches:
        tournaments_set.add(m.tournament_id)

    for m in finished_matches:
        if m.home_team_id == team.id:
            total_goals += (m.home_score or 0)
            if (m.home_score or 0) > (m.away_score or 0): total_wins += 1
        else:
            total_goals += (m.away_score or 0)
            if (m.away_score or 0) > (m.home_score or 0): total_wins += 1

    recent_form = []
    for m in finished_matches[:5]:
        if m.home_team_id == team.id:
            recent_form.append("В" if (m.home_score or 0) > (m.away_score or 0) else (
                "П" if (m.home_score or 0) < (m.away_score or 0) else "Н"))
        else:
            recent_form.append("В" if (m.away_score or 0) > (m.home_score or 0) else (
                "П" if (m.away_score or 0) < (m.home_score or 0) else "Н"))

    grouped_matches = {}
    for m in all_matches:
        season = m.tournament.season if m.tournament else "Без сезона"
        t_name = m.tournament.name if m.tournament else "Товарищеские матчи"

        if season not in grouped_matches:
            grouped_matches[season] = {}
        if t_name not in grouped_matches[season]:
            grouped_matches[season][t_name] = []
        grouped_matches[season][t_name].append(m)

    active_tournament = next((m.tournament for m in all_matches if m.tournament), None)
    standings = []
    if active_tournament:
        stmt_standings = select(TournamentStanding).where(
            TournamentStanding.tournament_id == active_tournament.id
        ).options(selectinload(TournamentStanding.team)).order_by(
            TournamentStanding.points.desc()
        ).limit(5)
        standings = (await db.execute(stmt_standings)).scalars().all()

    stmt_roster = select(TeamPlayer).where(
        TeamPlayer.team_id == team.id
    ).options(selectinload(TeamPlayer.player)).order_by(TeamPlayer.jersey_number)

    roster_raw = (await db.execute(stmt_roster)).scalars().all()

    roster = []
    seen_players = set()
    for tp in roster_raw:
        if tp.player_id not in seen_players:
            seen_players.add(tp.player_id)
            roster.append(tp)

    today = date.today()
    for tp in roster:
        if tp.player.date_of_birth:
            age = today.year - tp.player.date_of_birth.year - (
                        (today.month, today.day) < (tp.player.date_of_birth.month, tp.player.date_of_birth.day))
            tp.player.calculated_age = age
        else:
            tp.player.calculated_age = None

    return await render_page(request, db, "team_detail.html", context={
        "team": team,
        "stats": {
            "games": total_games,
            "wins": total_wins,
            "goals": total_goals,
            "tournaments": len(tournaments_set)
        },
        "team_news": team_news,
        "recent_form": recent_form,
        "grouped_matches": grouped_matches,
        "active_tournament": active_tournament,
        "standings": standings,
        "roster": roster  # <--- ПЕРЕДАЕМ СОСТАВ
    })


@router.get("/tournaments/{tournament_id}/teams/{team_id}/application")
async def team_application_page(
        tournament_id: int,
        team_id: int,
        request: Request,
        db: AsyncSession = Depends(get_db)
):
    tournament = await db.get(Tournament, tournament_id)
    team = await db.get(Team, team_id)
    if not tournament or not team:
        raise HTTPException(status_code=404, detail="Турнир или команда не найдены")

    stmt = select(TeamPlayer).where(
        TeamPlayer.tournament_id == tournament_id,
        TeamPlayer.team_id == team_id
    ).options(selectinload(TeamPlayer.player)).order_by(TeamPlayer.jersey_number)

    roster = (await db.execute(stmt)).scalars().all()

    active_players = [p for p in roster if p.is_active]
    released_players = [p for p in roster if not p.is_active]

    position_order = {
        PlayerPosition.GOALKEEPER: 1,  # Вратари
        PlayerPosition.DEFENDER: 2,    # Защитники
        PlayerPosition.MIDFIELDER: 3,  # Полузащитники
        PlayerPosition.FORWARD: 4,     # Нападающие
        None: 5                        # Без позиции
    }

    def sort_roster(tp):
        pos = position_order.get(tp.player.position, 5)
        num = tp.jersey_number if tp.jersey_number is not None else 999
        name = tp.player.last_name or ""
        return (pos, num, name)

    active_players.sort(key=sort_roster)
    released_players.sort(key=sort_roster)

    today = date.today()
    ages = []
    for tp in active_players:
        if tp.player.date_of_birth:
            age = today.year - tp.player.date_of_birth.year - (
                        (today.month, today.day) < (tp.player.date_of_birth.month, tp.player.date_of_birth.day))
            ages.append(age)

    avg_age = round(sum(ages) / len(ages), 2) if ages else 0

    stats_dict = {}
    for tp in roster:
        stats_dict[tp.player_id] = {
            "games": tp.stats_games or 0,
            "goals": tp.stats_goals or 0,
            "assists": tp.stats_assists or 0,
            "yellow_cards": tp.stats_yellow_cards or 0,
            "red_cards": tp.stats_red_cards or 0
        }

    return await render_page(request, db, "team_application.html", context={
        "tournament": tournament,
        "team": team,
        "active_players": active_players,
        "released_players": released_players,
        "avg_age": avg_age,
        "stats": stats_dict
    })


# Турниры

@router.post("/admin/tournaments/{tournament_id}/generate-playoff")
async def admin_generate_playoff(
    tournament_id: int,
    request: Request,
    num_teams: int = Form(...), # 16, 8, 4 или 2
    db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    tournament = await db.get(Tournament, tournament_id)
    if not tournament: return RedirectResponse(url="/admin/tournaments", status_code=303)

    stmt = select(TournamentStanding).where(
        TournamentStanding.tournament_id == tournament_id
    ).order_by(
        TournamentStanding.points.desc(),
        (TournamentStanding.goals_for - TournamentStanding.goals_against).desc(),
        TournamentStanding.goals_for.desc()
    ).limit(num_teams)

    top_teams = (await db.execute(stmt)).scalars().all()
    if len(top_teams) < num_teams:
        return RedirectResponse(url=f"/admin/tournaments/{tournament_id}/edit", status_code=303)

    if num_teams == 16: round_num = 1; stage_n = "1/8 Финала"; stage_t = MatchStage.PLAYOFF
    elif num_teams == 8: round_num = 2; stage_n = "1/4 Финала"; stage_t = MatchStage.PLAYOFF
    elif num_teams == 4: round_num = 3; stage_n = "1/2 Финала"; stage_t = MatchStage.PLAYOFF
    else: round_num = 4; stage_n = "Финал"; stage_t = MatchStage.FINAL

    for i in range(num_teams // 2):
        home_team = top_teams[i].team_id
        away_team = top_teams[num_teams - 1 - i].team_id

        existing = await db.scalar(select(Match).where(
            Match.tournament_id == tournament_id,
            Match.round == round_num,
            Match.home_team_id == home_team,
            Match.away_team_id == away_team
        ))
        if not existing:
            new_match = Match(
                tournament_id=tournament_id,
                home_team_id=home_team,
                away_team_id=away_team,
                status=MatchStatus.SCHEDULED,
                round=round_num,
                stage=stage_t,
                stage_name=stage_n
            )
            db.add(new_match)

    await db.commit()
    return RedirectResponse(url=f"/admin/tournaments/{tournament_id}/edit", status_code=303)


@router.get("/admin/tournaments/{tournament_id}/teams")
async def admin_tournament_teams(tournament_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    tournament = await db.get(Tournament, tournament_id)
    if not tournament: return RedirectResponse(url="/admin/tournaments", status_code=303)

    all_teams = (await db.execute(select(Team).order_by(Team.name))).scalars().all()

    stmt = select(TournamentTeam).where(TournamentTeam.tournament_id == tournament_id).options(
        selectinload(TournamentTeam.team))
    tournament_teams = (await db.execute(stmt)).scalars().all()

    added_team_ids = [tt.team_id for tt in tournament_teams]

    grouped_teams = {}
    for tt in tournament_teams:
        group_name = tt.group_name if tt.group_name else "Общая таблица"
        if group_name not in grouped_teams:
            grouped_teams[group_name] = []
        grouped_teams[group_name].append(tt)

    grouped_teams = dict(sorted(grouped_teams.items(), key=lambda x: (x[0] == "Общая таблица", x[0])))

    return await render_page(request, db, "admin_tournament_teams.html", context={
        "user": user,
        "tournament": tournament,
        "all_teams": all_teams,
        "grouped_teams": grouped_teams,
        "added_team_ids": added_team_ids
    })


@router.post("/admin/tournaments/{tournament_id}/teams/{team_id}/remove")
async def admin_tournament_remove_team(tournament_id: int, team_id: int, request: Request,
                                       db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    stmt = select(TournamentTeam).where(TournamentTeam.tournament_id == tournament_id,
                                        TournamentTeam.team_id == team_id)
    tt = (await db.execute(stmt)).scalar_one_or_none()
    if tt:
        await db.delete(tt)

    stmt_s = select(TournamentStanding).where(TournamentStanding.tournament_id == tournament_id,
                                              TournamentStanding.team_id == team_id)
    ts = (await db.execute(stmt_s)).scalar_one_or_none()
    if ts:
        await db.delete(ts)

    await db.commit()
    return RedirectResponse(url=f"/admin/tournaments/{tournament_id}/teams", status_code=303)


@router.post("/admin/tournaments/{tournament_id}/recalculate")
async def admin_tournament_recalculate(tournament_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    from app.services.standings import StandingsService
    await StandingsService.recalculate_standings(db, tournament_id)

    return RedirectResponse(url=f"/admin/tournaments/{tournament_id}/edit", status_code=303)


# Матчи

@router.post("/admin/matches/{match_id}/events/{event_id}/edit")
async def admin_match_event_edit(
        match_id: int, event_id: int, request: Request,
        team_id: int = Form(...), player_id: int = Form(...),
        event_type: MatchEventType = Form(...), minute: int = Form(...),
        comment: str = Form(None), db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    event = await db.get(MatchEvent, event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Событие не найдено")

    event.team_id = team_id
    event.player_id = player_id
    event.event_type = event_type
    event.minute = minute
    event.comment = comment
    db.add(event)
    await db.commit()

    match = await db.get(Match, match_id)
    await recalculate_match_score(match, db)

    from app.services.stats_recalc import StatsRecalculator
    await StatsRecalculator.recalculate_all(db, match.tournament_id)

    return RedirectResponse(url=f"/admin/matches/{match_id}/events", status_code=303)


# Административная панель

@router.post("/admin/roster/{roster_id}/edit")
async def admin_roster_edit(
        roster_id: int, request: Request,
        jersey_number: int = Form(None),
        is_captain: bool = Form(False),
        is_active: bool = Form(True),
        db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    roster_item = await crud_team_player.get(db, roster_id)
    if not roster_item:
        raise HTTPException(status_code=404, detail="Игрок не найден в заявке")

    roster_item.jersey_number = jersey_number
    roster_item.is_captain = is_captain
    roster_item.is_active = is_active
    db.add(roster_item)
    await db.commit()

    return RedirectResponse(url=f"/admin/teams/{roster_item.team_id}/roster?tournament_id={roster_item.tournament_id}", status_code=303)


# Турниры

@router.post("/admin/tournaments/{tournament_id}/teams/add")
async def admin_tournament_add_team(
        tournament_id: int,
        request: Request,
        team_id: int = Form(...),
        group_name: str = Form(None),  # <--- ДОБАВИЛИ ГРУППУ
        db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    existing = await db.scalar(select(TournamentTeam).where(
        TournamentTeam.tournament_id == tournament_id, TournamentTeam.team_id == team_id
    ))
    if not existing:
        db.add(TournamentTeam(tournament_id=tournament_id, team_id=team_id, group_name=group_name))

        db.add(TournamentStanding(
            tournament_id=tournament_id, team_id=team_id, group_name=group_name,
            games_played=0, wins=0, draws=0, losses=0, goals_for=0, goals_against=0, points=0
        ))
        await db.commit()

    return RedirectResponse(url=f"/admin/tournaments/{tournament_id}/teams", status_code=303)


@router.post("/admin/tournaments/{tournament_id}/teams/{team_id}/change-group")
async def admin_tournament_change_team_group(
        tournament_id: int, team_id: int, request: Request,
        new_group_name: str = Form(None),
        db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    tt = await db.scalar(select(TournamentTeam).where(
        TournamentTeam.tournament_id == tournament_id, TournamentTeam.team_id == team_id
    ))
    if tt:
        tt.group_name = new_group_name
        db.add(tt)

    ts = await db.scalar(select(TournamentStanding).where(
        TournamentStanding.tournament_id == tournament_id, TournamentStanding.team_id == team_id
    ))
    if ts:
        ts.group_name = new_group_name
        db.add(ts)

    await db.commit()
    return RedirectResponse(url=f"/admin/tournaments/{tournament_id}/teams", status_code=303)


# Дисквалификации

@router.get("/admin/disqualifications")
async def admin_disqualifications_list(
        request: Request,
        tournament_id: int = None,
        search: str = None,
        db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    stmt = select(Disqualification).options(
        selectinload(Disqualification.player),
        selectinload(Disqualification.team),
        selectinload(Disqualification.tournament)
    ).order_by(Disqualification.created_at.desc())

    if tournament_id:
        stmt = stmt.where(Disqualification.tournament_id == tournament_id)

    if search:
        stmt = stmt.join(Player).where(
            or_(
                Player.last_name.ilike(f"%{search}%"),
                Player.first_name.ilike(f"%{search}%")
            )
        )

    disqualifications = (await db.execute(stmt)).scalars().all()
    tournaments = (await db.execute(select(Tournament).order_by(Tournament.name))).scalars().all()

    active_disq = [d for d in disqualifications if d.matches_remaining and d.matches_remaining > 0]
    completed_disq = [d for d in disqualifications if not d.matches_remaining or d.matches_remaining == 0]

    return await render_page(request, db, "admin_disqualifications.html", context={
        "user": user,
        "active_disqualifications": active_disq,
        "completed_disqualifications": completed_disq,
        "tournaments": tournaments,
        "selected_tournament_id": tournament_id,
        "current_search": search or ""  # <--- Передаем поиск в шаблон
    })


@router.get("/admin/disqualifications/create")
async def admin_disqualification_create_form(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    tournaments = (await db.execute(select(Tournament).order_by(Tournament.name))).scalars().all()
    teams = (await db.execute(select(Team).order_by(Team.name))).scalars().all()

    players = (await db.execute(select(Player).order_by(Player.last_name))).scalars().all()

    return await render_page(request, db, "admin_disqualification_form.html", context={
        "user": user,
        "tournaments": tournaments,
        "teams": teams,
        "players": players
    })


@router.post("/admin/disqualifications/create")
async def admin_disqualification_create(
        request: Request,
        tournament_id: int = Form(...),
        team_id: int = Form(...),
        player_id: int = Form(...),
        matches_total: int = Form(...),
        reason: str = Form(...),
        db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    from datetime import date
    new_disq = Disqualification(
        tournament_id=tournament_id,
        team_id=team_id,
        player_id=player_id,
        matches_total=matches_total,
        matches_remaining=matches_total,
        reason=reason,
        decision_date=date.today()
    )
    db.add(new_disq)
    await db.commit()

    return RedirectResponse(url="/admin/disqualifications", status_code=303)


@router.post("/admin/disqualifications/{disq_id}/delete")
async def admin_disqualification_delete(disq_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    disq = await db.get(Disqualification, disq_id)
    if disq:
        await db.delete(disq)
        await db.commit()

    return RedirectResponse(url="/admin/disqualifications", status_code=303)


# Матчи

@router.post("/admin/matches/{match_id}/finish")
async def admin_match_finish(
    match_id: int, request: Request,
    home_penalty: int = Form(None), away_penalty: int = Form(None),
    tech_loss_team_id: int = Form(None),
    db: AsyncSession = Depends(get_db),
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse):
        return user
    match = await db.scalar(select(Match).where(Match.id == match_id).with_for_update())
    if match is None:
        raise HTTPException(404, "Матч не найден")
    if match.status == MatchStatus.FINISHED:
        return RedirectResponse(url="/admin/matches", status_code=303)
    if (home_penalty is None) != (away_penalty is None):
        raise HTTPException(422, "Укажите оба результата серии пенальти")
    if any(v is not None and v < 0 for v in (home_penalty, away_penalty)):
        raise HTTPException(422, "Счёт серии пенальти не может быть отрицательным")
    if tech_loss_team_id and tech_loss_team_id not in (match.home_team_id, match.away_team_id):
        raise HTTPException(422, "Команда технического поражения не участвует в матче")
    if tech_loss_team_id:
        match.tech_loss = True
        match.home_score = 0 if tech_loss_team_id == match.home_team_id else 3
        match.away_score = 3 if tech_loss_team_id == match.home_team_id else 0
    elif match.home_score is None or match.away_score is None:
        raise HTTPException(422, "Перед завершением матча укажите счёт")
    match.home_penalty = home_penalty
    match.away_penalty = away_penalty
    match.status = MatchStatus.FINISHED
    await db.flush()
    await StatsRecalculator.recalculate_all(db, match.tournament_id)
    await db.commit()
    return RedirectResponse(url="/admin/matches", status_code=303)



# Публичные страницы

@router.get("/participants/hall-of-fame")
async def participants_hall_page(
        request: Request,
        db: AsyncSession = Depends(get_db)
):
    from sqlalchemy import select, func, desc
    from app.models.team import Team
    from app.models.player import Player
    from app.models.team_player import TeamPlayer
    from app.models.match import Match
    from app.models.enums import MatchStatus

    stmt_matches = select(Match).where(Match.status == MatchStatus.FINISHED)
    all_matches = (await db.execute(stmt_matches)).scalars().all()

    team_stats = {}
    for m in all_matches:
        if m.home_team_id not in team_stats: team_stats[m.home_team_id] = {'games': 0, 'wins': 0, 'goals': 0, 'clean_sheets': 0}
        if m.away_team_id not in team_stats: team_stats[m.away_team_id] = {'games': 0, 'wins': 0, 'goals': 0, 'clean_sheets': 0}

        h = team_stats[m.home_team_id]
        a = team_stats[m.away_team_id]

        h['games'] += 1
        a['games'] += 1
        h['goals'] += (m.home_score or 0)
        a['goals'] += (m.away_score or 0)

        if m.away_score == 0: h['clean_sheets'] += 1
        if m.home_score == 0: a['clean_sheets'] += 1

        if (m.home_score or 0) > (m.away_score or 0): h['wins'] += 1
        elif (m.away_score or 0) > (m.home_score or 0): a['wins'] += 1

    all_teams = (await db.scalars(select(Team))).all()
    teams_dict = {t.id: t for t in all_teams}

    teams_list = []
    for tid, stats in team_stats.items():
        team = teams_dict.get(tid)
        if team:
            teams_list.append({
                "team": team,
                "games": stats['games'],
                "wins": stats['wins'],
                "goals": stats['goals'],
                "clean_sheets": stats['clean_sheets']
            })

    top_teams_games = sorted(teams_list, key=lambda x: x['games'], reverse=True)[:5]
    top_teams_wins = sorted(teams_list, key=lambda x: x['wins'], reverse=True)[:5]
    top_teams_cs = sorted(teams_list, key=lambda x: x['clean_sheets'], reverse=True)[:5]
    top_teams_goals = sorted(teams_list, key=lambda x: x['goals'], reverse=True)[:5]

    stmt_tp = select(TeamPlayer).options(
        selectinload(TeamPlayer.player),
        selectinload(TeamPlayer.team)
    )
    all_tp = (await db.execute(stmt_tp)).scalars().all()

    player_stats = {}
    for tp in all_tp:
        if not tp.player: continue
        pid = tp.player.id
        if pid not in player_stats:
            player_stats[pid] = {
                'player': tp.player,
                'team': tp.team,
                'games': 0, 'goals': 0, 'assists': 0
            }

        player_stats[pid]['games'] += (tp.stats_games or 0)
        player_stats[pid]['goals'] += (tp.stats_goals or 0)
        player_stats[pid]['assists'] += (tp.stats_assists or 0)
        if tp.team: player_stats[pid]['team'] = tp.team # Обновляем на актуальную

    players_list = list(player_stats.values())

    top_players_games = sorted(players_list, key=lambda x: x['games'], reverse=True)[:5]
    top_players_ga = sorted(players_list, key=lambda x: (x['goals'] + x['assists']), reverse=True)[:5]
    top_players_goals = sorted(players_list, key=lambda x: x['goals'], reverse=True)[:5]
    top_players_assists = sorted(players_list, key=lambda x: x['assists'], reverse=True)[:5]

    from app.models.tournament import Tournament
    first_tournament = await db.scalar(select(Tournament).limit(1))
    hof_style = getattr(first_tournament, 'stats_style', 'default') or 'default'

    return await render_page(request, db, "hall_of_fame.html", context={
        "teams_games": top_teams_games,
        "teams_wins": top_teams_wins,
        "teams_cs": top_teams_cs,
        "teams_goals": top_teams_goals,
        "players_games": top_players_games,
        "players_ga": top_players_ga,
        "players_goals": top_players_goals,
        "players_assists": top_players_assists,
        "hof_style": hof_style
    })


# Турниры

@router.post("/admin/tournaments/{tournament_id}/generate-card")
async def admin_generate_champion_card(
    tournament_id: int,
    request: Request,
    prompt_index: int = Form(0),
    db: AsyncSession = Depends(get_db),
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse):
        return user

    try:
        image_url = await generate_champion_card(db, tournament_id, prompt_index)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка генерации: {e}")

    return RedirectResponse(
        url=f"/admin/tournaments/{tournament_id}/edit?card_url={image_url}",
        status_code=303,
    )


@router.get("/admin/tournaments/reorder")
async def admin_tournaments_reorder(
        request: Request,
        season: str = None,
        db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    seasons_result = await db.execute(select(Tournament.season).distinct().order_by(Tournament.season.desc()))
    available_seasons = [s[0] for s in seasons_result.all() if s[0]]

    if not season and available_seasons:
        season = available_seasons[0]

    stmt = select(Tournament).where(Tournament.season == season).order_by(Tournament.sort_order.asc(),
                                                                          Tournament.name.asc())
    tournaments = (await db.execute(stmt)).scalars().all()

    return await render_page(request, db, "admin_tournament_reorder.html", context={
        "user": user,
        "tournaments": tournaments,
        "available_seasons": available_seasons,
        "current_season": season
    })


@router.post("/admin/tournaments/reorder/save")
async def admin_tournaments_reorder_save(
        request: Request,
        db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    try:
        data = await request.json()

        for item in data:
            t_id = item.get("id")
            new_order = item.get("sort_order")
            if t_id is not None and new_order is not None:
                t = await db.get(Tournament, t_id)
                if t:
                    t.sort_order = new_order
                    db.add(t)

        await db.commit()
        return {"status": "ok", "message": "Порядок сохранен"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.get("/admin/tournaments/{tournament_id}/regulations")
async def admin_tournament_regulations(tournament_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    tournament = await db.get(Tournament, tournament_id)
    if not tournament:
        return RedirectResponse(url="/admin/tournaments", status_code=303)

    return await render_page(request, db, "admin_tournament_regulations.html", context={
        "user": user,
        "tournament": tournament
    })

@router.post("/admin/tournaments/{tournament_id}/regulations")
async def admin_tournament_regulations_save(
        tournament_id: int, request: Request,
        allow_draws: bool = Form(False),
        points_win: int = Form(3), points_draw: int = Form(1), points_loss: int = Form(0),
        periods_count: int = Form(2), period_duration: int = Form(45),
        auto_disqualifications: bool = Form(False),
        yellow_cards_limit: int = Form(4), yellow_suspension: int = Form(1), red_suspension: int = Form(1),
        tech_loss_goals: bool = Form(False),
        max_starting_players: int = Form(11), max_bench_players: int = Form(5),
        tie_breakers: str = Form("[]"),
        h2h_always: bool = Form(False),  # <--- H2H: ДОБАВЛЕН ПРИЕМ ИЗ ФОРМЫ
        db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    tournament = await db.get(Tournament, tournament_id)
    if not tournament:
        return RedirectResponse(url="/admin/tournaments", status_code=303)

    import json
    try:
        tb_list = json.loads(tie_breakers)
    except:
        tb_list = []

    tournament.allow_draws = allow_draws
    tournament.points_win = points_win
    tournament.points_draw = points_draw
    tournament.points_loss = points_loss
    tournament.periods_count = periods_count
    tournament.period_duration = period_duration
    tournament.auto_disqualifications = auto_disqualifications
    tournament.yellow_cards_limit = yellow_cards_limit
    tournament.yellow_suspension = yellow_suspension
    tournament.red_suspension = red_suspension
    tournament.tech_loss_goals = tech_loss_goals
    tournament.max_starting_players = max_starting_players
    tournament.max_bench_players = max_bench_players
    tournament.tie_breakers = tb_list
    tournament.h2h_always = h2h_always  # <--- H2H: СОХРАНЕНИЕ В БАЗУ

    db.add(tournament)
    await db.commit()

    return RedirectResponse(url=f"/admin/tournaments/{tournament_id}/edit?msg=regulations_saved", status_code=303)


@router.get("/admin/tournaments/{tournament_id}/structure")
async def admin_tournament_structure(
        tournament_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    tournament = await db.get(Tournament, tournament_id)
    if not tournament:
        return RedirectResponse(url="/admin/tournaments", status_code=303)

    stmt_tt = select(TournamentTeam).where(TournamentTeam.tournament_id == tournament_id).options(
        selectinload(TournamentTeam.team)
    )
    tournament_teams = (await db.execute(stmt_tt)).scalars().all()

    saved_groups = tournament.custom_groups or []

    groups = {g: [] for g in saved_groups}
    unassigned_teams = []

    for tt in tournament_teams:
        if not tt.group_name:
            unassigned_teams.append(tt)
        else:
            if tt.group_name not in groups:
                groups[tt.group_name] = []  # Если группы нет в сохраненных, создаем
            groups[tt.group_name].append(tt)

    stmt_brackets = select(Match.stage_name).where(
        Match.tournament_id == tournament_id,
        Match.stage_name.isnot(None),
        Match.stage_name != ""
    ).distinct()
    brackets = [b[0] for b in (await db.execute(stmt_brackets)).all()]

    return await render_page(request, db, "admin_tournament_structure.html", context={
        "user": user,
        "tournament": tournament,
        "unassigned_teams": unassigned_teams,
        "groups": groups,
        "brackets": brackets
    })


@router.post("/admin/tournaments/{tournament_id}/structure/add-group")
async def admin_structure_add_group(
        tournament_id: int, request: Request, group_name: str = Form(...), db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    tournament = await db.get(Tournament, tournament_id)
    if tournament:
        groups = tournament.custom_groups or []
        if group_name not in groups:
            groups.append(group_name)
            tournament.custom_groups = groups
            db.add(tournament)
            await db.commit()

    return RedirectResponse(url=f"/admin/tournaments/{tournament_id}/structure", status_code=303)


@router.post("/admin/tournaments/{tournament_id}/structure/save")
async def admin_structure_save(
        tournament_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    form_data = await request.form()

    for key, value in form_data.items():
        if key.startswith("team_group_"):
            team_id = int(key.replace("team_group_", ""))

            group_name = value if value and value != "unassigned" else None

            stmt_tt = select(TournamentTeam).where(
                TournamentTeam.tournament_id == tournament_id,
                TournamentTeam.team_id == team_id
            )
            tt = (await db.execute(stmt_tt)).scalar_one_or_none()
            if tt:
                tt.group_name = group_name
                db.add(tt)

            stmt_ts = select(TournamentStanding).where(
                TournamentStanding.tournament_id == tournament_id,
                TournamentStanding.team_id == team_id
            )
            ts = (await db.execute(stmt_ts)).scalar_one_or_none()
            if ts:
                ts.group_name = group_name
                db.add(ts)

    await db.commit()
    return RedirectResponse(url=f"/admin/tournaments/{tournament_id}/structure", status_code=303)


@router.post("/admin/tournaments/{tournament_id}/structure/create-bracket")
async def admin_create_bracket(
        tournament_id: int, request: Request, bracket_name: str = Form(...), db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    new_match = Match(
        tournament_id=tournament_id,
        home_team_id=1,
        away_team_id=1,
        status=MatchStatus.SCHEDULED,
        stage=MatchStage.PLAYOFF,
        stage_name=bracket_name,
        round=1
    )
    db.add(new_match)
    await db.commit()

    return RedirectResponse(url=f"/admin/tournaments/{tournament_id}/structure", status_code=303)


# Публичные страницы

@router.get("/tournaments/{tournament_id}/photos")
async def tournament_photos_page(tournament_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    tournament = await db.get(Tournament, tournament_id)
    if not tournament:
        raise HTTPException(status_code=404, detail="Турнир не найден")

    stmt = select(MediaAlbum).where(MediaAlbum.tournament_id == tournament_id).options(
        selectinload(MediaAlbum.items)).order_by(MediaAlbum.created_at.desc())
    albums = (await db.execute(stmt)).scalars().all()

    return await render_page(request, db, "tournament_media.html", context={
        "tournament": tournament,
        "albums": albums,
        "media_type": "photos"
    })


@router.get("/tournaments/{tournament_id}/videos")
async def tournament_videos_page(tournament_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    tournament = await db.get(Tournament, tournament_id)
    if not tournament:
        raise HTTPException(status_code=404, detail="Турнир не найден")

    stmt = select(MediaAlbum).where(MediaAlbum.tournament_id == tournament_id).options(
        selectinload(MediaAlbum.items)).order_by(MediaAlbum.created_at.desc())
    albums = (await db.execute(stmt)).scalars().all()

    return await render_page(request, db, "tournament_media.html", context={
        "tournament": tournament,
        "albums": albums,
        "media_type": "videos"
    })

def build_matches_dict(matches):
    """Хелпер для создания словаря с данными для всплывающих окон"""
    from app.models.enums import MatchEventType
    matches_dict = {}
    for m in matches:
        events_list = []
        if m.events:
            sorted_events = sorted(m.events, key=lambda x: x.minute)
            for e in sorted_events:
                event_str = str(e.event_type).upper()
                if "YELLOW" in event_str: etype = "YELLOW"
                elif "RED" in event_str: etype = "RED"
                elif "OWN" in event_str: etype = "OWN_GOAL"
                elif "PENALTY" in event_str: etype = "PENALTY"
                else: etype = "GOAL"

                events_list.append({
                    "minute": e.minute, "team_id": e.team_id,
                    "player": f"{e.player.last_name} {e.player.first_name[0]}." if e.player else "Игрок",
                    "type": etype
                })

        matches_dict[m.id] = {
            "id": m.id, "home_name": m.home_team.name, "away_name": m.away_team.name,
            "home_team_id": m.home_team_id, "away_team_id": m.away_team_id,
            "home_score": m.home_score if m.home_score is not None else 0,
            "away_score": m.away_score if m.away_score is not None else 0,
            "home_logo": m.home_team.logo_url or f"https://ui-avatars.com/api/?name={m.home_team.name}&background=0f2537&color=fff&size=64&rounded=true",
            "away_logo": m.away_team.logo_url or f"https://ui-avatars.com/api/?name={m.away_team.name}&background=0f2537&color=fff&size=64&rounded=true",
            "home_penalty": m.home_penalty, "away_penalty": m.away_penalty,
            "stage_name": m.stage_name, "tech_loss": m.tech_loss,
            "date": to_local_time(m.match_date).strftime('%d.%m.%Y %H:%M') if m.match_date else 'TBA',
            "stadium": m.stadium or 'Стадион TBA', "events": events_list,
        }
    return matches_dict


@router.get("/tournaments/{tournament_id}/stats")
async def tournament_full_stats_page(
        tournament_id: int,
        request: Request,
        db: AsyncSession = Depends(get_db)
):
    from sqlalchemy import func, or_
    from sqlalchemy.orm import selectinload
    from app.models.tournament import Tournament
    from app.models.team_player import TeamPlayer
    from app.models.player import Player
    from app.models.tournament_standing import TournamentStanding

    tournament = await db.get(Tournament, tournament_id)
    if not tournament:
        raise HTTPException(status_code=404, detail="Турнир не найден")

    stmt_goals = select(TeamPlayer).join(Player).where(
        TeamPlayer.tournament_id == tournament_id,
        func.coalesce(TeamPlayer.stats_goals, 0) > 0
    ).options(
        selectinload(TeamPlayer.player),
        selectinload(TeamPlayer.team)
    ).order_by(
        func.coalesce(TeamPlayer.stats_goals, 0).desc(),
        func.coalesce(TeamPlayer.stats_games, 0).asc()
    )
    stats_goals = (await db.execute(stmt_goals)).scalars().all()

    stmt_cards = select(TeamPlayer).join(Player).where(
        TeamPlayer.tournament_id == tournament_id,
        or_(
            func.coalesce(TeamPlayer.stats_yellow_cards, 0) > 0,
            func.coalesce(TeamPlayer.stats_red_cards, 0) > 0
        )
    ).options(
        selectinload(TeamPlayer.player),
        selectinload(TeamPlayer.team)
    ).order_by(
        func.coalesce(TeamPlayer.stats_yellow_cards, 0).desc(),
        func.coalesce(TeamPlayer.stats_red_cards, 0).desc()
    )
    stats_cards = (await db.execute(stmt_cards)).scalars().all()

    stmt_standings = select(TournamentStanding).where(
        TournamentStanding.tournament_id == tournament_id
    ).options(selectinload(TournamentStanding.team)).order_by(
        TournamentStanding.points.desc(),
        (TournamentStanding.goals_for - TournamentStanding.goals_against).desc()
    )
    standings = (await db.execute(stmt_standings)).scalars().all()

    return await render_page(request, db, "tournament_stats_full.html", context={
        "tournament": tournament,
        "tournament_stats_goals": stats_goals,
        "tournament_stats_cards": stats_cards,
        "standings": standings
    })


# Матчи

@router.post("/admin/matches/{match_id}/quick-update")
async def admin_matches_quick_update(
    match_id: int,
    request: Request,
    match_date: str = Form(None),
    home_score: str = Form(None),
    away_score: str = Form(None),
    home_penalty: str = Form(None),
    away_penalty: str = Form(None),
    round: str = Form(None),
    db: AsyncSession = Depends(get_db),
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse):
        return user
    match = await db.scalar(select(Match).where(Match.id == match_id).with_for_update())
    if match is None:
        raise HTTPException(404, "Матч не найден")
    previous = (match.home_score, match.away_score, match.status)
    if match_date is not None:
        if match_date.strip():
            try:
                from zoneinfo import ZoneInfo
                local_tz = ZoneInfo("Asia/Almaty")
                local_dt = datetime.strptime(match_date, "%Y-%m-%dT%H:%M")
                match.match_date = local_dt.replace(tzinfo=local_tz).astimezone(timezone.utc)
            except ValueError as exc:
                raise HTTPException(422, "Неверная дата матча") from exc
        else:
            match.match_date = None
    def parse_nonnegative(raw, label):
        if raw is None or not raw.strip():
            return None
        try:
            value = int(raw)
        except ValueError as exc:
            raise HTTPException(422, f"Некорректное поле: {label}") from exc
        if value < 0:
            raise HTTPException(422, f"{label} не может быть отрицательным")
        return value
    match.home_score = parse_nonnegative(home_score, "Голы хозяев")
    match.away_score = parse_nonnegative(away_score, "Голы гостей")
    match.home_penalty = parse_nonnegative(home_penalty, "Пенальти хозяев")
    match.away_penalty = parse_nonnegative(away_penalty, "Пенальти гостей")
    match.round = parse_nonnegative(round, "Тур")
    if (match.home_score is None) != (match.away_score is None):
        raise HTTPException(422, "Нужно указать оба счёта матча")
    if (match.home_penalty is None) != (match.away_penalty is None):
        raise HTTPException(422, "Нужно указать оба результата серии пенальти")
    if match.home_score is not None:
        match.status = MatchStatus.FINISHED
    elif match.status == MatchStatus.FINISHED:
        match.status = MatchStatus.SCHEDULED
    await db.flush()
    if previous != (match.home_score, match.away_score, match.status):
        await StatsRecalculator.recalculate_all(db, match.tournament_id)
    await db.commit()
    referer = request.headers.get("referer", "/admin/matches")
    # Do not redirect to arbitrary external domains.
    if not referer.startswith("/") or referer.startswith("//"):
        referer = "/admin/matches"
    return RedirectResponse(url=referer, status_code=303)



# Турниры

@router.post("/admin/tournaments/{tid}/generate-schedule")
async def admin_generate_schedule(
    request: Request,
    tid: int,
    circles: int = Form(1),
    db: AsyncSession = Depends(get_db),
):
    admin = await get_admin_from_cookie(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    if not 1 <= circles <= 4:
        raise HTTPException(422, "Количество кругов должно быть от 1 до 4")
    tournament = await crud_tournament.get(db, tid)
    if tournament is None:
        raise HTTPException(404, "Турнир не найден")
    tteams = (await db.scalars(
        select(TournamentTeam).where(TournamentTeam.tournament_id == tid)
    )).all()
    if len(tteams) < 2:
        return RedirectResponse(url=f"/admin/tournaments/{tid}?msg=not_enough_teams", status_code=303)
    groups = {}
    for tt in tteams:
        groups.setdefault(tt.group_name or "", []).append(tt.team_id)
    from app.services.schedule_generator import generate_for_tournament
    # Validate and generate before deleting any existing schedule.
    has_groups = any(bool(tt.group_name) for tt in tteams)
    if has_groups:
        pairs = generate_for_tournament(
            all_team_ids=[tt.team_id for tt in tteams], circles=circles,
            groups_info={name or "Без группы": ids for name, ids in groups.items()},
        )
    else:
        pairs = generate_for_tournament(
            all_team_ids=[tt.team_id for tt in tteams], circles=circles
        )
    if not pairs:
        raise HTTPException(422, "Не удалось сформировать календарь")
    # Prevent deleting scheduled matches with attached events.
    scheduled_ids = select(Match.id).where(
        Match.tournament_id == tid, Match.status == MatchStatus.SCHEDULED
    )
    events_count = await db.scalar(
        select(func.count(MatchEvent.id)).where(MatchEvent.match_id.in_(scheduled_ids))
    )
    if events_count:
        raise HTTPException(409, "У запланированных матчей есть события. Удаление запрещено")
    await db.execute(delete(Match).where(
        Match.tournament_id == tid, Match.status == MatchStatus.SCHEDULED
    ))
    for pair in pairs:
        db.add(Match(
            tournament_id=tid,
            home_team_id=pair.home_team_id,
            away_team_id=pair.away_team_id,
            round=pair.round,
            group_name=pair.group_name,
            stage=MatchStage.GROUP if pair.group_name else None,
            status=MatchStatus.SCHEDULED,
        ))
    await db.commit()
    return RedirectResponse(
        url=f"/admin/tournaments/{tid}?msg=schedule_generated&count={len(pairs)}",
        status_code=303,
    )



async def _all_tournaments_for_select(db: AsyncSession):
    return (await db.execute(
        select(Tournament).order_by(Tournament.season.desc(), Tournament.name)
    )).scalars().all()


async def _load_schedule_teams(db: AsyncSession, tid: int):
    tteams = (await db.execute(
        select(TournamentTeam).where(TournamentTeam.tournament_id == tid)
    )).scalars().all()

    if not tteams:
        return [], {}

    team_ids = [tt.team_id for tt in tteams]
    teams = (await db.execute(
        select(Team).where(Team.id.in_(team_ids)).order_by(Team.name)
    )).scalars().all()

    teams_by_id = {t.id: t for t in teams}
    teams_list = [{"id": t.id, "name": t.name} for t in teams]

    groups_info: dict[str, list[dict]] = {}
    for tt in tteams:
        t = teams_by_id.get(tt.team_id)
        if not t:
            continue
        g = tt.group_name or ""
        groups_info.setdefault(g, []).append({"id": t.id, "name": t.name})

    return teams_list, groups_info


# Административная панель

@router.get("/admin/schedule")
async def admin_schedule_global(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    admin = await get_admin_from_cookie(request, db)
    if isinstance(admin, RedirectResponse):
        return admin

    return await render_page(request, db, "admin_schedule.html", context={
        "tournament": None,
        "all_tournaments": await _all_tournaments_for_select(db),
        "teams_list": [],
        "groups_info": {},
        "preview_matches": [],
        "circles": 1,
    })


# Турниры

@router.get("/admin/tournaments/{tid}/schedule")
async def admin_schedule_get(
    request: Request,
    tid: int,
    circles: int = 1,
    db: AsyncSession = Depends(get_db),
):
    admin = await get_admin_from_cookie(request, db)
    if isinstance(admin, RedirectResponse):
        return admin

    tournament = await crud_tournament.get(db, tid)
    if not tournament:
        raise HTTPException(404, "Турнир не найден")

    teams_list, groups_info = await _load_schedule_teams(db, tid)

    return await render_page(request, db, "admin_schedule.html", context={
        "tournament": tournament,
        "all_tournaments": await _all_tournaments_for_select(db),
        "teams_list": teams_list,
        "groups_info": groups_info,
        "preview_matches": [],
        "circles": circles,
    })

@router.post("/admin/tournaments/{tid}/schedule/approve")
async def admin_schedule_approve(
    request: Request,
    tid: int,
    db: AsyncSession = Depends(get_db),
):
    token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(403, "Not authenticated")
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
        user = await crud_user.get_by_email(db, email=payload.get("sub"))
        if not user or user.role != UserRole.ADMIN:
            raise HTTPException(403, "Not admin")
    except JWTError:
        raise HTTPException(403, "Invalid token")

    tournament = await crud_tournament.get(db, tid)
    if not tournament:
        raise HTTPException(404, "Турнир не найден")

    body = await request.json()
    matches = body.get("matches") or []
    if not isinstance(matches, list):
        raise HTTPException(400, "matches must be a list")

    await db.execute(
        delete(Match).where(
            Match.tournament_id == tid,
            Match.status == MatchStatus.SCHEDULED,
        )
    )

    count = 0
    for m in matches:
        try:
            home_id = int(m["home_team_id"])
            away_id = int(m["away_team_id"])
            rnd = int(m.get("round", 1))
        except (KeyError, ValueError, TypeError):
            continue
        if home_id == away_id:
            continue

        group_name = (m.get("group_name") or "").strip() or None

        db.add(Match(
            tournament_id=tid,
            home_team_id=home_id,
            away_team_id=away_id,
            round=rnd,
            group_name=group_name,
            stage=MatchStage.GROUP if group_name else None,
            status=MatchStatus.SCHEDULED,
        ))
        count += 1

    await db.commit()
    return {"ok": True, "count": count}


@router.get("/admin/tournaments/{tournament_id}/schedule")
async def admin_schedule_page(
    tournament_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse):
        return user

    tournament = await db.get(Tournament, tournament_id)
    if not tournament:
        raise HTTPException(404, "Турнир не найден")

    stmt_tt = select(TournamentTeam).where(
        TournamentTeam.tournament_id == tournament_id
    ).options(selectinload(TournamentTeam.team))
    tt_list = (await db.execute(stmt_tt)).scalars().all()

    all_teams = [tt.team for tt in tt_list if tt.team]

    groups_info: dict[str, list] = {}
    has_groups = any(getattr(tt, "group_name", None) for tt in tt_list)
    if has_groups:
        for tt in tt_list:
            if not tt.team:
                continue
            gname = (tt.group_name or "").strip() or "—"
            groups_info.setdefault(gname, []).append(tt.team)

    all_tournaments = (await db.execute(
        select(Tournament).order_by(Tournament.season.desc(), Tournament.name)
    )).scalars().all()

    return await render_page(request, db, "admin_schedule.html", context={
        "user": user,
        "tournament": tournament,
        "all_tournaments": all_tournaments,
        "teams_list": all_teams,
        "groups_info": groups_info if has_groups else None,
        "preview_matches": None,
        "circles": 1,
    })


@router.post("/admin/tournaments/{tournament_id}/schedule/preview")
async def admin_schedule_preview(
    tournament_id: int,
    request: Request,
    circles: int = Form(1),
    db: AsyncSession = Depends(get_db),
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse):
        return user

    tournament = await db.get(Tournament, tournament_id)
    if not tournament:
        raise HTTPException(404, "Турнир не найден")

    stmt_tt = select(TournamentTeam).where(
        TournamentTeam.tournament_id == tournament_id
    ).options(selectinload(TournamentTeam.team))
    tt_list = (await db.execute(stmt_tt)).scalars().all()

    all_teams = [tt.team for tt in tt_list if tt.team]
    if len(all_teams) < 2:
        raise HTTPException(400, "В турнире меньше 2 команд")

    groups_info: dict[str, list[int]] = {}
    has_groups = any(getattr(tt, "group_name", None) for tt in tt_list)
    if has_groups:
        for tt in tt_list:
            if not tt.team:
                continue
            gname = (tt.group_name or "").strip() or "—"
            groups_info.setdefault(gname, []).append(tt.team.id)

    from app.services.schedule_generator import generate_for_tournament
    pairs = generate_for_tournament(
        all_team_ids=[t.id for t in all_teams],
        circles=circles,
        groups_info=groups_info if has_groups else None,
    )

    preview_matches = [
        {
            "round": p.round,
            "group_name": p.group_name,
            "home_team_id": p.home_team_id,
            "away_team_id": p.away_team_id,
        }
        for p in pairs
    ]

    all_tournaments = (await db.execute(
        select(Tournament).order_by(Tournament.season.desc(), Tournament.name)
    )).scalars().all()

    return await render_page(request, db, "admin_schedule.html", context={
        "user": user,
        "tournament": tournament,
        "all_tournaments": all_tournaments,
        "teams_list": all_teams,
        "groups_info": groups_info if has_groups else None,
        "preview_matches": preview_matches,
        "circles": circles,
    })


@router.post("/admin/tournaments/{tournament_id}/schedule/confirm")
async def admin_schedule_confirm(
    tournament_id: int,
    request: Request,
    replace: str = Form(None),
    db: AsyncSession = Depends(get_db),
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse):
        return user

    tournament = await db.get(Tournament, tournament_id)
    if not tournament:
        raise HTTPException(404, "Турнир не найден")

    form = await request.form()

    if replace == "true":
        await db.execute(delete(Match).where(Match.tournament_id == tournament_id))

    from datetime import datetime as _dt
    created = 0
    i = 0
    while f"home_{i}" in form:
        if form.get(f"include_{i}") == "on":
            home_id = int(form[f"home_{i}"])
            away_id = int(form[f"away_{i}"])
            round_num = form.get(f"round_{i}", "1")
            date_str = (form.get(f"date_{i}") or "").strip()
            match_date = None
            if date_str:
                try:
                    match_date = _dt.strptime(date_str, "%Y-%m-%dT%H:%M")
                except ValueError:
                    pass
            if home_id != away_id:
                db.add(Match(
                    tournament_id=tournament_id,
                    round=str(round_num),
                    home_team_id=home_id,
                    away_team_id=away_id,
                    match_date=match_date,
                    status=MatchStatus.SCHEDULED,
                ))
                created += 1
        i += 1

    await db.commit()
    return RedirectResponse(
        url=f"/admin/tournaments/{tournament_id}/schedule?created={created}",
        status_code=303,
    )


# Кабинет тренера

@router.get("/coach", include_in_schema=False)
async def coach_panel(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="coach_panel.html",
    )



# ============================================================
# ADMIN: МОДЕРАЦИЯ ЗАЯВОК ТРЕНЕРОВ
# ============================================================

from urllib.parse import urlencode

from fastapi import Form
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.models.registration import (
    Registration,
    RegistrationPlayer,
    RegistrationStatus,
)


def admin_registration_redirect(
    application_id: int,
    message: str,
) -> RedirectResponse:
    query = urlencode({"message": message})

    return RedirectResponse(
        url=f"/admin/coach-applications/{application_id}?{query}",
        status_code=303,
    )


def verify_admin_form_origin(request: Request) -> None:
    """
    Базовая защита POST-запросов с cookie-авторизацией.
    Для production рекомендуется полноценный CSRF-токен.
    """
    from urllib.parse import urlparse

    origin = request.headers.get("origin")
    referer = request.headers.get("referer")
    expected = str(request.base_url).rstrip("/")

    if origin:
        actual = origin.rstrip("/")
    elif referer:
        parsed = urlparse(referer)
        actual = f"{parsed.scheme}://{parsed.netloc}"
    else:
        raise HTTPException(
            status_code=403,
            detail="Не удалось проверить источник запроса",
        )

    if actual != expected:
        raise HTTPException(
            status_code=403,
            detail="Недопустимый источник запроса",
        )


@router.get("/admin/coach-applications")
async def admin_coach_applications(
    request: Request,
    status_filter: str = "submitted",
    db: AsyncSession = Depends(get_db),
):
    admin = await get_admin_from_cookie(request, db)

    if isinstance(admin, RedirectResponse):
        return admin

    allowed = {
        "all",
        "draft",
        "submitted",
        "revision",
        "approved",
        "rejected",
    }

    if status_filter not in allowed:
        status_filter = "submitted"

    stmt = (
        select(Registration)
        .options(selectinload(Registration.players))
        .order_by(Registration.id.desc())
    )

    if status_filter != "all":
        stmt = stmt.where(
            Registration.status == status_filter
        )

    applications = (await db.scalars(stmt)).all()

    team_ids = {item.team_id for item in applications}
    tournament_ids = {
        item.tournament_id for item in applications
    }
    author_ids = {item.author_id for item in applications}

    teams = {}
    tournaments = {}
    authors = {}

    if team_ids:
        rows = (
            await db.scalars(
                select(Team).where(Team.id.in_(team_ids))
            )
        ).all()
        teams = {item.id: item for item in rows}

    if tournament_ids:
        rows = (
            await db.scalars(
                select(Tournament).where(
                    Tournament.id.in_(tournament_ids)
                )
            )
        ).all()
        tournaments = {item.id: item for item in rows}

    if author_ids:
        rows = (
            await db.scalars(
                select(User).where(User.id.in_(author_ids))
            )
        ).all()
        authors = {item.id: item for item in rows}

    counts_rows = (
        await db.execute(
            select(
                Registration.status,
                func.count(Registration.id),
            ).group_by(Registration.status)
        )
    ).all()

    counts = dict(counts_rows)
    counts["all"] = sum(counts.values())

    return await render_page(
        request,
        db,
        "admin_coach_applications.html",
        context={
            "user": admin,
            "applications": applications,
            "teams_map": teams,
            "tournaments_map": tournaments,
            "authors_map": authors,
            "status_filter": status_filter,
            "counts": counts,
        },
    )


@router.get("/admin/coach-applications/{application_id}")
async def admin_coach_application_detail(
    application_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    admin = await get_admin_from_cookie(request, db)

    if isinstance(admin, RedirectResponse):
        return admin

    application = await db.scalar(
        select(Registration)
        .options(selectinload(Registration.players))
        .where(Registration.id == application_id)
    )

    if application is None:
        raise HTTPException(
            status_code=404,
            detail="Заявка не найдена",
        )

    team = await db.get(Team, application.team_id)
    tournament = await db.get(
        Tournament,
        application.tournament_id,
    )
    author = await db.get(User, application.author_id)

    existing_ids = {
        player.existing_player_id
        for player in application.players
        if player.existing_player_id is not None
    }

    existing_players = {}

    if existing_ids:
        rows = (
            await db.scalars(
                select(Player).where(
                    Player.id.in_(existing_ids)
                )
            )
        ).all()

        existing_players = {
            player.id: player for player in rows
        }

    return await render_page(
        request,
        db,
        "admin_coach_application_detail.html",
        context={
            "user": admin,
            "application": application,
            "team": team,
            "tournament": tournament,
            "author": author,
            "existing_players": existing_players,
            "message": request.query_params.get("message"),
        },
    )


@router.get("/admin/api/coach-applications/count")
async def admin_coach_applications_count(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    admin = await get_admin_from_cookie(request, db)

    if isinstance(admin, RedirectResponse):
        raise HTTPException(
            status_code=401,
            detail="Необходима авторизация администратора",
        )

    pending = await db.scalar(
        select(func.count(Registration.id)).where(
            Registration.status
            == RegistrationStatus.SUBMITTED.value
        )
    )

    return {"pending": pending or 0}



@router.post(
    "/admin/coach-applications/{application_id}/review"
)
async def admin_review_coach_application(
    application_id: int,
    request: Request,
    action: str = Form(...),
    comment: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    admin = await get_admin_from_cookie(request, db)

    if isinstance(admin, RedirectResponse):
        return admin

    verify_admin_form_origin(request)

    if action not in {"approve", "revision", "reject"}:
        raise HTTPException(
            status_code=422,
            detail="Неизвестное действие",
        )

    comment = comment.strip()

    if len(comment) > 3000:
        raise HTTPException(
            status_code=422,
            detail="Комментарий слишком длинный",
        )

    if action in {"revision", "reject"} and not comment:
        return admin_registration_redirect(
            application_id,
            "Необходимо указать причину решения",
        )

    try:
        if action == "approve":
            result = await approve_registration(
                db=db,
                registration_id=application_id,
                admin_id=admin.id,
                comment=comment,
            )

            await db.commit()

            message = (
                "Заявка одобрена. "
                f"Новых игроков: {result['created_players']}. "
                f"Добавлено в состав: {result['added_to_roster']}. "
                f"Обновлено связей: {result['updated_roster']}."
            )

        else:
            application = await db.scalar(
                select(Registration)
                .where(Registration.id == application_id)
                .with_for_update()
            )

            if application is None:
                raise HTTPException(
                    status_code=404,
                    detail="Заявка не найдена",
                )

            if (
                application.status
                != RegistrationStatus.SUBMITTED.value
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Заявка уже обработана",
                )

            if action == "revision":
                application.status = (
                    RegistrationStatus.REVISION.value
                )
                message = "Заявка возвращена на доработку"

            else:
                application.status = (
                    RegistrationStatus.REJECTED.value
                )
                message = "Заявка отклонена"

            application.admin_comment = comment
            application.reviewed_by_id = admin.id
            application.reviewed_at = datetime.now(
                timezone.utc
            )

            await db.commit()

    except HTTPException:
        await db.rollback()
        raise

    except IntegrityError:
        await db.rollback()

        raise HTTPException(
            status_code=409,
            detail=(
                "Конфликт данных при обработке заявки. "
                "Проверьте состав и повторите попытку."
            ),
        )

    return admin_registration_redirect(
        application_id,
        message,
    )



# ============================================================
# ADMIN: РЕДАКТОР ЗАЯВОК ТРЕНЕРОВ
# ============================================================

from datetime import date
from urllib.parse import urlencode

from sqlalchemy.exc import IntegrityError

from app.models.registration import (
    Registration,
    RegistrationPlayer,
)
from app.services.registration_editor import (
    get_editable_registration,
    get_registration_player,
    validate_birth_date,
    validate_coach_birth_year,
    validate_name,
    validate_player_link,
    validate_position,
    validate_roster_capacity,
    validate_roster_entry,
)


def editor_redirect(
    registration_id: int,
    message: str,
) -> RedirectResponse:
    query = urlencode({"message": message})

    return RedirectResponse(
        url=(
            f"/admin/coach-applications/"
            f"{registration_id}?{query}"
        ),
        status_code=303,
    )


async def editor_admin(
    request: Request,
    db: AsyncSession,
):
    user = await get_admin_from_cookie(request, db)

    if isinstance(user, RedirectResponse):
        return user

    # Используем проверку источника POST-запросов,
    # которую добавляли в pages.py ранее.
    verify_admin_form_origin(request)

    return user


async def editor_commit(
    db: AsyncSession,
) -> None:
    try:
        await db.commit()

    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            409,
            "Конфликт данных. Проверьте номер "
            "и привязку существующего игрока.",
        )


# ------------------------------------------------------------
# ИЗМЕНИТЬ ДАННЫЕ ТРЕНЕРА
# ------------------------------------------------------------

@router.post(
    "/admin/coach-applications/{application_id}/coach"
)
async def admin_edit_application_coach(
    application_id: int,
    request: Request,
    first_name: str = Form(...),
    last_name: str = Form(...),
    birth_year: int = Form(...),
    photo: UploadFile = File(None),
    db: AsyncSession = Depends(get_db),
):
    admin = await editor_admin(request, db)

    if isinstance(admin, RedirectResponse):
        return admin

    registration = await get_editable_registration(
        db,
        application_id,
    )

    validate_coach_birth_year(birth_year)

    registration.coach_first_name = validate_name(
        first_name,
        "Имя тренера",
    )
    registration.coach_last_name = validate_name(
        last_name,
        "Фамилия тренера",
    )
    registration.coach_birth_year = birth_year

    if photo and photo.filename:
        registration.coach_photo_url = (
            await save_upload_file(photo, "coaches")
        )

    await editor_commit(db)

    return editor_redirect(
        application_id,
        "Данные тренера обновлены",
    )


# ------------------------------------------------------------
# ПОИСК СУЩЕСТВУЮЩИХ ИГРОКОВ
# ------------------------------------------------------------

@router.get("/admin/api/registration-players/search")
async def admin_search_registration_players(
    request: Request,
    q: str = "",
    db: AsyncSession = Depends(get_db),
):
    admin = await get_admin_from_cookie(request, db)

    if isinstance(admin, RedirectResponse):
        raise HTTPException(401, "Необходима авторизация")

    q = q.strip()

    if len(q) < 2:
        return []

    if len(q) > 100:
        raise HTTPException(422, "Слишком длинный запрос")

    escaped = (
        q.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    pattern = f"%{escaped}%"

    stmt = (
        select(Player)
        .where(
            or_(
                Player.first_name.ilike(
                    pattern, escape="\\"
                ),
                Player.last_name.ilike(
                    pattern, escape="\\"
                ),
                (
                    Player.last_name
                    + " "
                    + Player.first_name
                ).ilike(pattern, escape="\\"),
                (
                    Player.first_name
                    + " "
                    + Player.last_name
                ).ilike(pattern, escape="\\"),
            )
        )
        .order_by(
            Player.last_name,
            Player.first_name,
        )
        .limit(20)
    )

    players = (await db.scalars(stmt)).all()

    return [
        {
            "id": player.id,
            "first_name": player.first_name,
            "last_name": player.last_name,
            "birth_date": (
                player.date_of_birth.isoformat()
                if player.date_of_birth else ""
            ),
            "position": (
                player.position.value
                if player.position else ""
            ),
            "photo_url": player.photo_url or "",
        }
        for player in players
    ]


# ------------------------------------------------------------
# ДОБАВИТЬ ИГРОКА В ЗАЯВКУ
# ------------------------------------------------------------

@router.post(
    "/admin/coach-applications/{application_id}/players/add"
)
async def admin_add_registration_player(
    application_id: int,
    request: Request,
    first_name: str = Form(...),
    last_name: str = Form(...),
    birth_date: date = Form(...),
    position: str = Form(...),
    jersey_number: int = Form(...),
    existing_player_id: Optional[int] = Form(None),
    photo: UploadFile = File(None),
    db: AsyncSession = Depends(get_db),
):
    admin = await editor_admin(request, db)

    if isinstance(admin, RedirectResponse):
        return admin

    registration = await get_editable_registration(
        db,
        application_id,
    )

    await validate_roster_capacity(
        db,
        registration.id,
    )

    validate_birth_date(birth_date)

    await validate_roster_entry(
        db,
        registration.id,
        jersey_number,
        existing_player_id,
    )

    existing = await validate_player_link(
        db,
        existing_player_id,
    )

    first_name = validate_name(
        first_name, "Имя игрока"
    )
    last_name = validate_name(
        last_name, "Фамилия игрока"
    )
    position = validate_position(position)

    photo_url = None

    if existing is not None:
        # Для существующего игрока берём
        # официальные данные из основной БД.
        if (
            existing.date_of_birth is None
            or existing.position is None
            or not existing.photo_url
        ):
            raise HTTPException(
                422,
                "У существующего игрока не заполнены "
                "дата рождения, позиция или фото. "
                "Сначала исправьте его карточку.",
            )

        first_name = existing.first_name
        last_name = existing.last_name
        birth_date = existing.date_of_birth
        position = existing.position.value
        photo_url = existing.photo_url

    elif photo and photo.filename:
        photo_url = await save_upload_file(
            photo,
            "players",
        )

    if not photo_url:
        raise HTTPException(
            422,
            "Для нового игрока необходимо фото",
        )

    player = RegistrationPlayer(
        registration_id=registration.id,
        existing_player_id=existing_player_id,
        first_name=first_name,
        last_name=last_name,
        birth_date=birth_date,
        position=position,
        jersey_number=jersey_number,
        photo_url=photo_url,
    )

    db.add(player)

    await editor_commit(db)

    return editor_redirect(
        application_id,
        "Игрок добавлен в заявку",
    )


# ------------------------------------------------------------
# РЕДАКТИРОВАТЬ ИГРОКА В ЗАЯВКЕ
# ------------------------------------------------------------

@router.post(
    "/admin/coach-applications/"
    "{application_id}/players/{player_id}/edit"
)
async def admin_edit_registration_player(
    application_id: int,
    player_id: int,
    request: Request,
    first_name: str = Form(...),
    last_name: str = Form(...),
    birth_date: date = Form(...),
    position: str = Form(...),
    jersey_number: int = Form(...),
    existing_player_id: Optional[int] = Form(None),
    photo: UploadFile = File(None),
    db: AsyncSession = Depends(get_db),
):
    admin = await editor_admin(request, db)

    if isinstance(admin, RedirectResponse):
        return admin

    registration = await get_editable_registration(
        db,
        application_id,
    )

    player = await get_registration_player(
        db,
        registration.id,
        player_id,
    )

    validate_birth_date(birth_date)

    await validate_roster_entry(
        db,
        registration.id,
        jersey_number,
        existing_player_id,
        exclude_player_id=player.id,
    )

    existing = await validate_player_link(
        db,
        existing_player_id,
    )

    first_name = validate_name(
        first_name, "Имя игрока"
    )
    last_name = validate_name(
        last_name, "Фамилия игрока"
    )
    position = validate_position(position)

    if existing is not None:
        if (
            existing.date_of_birth is None
            or existing.position is None
            or not existing.photo_url
        ):
            raise HTTPException(
                422,
                "У выбранного игрока неполные данные "
                "в основной базе",
            )

        # Привязка к существующему игроку.
        # Его официальные данные не изменяем.

        player.existing_player_id = existing.id
        player.first_name = existing.first_name
        player.last_name = existing.last_name
        player.birth_date = existing.date_of_birth
        player.position = existing.position.value
        player.photo_url = existing.photo_url

    else:
        # Отвязка от основного Player означает,
        # что при одобрении будет создан новый Player.

        player.existing_player_id = None
        player.first_name = first_name
        player.last_name = last_name
        player.birth_date = birth_date
        player.position = position

        if photo and photo.filename:
            player.photo_url = await save_upload_file(
                photo,
                "players",
            )

        if not player.photo_url:
            raise HTTPException(
                422,
                "У нового игрока должно быть фото",
            )

    player.jersey_number = jersey_number

    await editor_commit(db)

    return editor_redirect(
        application_id,
        "Данные игрока обновлены",
    )


# ------------------------------------------------------------
# УДАЛИТЬ ИГРОКА ИЗ ЗАЯВКИ
# ------------------------------------------------------------

@router.post(
    "/admin/coach-applications/"
    "{application_id}/players/{player_id}/delete"
)
async def admin_delete_registration_player(
    application_id: int,
    player_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    admin = await editor_admin(request, db)

    if isinstance(admin, RedirectResponse):
        return admin

    registration = await get_editable_registration(
        db,
        application_id,
    )

    player = await get_registration_player(
        db,
        registration.id,
        player_id,
    )

    # Удаляем только запись из заявки.
    # Официальный Player остаётся в БД.

    await db.delete(player)
    await editor_commit(db)

    return editor_redirect(
        application_id,
        "Игрок удалён из заявки",
    )



