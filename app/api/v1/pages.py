from collections import defaultdict
from pathlib import Path
import shutil
import uuid
from fastapi import APIRouter, Request, Depends, HTTPException, Form, UploadFile, File
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from jose import jwt, JWTError
from app.db.session import get_db
from app.core.security import create_access_token, verify_password, ALGORITHM
from app.core.config import settings


from app.models.news import News
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


from sqlalchemy import select, or_


@router.get("/")
async def home_page(request: Request, tournament_id: int = None, db: AsyncSession = Depends(get_db)):
    # 1. Получаем последний сезон
    latest_season = await db.scalar(select(Tournament.season).order_by(Tournament.season.desc()).limit(1))
    if not latest_season:
        latest_season = str(datetime.now().year)

    # 2. Фильтруем турниры (как мы делали ранее)
    keywords = ["примера", "сегунда", "терсера", "региональ", "апертура", "клаусура",
                "primera", "segunda", "tersera", "regional", "apertura", "clausura"]
    conditions = [Tournament.name.ilike(f"%{kw}%") for kw in keywords]

    stmt_tournaments = select(Tournament).where(
        Tournament.season == latest_season,
        or_(*conditions)
    ).order_by(Tournament.name)
    tournaments = (await db.execute(stmt_tournaments)).scalars().all()

    # Текущее время
    now = datetime.now(timezone.utc)

    # 3. Ближайшие матчи (только будущие)
    stmt_upcoming = select(Match).where(
        Match.status == MatchStatus.SCHEDULED,
        Match.match_date >= now
    ).options(selectinload(Match.home_team), selectinload(Match.away_team), selectinload(Match.tournament)).order_by(
        Match.match_date.asc()).limit(4)
    upcoming_matches = (await db.execute(stmt_upcoming)).scalars().all()

    # 4. Последние результаты (ТОЛЬКО ТЕКУЩИЙ СЕЗОН)
    stmt_last_matches = select(Match).join(Tournament).where(
        Match.status == MatchStatus.FINISHED,
        Tournament.season == latest_season
    ).options(selectinload(Match.home_team), selectinload(Match.away_team), selectinload(Match.tournament)).order_by(
        Match.match_date.desc()).limit(4)
    last_matches = (await db.execute(stmt_last_matches)).scalars().all()

    # 5. Новости
    stmt_news = select(News).where(News.is_published == True).order_by(News.published_at.desc()).limit(3)
    latest_news = (await db.execute(stmt_news)).scalars().all()

    # 6. Определяем выбранный турнир для таблицы
    active_tournament = None
    if tournament_id:
        active_tournament = next((t for t in tournaments if t.id == tournament_id), None)

    if not active_tournament:
        active_tournament = next((t for t in tournaments if t.status == TournamentStatus.ACTIVE),
                                 tournaments[0] if tournaments else None)

    standings = []
    top_scorers = []

    if active_tournament:
        stmt_standings = select(TournamentStanding).where(
            TournamentStanding.tournament_id == active_tournament.id
        ).options(selectinload(TournamentStanding.team)).order_by(TournamentStanding.points.desc()).limit(5)
        standings = (await db.execute(stmt_standings)).scalars().all()
        top_scorers = await StatsService.get_top_scorers(db, active_tournament.id, limit=5)

    return await render_page(request, db, "index.html", context={
        "tournaments": tournaments, "last_matches": last_matches, "upcoming_matches": upcoming_matches,
        "latest_news": latest_news, "standings": standings, "top_scorers": top_scorers,
        "active_tournament": active_tournament,
        "selected_tournament_id": active_tournament.id if active_tournament else None,
        "latest_season": latest_season  # Передаем год для ссылки "Все матчи"
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
    limit = 20
    skip = (page - 1) * limit

    # 1. Получаем доступные года
    years_result = await db.execute(select(Tournament.season).distinct().order_by(Tournament.season.desc()))
    available_years = [y[0] for y in years_result.all() if y[0]]

    if not year and available_years:
        year = available_years[0]

    # 2. Получаем турниры для выбранного года
    tournaments = (
        await db.execute(select(Tournament).where(Tournament.season == year).order_by(Tournament.name))).scalars().all()

    # 3. Базовый запрос
    stmt = select(Match).join(Tournament).options(
        selectinload(Match.home_team),
        selectinload(Match.away_team),
        selectinload(Match.tournament)
    )
    count_stmt = select(func.count()).select_from(Match).join(Tournament)

    # 4. Применяем фильтры
    if year:
        stmt = stmt.where(Tournament.season == year)
        count_stmt = count_stmt.where(Tournament.season == year)
    if tournament_id:
        stmt = stmt.where(Match.tournament_id == tournament_id)
        count_stmt = count_stmt.where(Match.tournament_id == tournament_id)
    if date_filter:
        stmt = stmt.where(func.date(Match.match_date) == date_filter)
        count_stmt = count_stmt.where(func.date(Match.match_date) == date_filter)

    # 5. Сортировка и пагинация
    total_matches = await db.scalar(count_stmt)
    stmt = stmt.order_by(Match.match_date.desc()).offset(skip).limit(limit)
    matches = (await db.execute(stmt)).scalars().all()

    total_pages = (total_matches + limit - 1) // limit if total_matches > 0 else 1

    return await render_page(request, db, "matches.html", context={
        "matches": matches,
        "page": page,
        "total_pages": total_pages,
        "total_matches": total_matches,
        "available_years": available_years,
        "tournaments": tournaments,
        "current_year": year,
        "selected_tournament_id": tournament_id,
        "date_filter": date_filter
    })


@router.get("/about")
async def about_page(request: Request, db: AsyncSession = Depends(get_db)):
    return await render_page(request, db, "about.html")


@router.get("/tournaments")
async def tournaments_list_page(request: Request, season: str = None, db: AsyncSession = Depends(get_db)):
    # 1. Получаем все доступные сезоны
    seasons_result = await db.execute(select(Tournament.season).distinct().order_by(Tournament.season.desc()))
    available_seasons = [s[0] for s in seasons_result.all() if s[0]]

    # 2. Если сезон не выбран, берем последний
    if not season and available_seasons:
        season = available_seasons[0]

    # 3. Достаем ВСЕ турниры за этот год (вообще без фильтров!)
    stmt = select(Tournament).where(Tournament.season == season).order_by(Tournament.name)
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


@router.get("/tournaments/{tournament_id}")
async def tournament_page(tournament_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    tournament = await db.get(Tournament, tournament_id)
    if not tournament:
        raise HTTPException(status_code=404, detail="Турнир не найден")

    # 1. ТАБЛИЦА
    stmt_standings = select(TournamentStanding).where(
        TournamentStanding.tournament_id == tournament_id
    ).options(selectinload(TournamentStanding.team))

    standings_raw = (await db.execute(stmt_standings)).scalars().all()
    is_cup = any(s.group_name for s in standings_raw) or tournament.division == "CUP"

    if is_cup:
        standings = sorted(standings_raw, key=lambda x: (x.group_name or 'ZZZ', -x.points, -x.goals_for))
    else:
        standings = sorted(standings_raw, key=lambda x: (-x.points, -x.goals_for))

    grouped_standings = {}
    for s in standings:
        group_name = s.group_name if s.group_name else "Общая таблица"
        if group_name not in grouped_standings:
            grouped_standings[group_name] = []
        grouped_standings[group_name].append(s)

    # 2. МАТЧИ
    stmt_matches = select(Match).where(Match.tournament_id == tournament_id).options(
        selectinload(Match.home_team),
        selectinload(Match.away_team),
        selectinload(Match.events).selectinload(MatchEvent.player)
    )
    matches_raw = (await db.execute(stmt_matches)).scalars().all()

    fallback_date = datetime.min.replace(tzinfo=timezone.utc)
    matches = sorted(
        matches_raw,
        key=lambda x: (
            int(x.round) if x.round and str(x.round).isdigit() else 0,
            x.match_date.timestamp() if x.match_date else fallback_date.timestamp()
        )
    )

    # 3. РАЗДЕЛЯЕМ НА ГРУППЫ И ПЛЕЙ-ОФФ
    group_matches = [m for m in matches if m.stage == MatchStage.GROUP or m.stage is None]
    playoff_matches = [m for m in matches if m.stage in [MatchStage.PLAYOFF, MatchStage.FINAL]]
    playoff_matches.sort(key=lambda x: (x.round or 0, x.match_date or fallback_date))

    playoff_brackets = {}
    for m in playoff_matches:
        r_num = m.round if m.round else 1
        if r_num not in playoff_brackets:
            playoff_brackets[r_num] = []
        playoff_brackets[r_num].append(m)
    playoff_brackets = dict(sorted(playoff_brackets.items()))
    round_names = {1: "1/8 ФИНАЛА", 2: "1/4 ФИНАЛА", 3: "1/2 ФИНАЛА", 4: "ФИНАЛ"}

    # 4. ШАХМАТКА
    matrix_data = {}
    for m in matches:
        score = f"{m.home_score}-{m.away_score}" if m.home_score is not None and m.away_score is not None else "-:-"
        matrix_data[(m.home_team_id, m.away_team_id)] = {"score": score, "match_id": m.id}

    teams_list = [s.team for s in standings]

    # 5. ПРОГРЕСС
    progress_data = {}
    for team in teams_list:
        team_matches = [m for m in matches if m.home_team_id == team.id or m.away_team_id == team.id]
        team_matches.sort(key=lambda x: x.match_date.timestamp() if x.match_date else 0, reverse=True)
        recent_form = []
        for m in team_matches[:5]:
            if m.home_score is not None and m.away_score is not None:
                if m.home_team_id == team.id:
                    res = 'В' if m.home_score > m.away_score else ('П' if m.home_score < m.away_score else 'Н')
                else:
                    res = 'В' if m.away_score > m.home_score else ('П' if m.away_score < m.home_score else 'Н')
                recent_form.append({"result": res, "match_id": m.id})
        progress_data[team.id] = recent_form

    # 6. ПОПАП
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
            "date": m.match_date.strftime('%d.%m.%Y %H:%M') if m.match_date else 'TBA',
            "stadium": m.stadium or 'Стадион TBA', "events": events_list,
            "no_events_but_score": bool(m.home_score is not None and len(events_list) == 0),
        }

    # 7. КАЛЕНДАРЬ (ГРУППЫ)
    matches_by_round = defaultdict(list)
    matches_by_date = defaultdict(list)

    for m in matches:  # Проходимся по всем матчам турнира
        if m.round and int(m.round) > 0 and int(m.round) < 5:
            group_key = f"{m.round} ТУР" if m.stage == MatchStage.GROUP else "ПЛЕЙ-ОФФ"
        else:
            group_key = "ГРУППОВОЙ ЭТАП"
        matches_by_round[group_key].append(m)

        if m.match_date:
            date_str = m.match_date.strftime('%d %b, %a').upper()
            matches_by_date[date_str].append(m)
        else:
            matches_by_date['ДАТА TBA'].append(m)

    for date_key in matches_by_date:
        matches_by_date[date_key].sort(key=lambda x: x.match_date.timestamp() if x.match_date else 0)

    matches_by_date = dict(sorted(matches_by_date.items(),
                                  key=lambda item: item[1][0].match_date.timestamp() if item[1][
                                      0].match_date else float('inf')))
    matches_by_round = dict(sorted(matches_by_round.items(), key=lambda item: (not item[0][0].isdigit(), item[0])))
    # 8. СТАТИСТИКА
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

    # 9. ДИСКВАЛИФИКАЦИИ
    stmt_disq = select(Disqualification).where(Disqualification.tournament_id == tournament_id).options(
        selectinload(Disqualification.player), selectinload(Disqualification.team)
    ).order_by(Disqualification.decision_date.desc())
    all_disq = (await db.execute(stmt_disq)).scalars().all()
    active_disq = [d for d in all_disq if d.matches_remaining and d.matches_remaining > 0]
    completed_disq = [d for d in all_disq if not d.matches_remaining or d.matches_remaining == 0]

    # 10. НОВОСТИ
    stmt_news = select(News).where(News.is_published == True, News.tournament_id == tournament_id).order_by(
        News.published_at.desc()).limit(9)
    tournament_news = (await db.execute(stmt_news)).scalars().all()

    return await render_page(request, db, "tournament_detail.html", context={
        "tournament": tournament, "grouped_standings": grouped_standings, "is_cup": is_cup,
        "playoff_brackets": playoff_brackets, "round_names": round_names, "matches": matches,
        "standings": standings, "teams_list": teams_list, "matrix": matrix_data,
        "progress": progress_data, "matches_dict": matches_dict,
        "matches_by_round": matches_by_round, "matches_by_date": matches_by_date,
        "tournament_stats_goals": tournament_stats_goals, "tournament_stats_cards": tournament_stats_cards,
        "active_disq": active_disq, "completed_disq": completed_disq, "tournament_news": tournament_news,
        "home_penalty": m.home_penalty,
        "away_penalty": m.away_penalty,
        "stage_name": m.stage_name,
    })

@router.get("/participants/teams")
async def participants_teams_page(
        request: Request,
        page: int = 1,
        season: str = None,
        db: AsyncSession = Depends(get_db)
):
    limit = 24
    skip = (page - 1) * limit

    # 1. Получаем список доступных сезонов
    seasons_result = await db.execute(select(Tournament.season).distinct().order_by(Tournament.season.desc()))
    available_seasons = [s[0] for s in seasons_result.all() if s[0]]

    # Если сезон не выбран, берем последний (например, 2026)
    if not season and available_seasons:
        season = available_seasons[0]

    # 2. Формируем запрос команд (с учетом сезона)
    stmt = select(Team)
    count_stmt = select(func.count()).select_from(Team)

    if season:
        # Джоиним через таблицу связей, чтобы найти команды, игравшие в этом сезоне
        stmt = stmt.join(TournamentTeam).join(Tournament).where(Tournament.season == season).distinct()
        count_stmt = count_stmt.join(TournamentTeam).join(Tournament).where(Tournament.season == season)

    total_teams = await db.scalar(count_stmt)
    stmt = stmt.order_by(Team.name).offset(skip).limit(limit)

    teams = (await db.execute(stmt)).scalars().all()
    total_pages = (total_teams + limit - 1) // limit if total_teams > 0 else 1

    return await render_page(request, db, "participants.html", context={
        "teams": teams,
        "page": page,
        "total_pages": total_pages,
        "total_teams": total_teams,
        "available_seasons": available_seasons,
        "current_season": season
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
async def news_page(
    request: Request,
    page: int = 1,
    tournament_id: int = None,
    team_id: int = None,
    db: AsyncSession = Depends(get_db)
):
    limit = 10 # На оригинальном сайте новости крупные, поэтому по 10 штук на страницу
    skip = (page - 1) * limit

    # Получаем списки для фильтров
    tournaments = (await db.execute(select(Tournament).order_by(Tournament.name))).scalars().all()
    teams = (await db.execute(select(Team).order_by(Team.name))).scalars().all()

    # Базовый запрос
    stmt = select(News).where(News.is_published == True)
    count_stmt = select(func.count()).select_from(News).where(News.is_published == True)

    # Применяем фильтры
    if tournament_id:
        stmt = stmt.where(News.tournament_id == tournament_id)
        count_stmt = count_stmt.where(News.tournament_id == tournament_id)

    if team_id:
        stmt = stmt.join(News.teams).where(Team.id == team_id)
        count_stmt = count_stmt.join(News.teams).where(Team.id == team_id)

    # Загружаем связи и сортируем
    stmt = stmt.options(
        selectinload(News.teams),
        selectinload(News.tournament)
    ).order_by(News.published_at.desc()).offset(skip).limit(limit)

    total_news = await db.scalar(count_stmt)
    news_list = (await db.execute(stmt)).scalars().all()
    total_pages = (total_news + limit - 1) // limit if total_news > 0 else 1

    return await render_page(request, db, "news.html", context={
        "news_list": news_list,
        "page": page,
        "total_pages": total_pages,
        "tournaments": tournaments,
        "teams": teams,
        "selected_tournament": tournament_id,
        "selected_team": team_id
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
async def media_page(request: Request, db: AsyncSession = Depends(get_db)):
    stmt = select(MediaAlbum).options(selectinload(MediaAlbum.items)).order_by(MediaAlbum.created_at.desc())
    albums = (await db.execute(stmt)).scalars().all()
    return await render_page(request, db, "media.html", context={"albums": albums})


@router.get("/matches/{match_id}")
async def match_protocol_page(match_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    # 1. Загружаем матч со всеми связями
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

    # Сортируем события по минутам
    if match.events:
        match.events.sort(key=lambda e: e.minute)

    # 2. Загружаем составы команд (заявки на турнир)
    home_roster = await crud_team_player.get_by_tournament_and_team(
        db, tournament_id=match.tournament_id, team_id=match.home_team_id
    )
    away_roster = await crud_team_player.get_by_tournament_and_team(
        db, tournament_id=match.tournament_id, team_id=match.away_team_id
    )

    # 3. Загружаем турнирную таблицу для расчета позиций
    stmt_standings = select(TournamentStanding).where(
        TournamentStanding.tournament_id == match.tournament_id
    ).order_by(TournamentStanding.points.desc(), TournamentStanding.goals_for.desc())
    standings = (await db.execute(stmt_standings)).scalars().all()

    # Ищем позиции и данные команд в таблице
    home_pos = next((i + 1 for i, s in enumerate(standings) if s.team_id == match.home_team_id), None)
    away_pos = next((i + 1 for i, s in enumerate(standings) if s.team_id == match.away_team_id), None)
    home_standing = next((s for s in standings if s.team_id == match.home_team_id), None)
    away_standing = next((s for s in standings if s.team_id == match.away_team_id), None)

    # 4. Топ бомбардир турнира
    top_scorers = await StatsService.get_top_scorers(db, match.tournament_id, limit=1)
    top_scorer = top_scorers[0] if top_scorers else None

    # 5. Личные встречи (Head-to-Head)
    # Ищем завершенные матчи между этими двумя командами в рамках этого же турнира (или вообще)
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

    # 6. ГРУППИРОВКА СОБЫТИЙ ПО ИГРОКАМ
    # Это нужно для того, чтобы в протоколе выводить голы/карточки строго напротив нужного игрока
    events_by_player = {}
    if match.events:
        for e in match.events:
            if e.player_id:
                if e.player_id not in events_by_player:
                    events_by_player[e.player_id] = []
                events_by_player[e.player_id].append(e)

    # 7. РЕНДЕР СТРАНИЦЫ
    return await render_page(request, db, "match_detail.html", context={
        "match": match,
        "home_roster": home_roster,
        "away_roster": away_roster,
        "home_standing": home_standing,
        "away_standing": away_standing,
        "home_pos": home_pos,
        "away_pos": away_pos,
        "top_scorer": top_scorer,
        "h2h_matches": h2h_matches,  # <--- Передаем личные встречи
        "events_by_player": events_by_player,  # <--- Передаем сгруппированные события
        "active_tab": request.query_params.get("tab", "preview")
    })

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

    # 1. Счетчики (для мелких бейджей)
    users_count = await db.scalar(select(func.count()).select_from(User))
    news_count = await db.scalar(select(func.count()).select_from(News))

    # 2. Ближайшие матчи (топ 4)
    upcoming_stmt = select(Match).where(
        Match.status == MatchStatus.SCHEDULED,
        Match.match_date.is_not(None)
    ).options(
        selectinload(Match.home_team), selectinload(Match.away_team), selectinload(Match.tournament)
    ).order_by(Match.match_date.asc()).limit(4)
    upcoming_matches = (await db.execute(upcoming_stmt)).scalars().all()

    # 3. Последние новости (топ 3)
    news_stmt = select(News).where(News.is_published == True).order_by(News.published_at.desc()).limit(3)
    recent_news = (await db.execute(news_stmt)).scalars().all()

    # 4. Последние добавленные команды (топ 4)
    teams_stmt = select(Team).order_by(Team.created_at.desc()).limit(4)
    recent_teams = (await db.execute(teams_stmt)).scalars().all()

    # 5. Активные турниры (топ 3)
    tournaments_stmt = select(Tournament).where(
        Tournament.status == TournamentStatus.ACTIVE
    ).order_by(Tournament.start_at.desc()).limit(3)
    active_tournaments = (await db.execute(tournaments_stmt)).scalars().all()

    return await render_page(request, db, "admin.html", context={
        "user": user,
        "users_count": users_count,
        "news_count": news_count,
        "upcoming_matches": upcoming_matches,
        "recent_news": recent_news,
        "recent_teams": recent_teams,
        "active_tournaments": active_tournaments
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

    # =========================================================================
    # ЛОГИКА АВТОМАТИЧЕСКОГО СОЗДАНИЯ НОВОСТИ О ЧЕМПИОНЕ
    # =========================================================================
    if status == TournamentStatus.FINISHED:
        # 1. Проверяем, нет ли уже новости о чемпионе
        existing_news = await db.scalar(
            select(News).where(News.tournament_id == tournament_id, News.title.ilike("%Чемпион%"))
        )
        if not existing_news:
            # 2. Берем первую команду из таблицы
            stmt = select(TournamentStanding).where(
                TournamentStanding.tournament_id == tournament_id
            ).options(selectinload(TournamentStanding.team)).order_by(
                TournamentStanding.points.desc(), TournamentStanding.goals_for.desc()
            ).limit(1)
            winner_standing = (await db.execute(stmt)).scalar_one_or_none()

            if winner_standing and winner_standing.team:
                winner_team = winner_standing.team

                # 3. Создаем заголовок и контент
                news_title = f"{winner_team.name} - Чемпион {tournament.name}"
                # Очищаем слаг от кириллицы, оставляем только английские буквы и дефисы
                import re
                slug = re.sub(r'[^a-z0-9]+', '-', news_title.lower()).strip('-')
                if not slug:
                    slug = f"champion-{tournament_id}-{int(datetime.now().timestamp())}"

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

                # 4. Создаем новость
                new_news = News(
                    title=news_title,
                    slug=slug,
                    content=news_content,
                    published_at=datetime.now(timezone.utc),
                    is_published=True,
                    tournament_id=tournament_id
                )
                new_news.teams.append(winner_team)
                db.add(new_news)
                await db.commit()
                print(f"  ✅ Автоматически создана новость о чемпионе: {news_title}")
    # =========================================================================

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

    # Если матч сразу создан как завершенный — пересчитываем таблицу
    if status == MatchStatus.FINISHED:
        await StandingsService.recalculate_standings(db, tournament_id)

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

    # Создаем запрос
    stmt = select(News).order_by(News.published_at.desc(), News.id.desc())

    # ВЫПОЛНЯЕМ запрос и получаем список
    news_list = (await db.execute(stmt)).scalars().all()

    return await render_page(request, db, "admin_news.html", context={
        "user": user,
        "news_list": news_list  # <--- Передаем уже полученный список, а не stmt
    })


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


from sqlalchemy import select, func, or_, and_
from sqlalchemy.orm import selectinload
from datetime import datetime


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

    # Достаем все матчи команды
    stmt = select(Match).where(
        or_(Match.home_team_id == team.id, Match.away_team_id == team.id)
    ).options(
        selectinload(Match.home_team),
        selectinload(Match.away_team),
        selectinload(Match.tournament)
    ).order_by(Match.match_date.desc())

    all_matches = (await db.execute(stmt)).scalars().all()

    # --- ИСПРАВЛЕНИЕ 1: Сортируем матчи правильно (TBA - в конец) ---
    # В Python нельзя напрямую сортировать datetime и None, поэтому используем ключ
    all_matches = sorted(
        all_matches,
        key=lambda x: x.match_date.timestamp() if x.match_date else 0,
        reverse=True
    )

    # Считаем статистику (только по завершенным)
    finished_matches = [m for m in all_matches if m.status == MatchStatus.FINISHED]
    total_games = len(finished_matches)
    total_wins = 0
    total_goals = 0
    tournaments_set = set()

    # --- ИСПРАВЛЕНИЕ 2: Считаем турниры по ВСЕМ матчам, а не только по завершенным ---
    for m in all_matches:
        tournaments_set.add(m.tournament_id)

    for m in finished_matches:
        if m.home_team_id == team.id:
            total_goals += (m.home_score or 0)
            if (m.home_score or 0) > (m.away_score or 0): total_wins += 1
        else:
            total_goals += (m.away_score or 0)
            if (m.away_score or 0) > (m.home_score or 0): total_wins += 1

    # Форма (последние 5)
    recent_form = []
    for m in finished_matches[:5]:
        if m.home_team_id == team.id:
            if (m.home_score or 0) > (m.away_score or 0):
                recent_form.append("В")
            elif (m.home_score or 0) < (m.away_score or 0):
                recent_form.append("П")
            else:
                recent_form.append("Н")
        else:
            if (m.away_score or 0) > (m.home_score or 0):
                recent_form.append("В")
            elif (m.away_score or 0) < (m.home_score or 0):
                recent_form.append("П")
            else:
                recent_form.append("Н")

    # Группируем матчи по турнирам для календаря
    grouped_matches = {}
    for m in all_matches:
        t_name = m.tournament.name if m.tournament else "Товарищеские матчи"
        if t_name not in grouped_matches:
            grouped_matches[t_name] = []
        grouped_matches[t_name].append(m)

    # Получаем текущий турнир и таблицу
    active_tournament = next((m.tournament for m in all_matches if m.tournament), None)
    standings = []
    if active_tournament:
        stmt_standings = select(TournamentStanding).where(
            TournamentStanding.tournament_id == active_tournament.id
        ).options(selectinload(TournamentStanding.team)).order_by(
            TournamentStanding.points.desc()
        ).limit(5)
        standings = (await db.execute(stmt_standings)).scalars().all()

    return await render_page(request, db, "team_detail.html", context={
        "team": team,
        "stats": {
            "games": total_games,
            "wins": total_wins,
            "goals": total_goals,
            "tournaments": len(tournaments_set) # Теперь будет 8
        },
        "team_news": team_news,
        "active_tab": request.query_params.get("tab", "info"),
        "recent_form": recent_form,
        "grouped_matches": grouped_matches,
        "active_tournament": active_tournament,
        "standings": standings
    })


@router.get("/tournaments/{tournament_id}/teams/{team_id}/application")
async def team_application_page(
        tournament_id: int,
        team_id: int,
        request: Request,
        db: AsyncSession = Depends(get_db)
):
    # 1. Получаем турнир и команду
    tournament = await db.get(Tournament, tournament_id)
    team = await db.get(Team, team_id)
    if not tournament or not team:
        raise HTTPException(status_code=404, detail="Турнир или команда не найдены")

    # 2. Получаем заявку команды (всех игроков)
    stmt = select(TeamPlayer).where(
        TeamPlayer.tournament_id == tournament_id,
        TeamPlayer.team_id == team_id
    ).options(selectinload(TeamPlayer.player)).order_by(TeamPlayer.jersey_number)

    roster = (await db.execute(stmt)).scalars().all()

    # 3. Разделяем на заявленных и отзаявленных
    active_players = [p for p in roster if p.is_active]
    released_players = [p for p in roster if not p.is_active]

    # 4. Считаем средний возраст (только для активных)
    today = date.today()
    ages = []
    for tp in active_players:
        if tp.player.date_of_birth:
            age = today.year - tp.player.date_of_birth.year - (
                        (today.month, today.day) < (tp.player.date_of_birth.month, tp.player.date_of_birth.day))
            ages.append(age)

    avg_age = round(sum(ages) / len(ages), 2) if ages else 0

    # 5. Получаем статистику игроков в этом турнире (голы, карточки)
    # Для оптимизации можно сделать один запрос, но пока для простоты сделаем словарь
    stats_dict = {}
    # ... (здесь можно добавить логику подсчета голов из MatchEvent, если нужно) ...

    # 6. Навигация по турниру (для правого меню)
    # (Предполагается, что у тебя есть функция get_mega_menu_context или похожая,
    # но здесь мы просто передадим турнир для меню)

    return await render_page(request, db, "team_application.html", context={
        "tournament": tournament,
        "team": team,
        "active_players": active_players,
        "released_players": released_players,
        "avg_age": avg_age,
        "stats": stats_dict
    })


