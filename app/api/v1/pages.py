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
from app.services.champion_card import generate_champion_card

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

router = APIRouter()
BASE_DIR = Path(__file__).resolve().parent.parent.parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")

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

    # Берем только те турниры, где заполнена категория меню и стоит галочка
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

        # Если такой категории еще нет в словаре, создаем для нее список
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

    # Получаем партнеров для футера (только активных, по порядку)
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


from sqlalchemy import select, or_, desc, case, update, delete


@router.get("/")
async def home_page(request: Request, tournament_id: int = None, db: AsyncSession = Depends(get_db)):
    await check_and_finish_expired_tournaments(db)
    from datetime import date
    from sqlalchemy import func

    # 1. ПОЛУЧАЕМ ПОСЛЕДНИЙ СЕЗОН
    latest_season = await db.scalar(select(Tournament.season).order_by(Tournament.season.desc()).limit(1))
    if not latest_season:
        latest_season = str(datetime.now().year)

    # 2. ФИЛЬТРУЕМ ТУРНИРЫ И СОРТИРУЕМ (СТРОГО ПО РЕОРДЕРУ)
    keywords = ["примера", "сегунда", "терсера", "региональ", "junior", "primera", "segunda", "tersera", "regional"]
    conditions = [Tournament.name.ilike(f"%{kw}%") for kw in keywords]

    stmt_tournaments = select(Tournament).where(
        Tournament.season == latest_season,
        or_(*conditions)
    ).order_by(
        # Сначала строго по твоему порядку из админки
        Tournament.sort_order.asc(),
        # Если sort_order одинаковый (например, везде 0), тогда по алфавиту
        Tournament.name.asc()
    )
    tournaments = (await db.execute(stmt_tournaments)).scalars().all()

    # 3. ОПРЕДЕЛЯЕМ АКТИВНЫЙ ТУРНИР (для правой колонки)
    active_tournament = None
    if tournament_id:
        active_tournament = next((t for t in tournaments if t.id == tournament_id), None)

    if not active_tournament and tournaments:
        active_tournament = tournaments[0]

    # 4. МАТЧИ НА СЕГОДНЯ
    today = date.today()
    stmt_today = select(Match).where(
        func.date(Match.match_date) == today
    ).options(
        selectinload(Match.home_team),
        selectinload(Match.away_team),
        selectinload(Match.tournament)
    ).order_by(
        Match.match_date.asc().nullslast()
    )

    today_matches = (await db.execute(stmt_today)).scalars().all()

    # 5. ТАБЛИЦА, БОМБАРДИРЫ И ДИСКВАЛИФИКАЦИИ
    standings = []
    top_scorers = []
    active_disqualifications = []

    if active_tournament:
        # А) Таблица
        stmt_standings = select(TournamentStanding).where(
            TournamentStanding.tournament_id == active_tournament.id
        ).options(selectinload(TournamentStanding.team)).order_by(
            TournamentStanding.points.desc(),
            (TournamentStanding.goals_for - TournamentStanding.goals_against).desc()
        )
        standings = (await db.execute(stmt_standings)).scalars().all()

        # Б) Бомбардиры
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

        # В) Активные дисквалификации (matches_remaining > 0)
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

    # 6. ПОСЛЕДНИЕ НОВОСТИ (ОБНОВЛЕНА СОРТИРОВКА)
    stmt_news = select(News).where(News.is_published == True).order_by(
        News.sort_order.asc(),
        News.published_at.desc(),
        News.id.desc()
    ).limit(4)
    latest_news = (await db.execute(stmt_news)).scalars().all()

    # 7. РЕНДЕР
    return await render_page(request, db, "index.html", context={
        "tournaments": tournaments,
        "today_matches": today_matches,
        "latest_news": latest_news,
        "standings": standings,
        "top_scorers": top_scorers,
        "active_disqualifications": active_disqualifications,
        "active_tournament": active_tournament,
        "selected_tournament_id": active_tournament.id if active_tournament else None,
        "latest_season": latest_season
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
    from datetime import datetime, timezone
    from sqlalchemy import case, func

    limit = 20
    skip = (page - 1) * limit

    # 1. Получаем доступные года
    years_result = await db.execute(select(Tournament.season).distinct().order_by(Tournament.season.desc()))
    available_years = [y[0] for y in years_result.all() if y[0]]

    if not year and available_years:
        year = available_years[0]

    # 2. Получаем турниры для выбранного года (сортируем по названию)
    tournaments = (
        await db.execute(select(Tournament).where(Tournament.season == year).order_by(Tournament.name))
    ).scalars().all()

    # 3. Базовый запрос с жадной загрузкой связей
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

    # 5. УМНАЯ СОРТИРОВКА ПО ДАТЕ
    now = datetime.now(timezone.utc)

    # Группируем: 0 - будущие (дата >= сейчас), 1 - прошлые (дата < сейчас), 2 - без даты (TBA)
    time_group = case(
        (Match.match_date >= now, 0),
        (Match.match_date < now, 1),
        else_=2
    )

    stmt = stmt.order_by(
        # 1. Сначала будущие (0), потом прошлые (1), потом TBA (2)
        time_group,

        # 2. Внутри будущих: от ближайшего к дальнему (ASC)
        case((Match.match_date >= now, Match.match_date), else_=None).asc().nullslast(),

        # 3. Внутри прошлых: от недавних к старым (DESC)
        case((Match.match_date < now, Match.match_date), else_=None).desc().nullslast(),

        Match.id.desc()
    )

    # 6. Пагинация
    total_matches = await db.scalar(count_stmt)
    stmt = stmt.offset(skip).limit(limit)
    matches = (await db.execute(stmt)).scalars().all()

    total_pages = (total_matches + limit - 1) // limit if total_matches > 0 else 1

    # 7. Рендер страницы
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


    await check_and_finish_expired_tournaments(db)
    # 1. Получаем все доступные сезоны
    seasons_result = await db.execute(select(Tournament.season).distinct().order_by(Tournament.season.desc()))
    available_seasons = [s[0] for s in seasons_result.all() if s[0]]

    # 2. Если сезон не выбран, берем последний
    if not season and available_seasons:
        season = available_seasons[0]

    # 3. Достаем ВСЕ турниры за этот год и СОРТИРУЕМ по sort_order
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

    # 3. РАЗДЕЛЯЕМ НА ГРУППЫ И ПЛЕЙ-ОФФ (для сетки)
    group_matches = [m for m in matches if m.stage == MatchStage.GROUP or m.stage is None]
    playoff_matches = [m for m in matches if m.stage in [MatchStage.PLAYOFF, MatchStage.FINAL]]
    playoff_matches.sort(key=lambda x: (x.round or 0, x.match_date or fallback_date))

    brackets_data = {}
    for m in playoff_matches:
        bracket_name = m.group_name if m.group_name else "Плей-офф"
        if bracket_name not in brackets_data:
            brackets_data[bracket_name] = {}
        r_num = m.round if m.round else 1
        if r_num not in brackets_data[bracket_name]:
            brackets_data[bracket_name][r_num] = []
        brackets_data[bracket_name][r_num].append(m)

    for b_name in brackets_data:
        brackets_data[b_name] = dict(sorted(brackets_data[b_name].items()))

    round_names = {1: "1/8 ФИНАЛА", 2: "1/4 ФИНАЛА", 3: "1/2 ФИНАЛА", 4: "ФИНАЛ", 5: "ЗА 3 МЕСТО"}

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

    # 6. ПОПАП (Данные для модального окна)
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
            "home_penalty": m.home_penalty,
            "away_penalty": m.away_penalty,
            "stage_name": m.stage_name,
            "tech_loss": m.tech_loss,
            "date": m.match_date.strftime('%d.%m.%Y %H:%M') if m.match_date else 'TBA',
            "stadium": m.stadium or 'Стадион TBA', "events": events_list,
            "no_events_but_score": bool(m.home_score is not None and len(events_list) == 0),
        }

    # 7. КАЛЕНДАРЬ (ГРУППИРОВКА ПО ТУРАМ И ДАТАМ)
    matches_by_round = defaultdict(list)
    matches_by_date = defaultdict(list)

    # Умная сортировка: сначала будущие (по возрастанию), потом прошлые (от новых к старым)
    matches.sort(key=lambda x: (
        0 if x.status == MatchStatus.SCHEDULED else 1,
        x.match_date.timestamp() if x.status == MatchStatus.SCHEDULED and x.match_date else 9999999999,
        -x.match_date.timestamp() if x.status != MatchStatus.SCHEDULED and x.match_date else 0
    ))

    for m in matches:
        # ГЛАВНОЕ ИСПРАВЛЕНИЕ: Опираемся на stage_name (название тура), а не на поле stage
        if m.status == MatchStatus.SCHEDULED:
            group_key = "БЛИЖАЙШИЕ МАТЧИ"
        else:
            if m.stage_name:
                group_key = m.stage_name.upper()  # Например: "7 ТУР" или "1/4 ФИНАЛА"
            elif m.round and str(m.round).isdigit() and int(m.round) > 0:
                group_key = f"{m.round} ТУР"
            else:
                group_key = "ГРУППОВОЙ ЭТАП"

        matches_by_round[group_key].append(m)

        # Группировка по датам
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

    import re
    # Сортируем группы туров: "БЛИЖАЙШИЕ МАТЧИ" первые, плей-офф последние, остальные по возрастанию номера
    def group_sort_key(item):
        key = item[0]
        if key == "БЛИЖАЙШИЕ МАТЧИ": return (0, 0)
        if "ПЛЕЙ-ОФФ" in key or "ФИНАЛ" in key or "1/" in key: return (2, 0)

        match = re.search(r'\d+', key)
        if match: return (1, int(match.group()))
        return (1, 999)

    matches_by_round = dict(sorted(matches_by_round.items(), key=group_sort_key))

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

    # ОТДАЕМ В ШАБЛОН
    return await render_page(request, db, "tournament_detail.html", context={
        "tournament": tournament, "grouped_standings": grouped_standings, "is_cup": is_cup,
        "brackets_data": brackets_data, "round_names": round_names, "matches": matches,
        "standings": standings, "teams_list": teams_list, "matrix": matrix_data,
        "progress": progress_data, "matches_dict": matches_dict,
        "matches_by_round": matches_by_round, "matches_by_date": matches_by_date,
        "tournament_stats_goals": tournament_stats_goals, "tournament_stats_cards": tournament_stats_cards,
        "active_disq": active_disq, "completed_disq": completed_disq, "tournament_news": tournament_news,
    })


@router.get("/participants/teams")
async def participants_teams_page(
        request: Request,
        season: str = "all",
        search: str = None,
        db: AsyncSession = Depends(get_db)
):
    # 1. Получаем сезоны
    seasons_result = await db.execute(select(Tournament.season).distinct().order_by(Tournament.season.desc()))
    available_seasons = [s[0] for s in seasons_result.all() if s[0]]

    # 2. Запрос матчей
    stmt = select(Match).where(Match.status == MatchStatus.FINISHED)
    if season != "all":
        stmt = stmt.join(Tournament).where(Tournament.season == season)
    all_matches = (await db.execute(stmt)).scalars().all()

    # 3. Считаем статистику
    team_stats = {}
    for m in all_matches:
        if m.home_team_id not in team_stats: team_stats[m.home_team_id] = {'games': 0, 'wins': 0, 'clean_sheets': 0,
                                                                           'goals': 0}
        if m.away_team_id not in team_stats: team_stats[m.away_team_id] = {'games': 0, 'wins': 0, 'clean_sheets': 0,
                                                                           'goals': 0}

        h = team_stats[m.home_team_id]
        a = team_stats[m.away_team_id]
        h['games'] += 1;
        a['games'] += 1
        h['goals'] += (m.home_score or 0);
        a['goals'] += (m.away_score or 0)
        if m.away_score == 0: h['clean_sheets'] += 1
        if m.home_score == 0: a['clean_sheets'] += 1
        if (m.home_score or 0) > (m.away_score or 0):
            h['wins'] += 1
        elif (m.away_score or 0) > (m.home_score or 0):
            a['wins'] += 1

    # 4. Получаем команды (с фильтром поиска)
    stmt_teams = select(Team)
    if search:
        stmt_teams = stmt_teams.where(Team.name.ilike(f"%{search}%"))
    all_teams = (await db.execute(stmt_teams)).scalars().all()

    teams_data = []
    for t in all_teams:
        if t.id in team_stats:
            stats = team_stats[t.id]
            teams_data.append({
                "team": t, "games": stats['games'], "wins": stats['wins'],
                "clean_sheets": stats['clean_sheets'], "goals": stats['goals']
            })
        elif not search:  # Если поиска нет, показываем всех (даже с нулями)
            teams_data.append({
                "team": t, "games": 0, "wins": 0, "clean_sheets": 0, "goals": 0
            })

    teams_data = sorted(teams_data, key=lambda x: x['games'], reverse=True)

    return await render_page(request, db, "participants.html", context={
        "teams_data": teams_data,
        "available_seasons": available_seasons,
        "current_season": season,
        "current_search": search or ""
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

    # 1. Подзапрос: Базовая статистика из TeamPlayer
    stats_subq = select(
        TeamPlayer.player_id,
        func.coalesce(func.sum(TeamPlayer.stats_games), 0).label('games'),
        func.coalesce(func.sum(TeamPlayer.stats_goals), 0).label('goals'),
        func.coalesce(func.sum(TeamPlayer.stats_assists), 0).label('assists'),
        func.coalesce(func.sum(TeamPlayer.stats_yellow_cards), 0).label('yc'),
        func.coalesce(func.sum(TeamPlayer.stats_red_cards), 0).label('rc')
    ).group_by(TeamPlayer.player_id).subquery()

    # 2. Подзапрос: Голы с пенальти из MatchEvent
    pen_subq = select(
        MatchEvent.player_id,
        func.count(MatchEvent.id).label('pen_goals')
    ).where(MatchEvent.event_type == MatchEventType.PENALTY_GOAL).group_by(MatchEvent.player_id).subquery()

    # 3. Считаем общее количество игроков для пагинации
    count_stmt = select(func.count()).select_from(Player)
    if search:
        count_stmt = count_stmt.where(
            or_(Player.last_name.ilike(f"%{search}%"), Player.first_name.ilike(f"%{search}%")))

    total_players = await db.scalar(count_stmt)
    total_pages = (total_players + limit - 1) // limit if total_players > 0 else 1

    # 4. Основной запрос с JOIN'ами
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

    # Фильтр поиска
    if search:
        stmt = stmt.where(or_(Player.last_name.ilike(f"%{search}%"), Player.first_name.ilike(f"%{search}%")))

    # Сортировка и пагинация
    stmt = stmt.order_by(func.coalesce(stats_subq.c.games, 0).desc()).offset(skip).limit(limit)

    results = (await db.execute(stmt)).all()

    # 5. Формируем список данных для шаблона
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

    # Получаем ВСЕ события игрока, чтобы посчитать статистику и собрать историю
    stmt_events = select(MatchEvent).where(
        MatchEvent.player_id == player_id
    ).options(
        selectinload(MatchEvent.match).selectinload(Match.home_team),
        selectinload(MatchEvent.match).selectinload(Match.away_team),
        selectinload(MatchEvent.match).selectinload(Match.tournament),
        selectinload(MatchEvent.team)
    ).order_by(desc(MatchEvent.match_id))

    all_events = (await db.execute(stmt_events)).scalars().all()

    # Общая статистика
    total_goals = sum(1 for e in all_events if e.event_type in [MatchEventType.GOAL, MatchEventType.PENALTY_GOAL])
    total_assists = sum(1 for e in all_events if e.event_type == MatchEventType.ASSIST)
    total_yellow = sum(1 for e in all_events if e.event_type == MatchEventType.YELLOW_CARD)
    total_red = sum(1 for e in all_events if e.event_type == MatchEventType.RED_CARD)

    # Группировка по турнирам
    tournaments_dict = {}
    for ev in all_events:
        if not ev.match or not ev.match.tournament: continue
        tid = ev.match.tournament.id

        if tid not in tournaments_dict:
            tournaments_dict[tid] = {
                "tournament": ev.match.tournament,
                "team": ev.team,
                "games": 0,
                "goals": 0,
                "assists": 0,
                "yc": 0,
                "rc": 0,
                "matches": {}
            }

        t_data = tournaments_dict[tid]
        mid = ev.match.id

        if mid not in t_data["matches"]:
            # Определяем результат для команды игрока
            is_home = ev.match.home_team_id == ev.team_id
            score_h = ev.match.home_score or 0
            score_a = ev.match.away_score or 0

            if is_home:
                res = "В" if score_h > score_a else ("П" if score_h < score_a else "Н")
                opp = ev.match.away_team.name
                score_str = f"{score_h} - {score_a}"
            else:
                res = "В" if score_a > score_h else ("П" if score_a < score_h else "Н")
                opp = ev.match.home_team.name
                score_str = f"{score_a} - {score_h}"

            t_data["matches"][mid] = {
                "match": ev.match,
                "opponent": opp,
                "result": res,
                "score": score_str,
                "is_home": is_home,
                "events": []
            }

        t_data["matches"][mid]["events"].append(ev)

        # Считаем стату турнира
        if ev.event_type in [MatchEventType.GOAL, MatchEventType.PENALTY_GOAL]:
            t_data["goals"] += 1
        elif ev.event_type == MatchEventType.ASSIST:
            t_data["assists"] += 1
        elif ev.event_type == MatchEventType.YELLOW_CARD:
            t_data["yc"] += 1
        elif ev.event_type == MatchEventType.RED_CARD:
            t_data["rc"] += 1

    # Преобразуем в список и считаем игры
    history_data = []
    total_games = 0
    for k, v in tournaments_dict.items():
        v["matches"] = list(v["matches"].values())
        v["matches"].sort(key=lambda x: x["match"].match_date.timestamp() if x["match"].match_date else 0, reverse=True)
        v["games"] = len(v["matches"])
        total_games += v["games"]
        history_data.append(v)

    # Сортируем турниры по дате последнего матча
    history_data.sort(
        key=lambda x: x["matches"][0]["match"].match_date.timestamp() if x["matches"][0]["match"].match_date else 0,
        reverse=True)

    return await render_page(request, db, "player_detail.html", context={
        "player": player,
        "history_data": history_data,
        "stats": {
            "games": total_games,
            "goals": total_goals,
            "assists": total_assists,
            "yellow_cards": total_yellow,
            "red_cards": total_red
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

    # Считаем статистику команд, к которым привязаны тренеры
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
    # Безопасно парсим ID (если строка пустая или не число, будет None)
    t_id = int(tournament_id) if tournament_id and tournament_id.isdigit() else None
    tm_id = int(team_id) if team_id and team_id.isdigit() else None

    limit = 10
    skip = (page - 1) * limit

    # Получаем списки для фильтров
    tournaments = (await db.execute(select(Tournament).order_by(Tournament.name))).scalars().all()
    teams = (await db.execute(select(Team).order_by(Team.name))).scalars().all()

    # Базовый запрос
    stmt = select(News).where(News.is_published == True)
    count_stmt = select(func.count()).select_from(News).where(News.is_published == True)

    # Применяем фильтры
    if t_id:
        stmt = stmt.where(News.tournament_id == t_id)
        count_stmt = count_stmt.where(News.tournament_id == t_id)

    if tm_id:
        stmt = stmt.join(News.teams).where(Team.id == tm_id)
        count_stmt = count_stmt.join(News.teams).where(Team.id == tm_id)

    # Загружаем связи и сортируем
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

    # 6. Последние 5 матчей для домашней команды (строго ДО текущего матча)
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

    # 7. Последние 5 матчей для гостевой команды (строго ДО текущего матча)
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

    # 8. Игроки, которые когда-либо играли за обе команды
    home_players_subq = select(TeamPlayer.player_id).where(TeamPlayer.team_id == match.home_team_id)
    away_players_subq = select(TeamPlayer.player_id).where(TeamPlayer.team_id == match.away_team_id)

    common_players_stmt = select(Player).where(
        Player.id.in_(home_players_subq),
        Player.id.in_(away_players_subq)
    ).order_by(Player.last_name)
    common_players = (await db.execute(common_players_stmt)).scalars().all()

    # 9. ГРУППИРОВКА СОБЫТИЙ ПО ИГРОКАМ
    events_by_player = {}
    if match.events:
        for e in match.events:
            if e.player_id:
                if e.player_id not in events_by_player:
                    events_by_player[e.player_id] = []
                events_by_player[e.player_id].append(e)

    # 10. РЕНДЕР СТРАНИЦЫ
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


# ==========================================
# ПУБЛИЧНАЯ СТРАНИЦА СОТРУДНИКОВ
# ==========================================
@router.get("/management")
async def management_page(request: Request, db: AsyncSession = Depends(get_db)):
    staff_list = (await db.execute(
        select(Staff).where(Staff.is_active == True).order_by(Staff.sort_order.asc(), Staff.id.asc())
    )).scalars().all()

    # Группируем по РАНГУ (а не по должности)
    grouped_staff = OrderedDict()
    for person in staff_list:
        if person.rank not in grouped_staff:
            grouped_staff[person.rank] = []
        grouped_staff[person.rank].append(person)

    return await render_page(request, db, "management.html", context={"grouped_staff": grouped_staff})


# ==========================================
# АДМИНКА: СПИСОК
# ==========================================
@router.get("/admin/staff")
async def admin_staff_list(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    staff_list = (await db.execute(
        select(Staff).order_by(Staff.sort_order.asc(), Staff.id.asc())
    )).scalars().all()

    # Собираем уникальные значения для подсказок
    all_ranks = sorted(list(set(s.rank for s in staff_list)))
    all_positions = sorted(list(set(s.position for s in staff_list)))

    return await render_page(request, db, "admin_staff.html", context={
        "user": user,
        "staff_list": staff_list,
        "all_ranks": all_ranks,
        "all_positions": all_positions
    })


# ==========================================
# АДМИНКА: СОЗДАНИЕ И РЕДАКТИРОВАНИЕ
# ==========================================
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
        db: AsyncSession = Depends(get_db),
):
    user = await crud_user.get_by_email(db, email=username)

    if not user or not verify_password(password, user.hashed_password) or user.role != UserRole.ADMIN:
        return RedirectResponse(url="/login?error=1", status_code=303)

    access_token = create_access_token(subject=user.email)
    response = RedirectResponse(url="/admin", status_code=303)

    # Определяем, HTTPS ли соединение (на localhost будет False, на проде — True)
    is_secure = request.url.scheme == "https"

    # Устанавливаем cookie с защитой от CSRF (samesite) и XSS (httponly)
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


@router.get("/admin")
async def admin_panel(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    from datetime import date
    from sqlalchemy import func
    from app.models.disqualification import Disqualification

    # 1. СБОР СТАТИСТИКИ (Дашборд)
    total_tournaments = await db.scalar(select(func.count()).select_from(Tournament))
    total_teams = await db.scalar(select(func.count()).select_from(Team))

    today = date.today()
    today_matches_count = await db.scalar(
        select(func.count()).select_from(Match).where(func.date(Match.match_date) == today)
    )

    active_disq_count = await db.scalar(
        select(func.count()).select_from(Disqualification).where(Disqualification.matches_remaining > 0)
    )

    # 2. МАТЧИ НА СЕГОДНЯ (ИЛИ БЛИЖАЙШИЕ)
    # Ищем матчи, которые еще не завершены и дата >= сегодня
    stmt_matches = select(Match).where(
        Match.status.in_([MatchStatus.SCHEDULED, MatchStatus.LIVE]),
        func.date(Match.match_date) >= today
    ).options(
        selectinload(Match.home_team),
        selectinload(Match.away_team),
        selectinload(Match.tournament)
    ).order_by(Match.match_date.asc()).limit(6)

    upcoming_matches = (await db.execute(stmt_matches)).scalars().all()

    # 3. АКТИВНЫЕ ТУРНИРЫ (Топ 5)
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

    # 1. СИНХРОНИЗАЦИЯ СТАТУСОВ (Обновляем флаги is_active)
    # Находим ID всех команд, которые играют в активных турнирах
    active_tt_stmt = select(TournamentTeam.team_id).join(Tournament).where(
        Tournament.status == TournamentStatus.ACTIVE
    )
    active_team_ids = (await db.execute(active_tt_stmt)).scalars().all()

    # Сбрасываем флаг у всех, затем ставим True только участникам активных турниров
    await db.execute(update(Team).values(is_active=False))
    if active_team_ids:
        await db.execute(update(Team).where(Team.id.in_(active_team_ids)).values(is_active=True))
    await db.commit()

    # 2. ЗАПРАШИВАЕМ КОМАНДЫ С УЧЕТОМ ФИЛЬТРА
    stmt = select(Team)

    if not show_all:
        # Если фильтр включен, берем только тех, у кого флаг стал True
        stmt = stmt.where(Team.is_active == True)

    stmt = stmt.order_by(Team.name)
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

    # 1. Матчи (домашние и гостевые)
    await db.execute(update(Match).where(Match.home_team_id == source_id).values(home_team_id=target_id))
    await db.execute(update(Match).where(Match.away_team_id == source_id).values(away_team_id=target_id))

    # 2. События (голы, ассисты, карточки)
    await db.execute(update(MatchEvent).where(MatchEvent.team_id == source_id).values(team_id=target_id))

    # 3. Дисквалификации
    await db.execute(
        update(Disqualification).where(Disqualification.team_id == source_id).values(team_id=target_id)
    )

    # 4. Заявки игроков — при дублях СУММИРУЕМ статистику, а не теряем
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

    # 5. Участие в турнирах (с защитой от дублей)
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

    # 6. Тренеры
    await db.execute(update(Coach).where(Coach.team_id == source_id).values(team_id=target_id))

    # 7. Капитаны (User.team_id)
    await db.execute(update(User).where(User.team_id == source_id).values(team_id=target_id))

    # 8. Привязка новостей (M2M news_teams)
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
            # у target уже есть эта новость — просто снимаем дубль
            await db.execute(news_teams.delete().where(
                (news_teams.c.news_id == news_id) & (news_teams.c.team_id == source_id)
            ))
        else:
            await db.execute(news_teams.update().where(
                (news_teams.c.news_id == news_id) & (news_teams.c.team_id == source_id)
            ).values(team_id=target_id))

    # 9. Удаляем старые строки таблицы (пересчитаются)
    await db.execute(delete(TournamentStanding).where(TournamentStanding.team_id == source_id))

    # 10. Удаляем саму команду-источник
    await db.execute(delete(Team).where(Team.id == source_id))

    await db.commit()

    # 11. Пересчёт таблиц во всех затронутых турнирах
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
    # 1. Базовый запрос с ЖАДНОЙ ЗАГРУЗКОЙ матчей
    from sqlalchemy.orm import selectinload
    stmt = select(Tournament).options(
        selectinload(Tournament.matches).selectinload(Match.home_team),
        selectinload(Tournament.matches).selectinload(Match.away_team)
    )

    if search:
        stmt = stmt.where(Tournament.name.ilike(f"%{search}%"))
    if season:
        stmt = stmt.where(Tournament.season == season)

    # Получаем данные
    tournaments = (await db.execute(stmt)).unique().scalars().all()

    # 2. ЖЕЛЕЗОБЕТОННАЯ СОРТИРОВКА В PYTHON
    # Сортируем: Сначала по сезону (от новых к старым), затем по sort_order, затем по имени
    def sort_key(t):
        # Пытаемся превратить год в число для правильной сортировки
        try:
            season_num = int(t.season)
        except (ValueError, TypeError):
            season_num = 0

        # Возвращаем кортеж (минус для обратной сортировки года)
        return (-season_num, t.sort_order, t.name)

    tournaments = sorted(tournaments, key=sort_key)

    # Сортируем матчи внутри каждого турнира ОТ НОВЫХ К СТАРЫМ
    from datetime import datetime, timezone
    min_date = datetime.min.replace(tzinfo=timezone.utc)

    for t in tournaments:
        # Сначала по дате (новые сверху), затем по туру (по убыванию), затем по ID
        t.matches.sort(
            key=lambda m: (
                m.match_date or min_date,
                m.round or 0,
                m.id
            ),
            reverse=True
        )

    # 4. Получаем список сезонов
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

    # Получаем список уже существующих заголовков для подсказки
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
        tournament_format: str = Form(None), # Принимаем строкой, чтобы избежать 422
        sort_order: int = Form(0),
        logo: UploadFile = File(None),
        db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    logo_url = await save_upload_file(logo, "tournaments")
    start_date = datetime.fromisoformat(start_at) if start_at else None
    end_date = datetime.fromisoformat(end_at) if end_at else None

    # Конвертируем строку в Enum (на случай если пришло маленькими буквами)
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
        tournament_format=fmt, sort_order=sort_order
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

    # Получаем список уже существующих заголовков для подсказки
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
        logo: UploadFile = File(None),
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
        tournament_format=fmt, sort_order=sort_order
    )
    await crud_tournament.update(db, db_obj=tournament, obj_in=tournament_in)

    # =========================================================================
    # ЛОГИКА ДЛЯ НОВОСТЕЙ О ЧЕМПИОНЕ (ОСТАВЛЯЕМ КАК БЫЛО)
    # =========================================================================
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
                if not slug: slug = f"champion-{tournament_id}-{int(datetime.now().timestamp())}"

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


@router.get("/admin/matches")
async def admin_matches_list(
        request: Request,
        search: str = None,
        tournament_id: str = None,
        db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    from sqlalchemy import case, or_
    from datetime import datetime, timezone

    # Получаем текущую дату и время для сравнения
    now = datetime.now(timezone.utc)

    # 1. Базовый запрос
    stmt = select(Match).join(Tournament).options(
        selectinload(Match.home_team),
        selectinload(Match.away_team),
        selectinload(Match.tournament)
    )

    # 2. Применяем фильтры
    if search:
        stmt = stmt.where(
            or_(
                Match.home_team.has(Team.name.ilike(f"%{search}%")),
                Match.away_team.has(Team.name.ilike(f"%{search}%"))
            )
        )

    if tournament_id and tournament_id.strip().isdigit():
        stmt = stmt.where(Match.tournament_id == int(tournament_id))

    # 3. УМНАЯ СОРТИРОВКА ПО ДАТЕ
    # Группируем матчи: 0 - будущие (дата >= сейчас), 1 - прошлые (дата < сейчас), 2 - без даты (TBA)
    time_group = case(
        (Match.match_date >= now, 0),
        (Match.match_date < now, 1),
        else_=2
    )

    stmt = stmt.order_by(
        # 1. Сначала все БУДУЩИЕ матчи (0), потом ПРОШЛЫЕ (1)
        time_group,

        # 2. Внутри будущих: от ближайшего к дальнему (ASC)
        case((Match.match_date >= now, Match.match_date), else_=None).asc().nullslast(),

        # 3. Внутри прошлых: от недавних к старым (DESC)
        case((Match.match_date < now, Match.match_date), else_=None).desc().nullslast(),

        Match.id.desc()
    )

    matches = (await db.execute(stmt)).unique().scalars().all()
    tournaments = (await db.execute(select(Tournament).order_by(Tournament.name))).scalars().all()

    return await render_page(request, db, "admin_matches.html", context={
        "user": user,
        "matches": matches,
        "tournaments": tournaments,
        "current_search": search or "",
        "current_tournament_id": tournament_id or ""
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
        stage: str = Form(None), stage_name: str = Form(None), # <--- ДОБАВИЛИ
        db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    if home_team_id == away_team_id:
        raise HTTPException(status_code=400, detail="Команда не может играть сама с собой")

    parsed_date = datetime.fromisoformat(match_date) if match_date else None

    # Парсим стадию
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
        group_name=group_name, stage=match_stage, stage_name=stage_name # <--- ПЕРЕДАЕМ
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

    # 1. Получаем матч
    match = await crud_match.get(db, match_id)
    if not match:
        return RedirectResponse(url="/admin/matches", status_code=303)

    if home_team_id == away_team_id:
        raise HTTPException(status_code=400, detail="Команда не может играть сама с собой")

    # 2. Запоминаем старый статус (чтобы понять, изменился ли он)
    old_status = match.status

    # 3. Парсим данные из формы
    parsed_date = datetime.fromisoformat(match_date) if match_date else None

    match_stage = None
    if stage:
        from app.models.enums import MatchStage
        try:
            match_stage = MatchStage(stage)
        except ValueError:
            pass

    # 4. Обновляем данные матча в БД
    match_in = MatchUpdate(
        tournament_id=tournament_id, home_team_id=home_team_id, away_team_id=away_team_id,
        match_date=parsed_date, stadium=stadium, home_score=home_score, away_score=away_score,
        home_penalty=home_penalty, away_penalty=away_penalty,
        stage=match_stage, stage_name=stage_name, tech_loss=tech_loss,
        status=status, round=round, group_name=group_name
    )
    await crud_match.update(db, db_obj=match, obj_in=match_in)

    # =========================================================================
    # 5. УМНАЯ ЛОГИКА ПЕРЕСЧЕТА
    # =========================================================================
    from app.services.stats_recalc import StatsRecalculator
    from app.services.disqualifications import DisqualificationService

    # Проверяем, есть ли в базе счет (быстрое добавление) ИЛИ админ сам сменил статус на Завершен
    is_finished_now = (status == MatchStatus.FINISHED) or (home_score is not None and away_score is not None)

    if is_finished_now:
        # Если счет ввели, но статус забыли сменить — принудительно ставим FINISHED
        if match.status != MatchStatus.FINISHED:
            match.status = MatchStatus.FINISHED
            db.add(match)
            await db.commit()
            await db.refresh(match)

        # Если матч ТОЛЬКО ЧТО перешел в статус "Завершен" (т.е. раньше был SCHEDULED)
        if old_status != MatchStatus.FINISHED:
            # А) Списываем отбытые матчи у текущих дисквалификаций
            await DisqualificationService.process_match_finished(db, match)
            # Б) Автоматически выдаем новые за красные или желтые
            await DisqualificationService.auto_suspend_by_cards(db, match)

        # В) В любом случае пересчитываем всю таблицу и статистику
        # (это нужно, если админ зашел и просто исправил счет уже завершенного матча)
        await StatsRecalculator.recalculate_all(db, tournament_id)

    # Если матч был завершен, а его вернули в статус "Запланирован" (убрали счет)
    elif old_status == MatchStatus.FINISHED and not is_finished_now:
        # Пересчитываем таблицу, чтобы отнять очки у команд
        await StatsRecalculator.recalculate_all(db, tournament_id)

    return RedirectResponse(url="/admin/matches", status_code=303)


@router.post("/admin/matches/{match_id}/delete")
async def admin_match_delete(match_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    match = await crud_match.get(db, match_id)
    if match: await crud_match.delete(db, db_obj=match)

    return RedirectResponse(url="/admin/matches", status_code=303)


@router.get("/admin/players")
async def admin_players_list(request: Request, search: str = None, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    # Базовый запрос с сортировкой по фамилии
    stmt = select(Player).order_by(Player.last_name, Player.first_name)

    # Если ввели поиск — фильтруем по фамилии ИЛИ имени
    if search:
        stmt = stmt.where(
            or_(
                Player.last_name.ilike(f"%{search}%"),
                Player.first_name.ilike(f"%{search}%")
            )
        )

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
            # Автогол: игрок одной команды забивает в свои ворота
            if e.team_id == match.home_team_id:
                away_goals += 1
            elif e.team_id == match.away_team_id:
                home_goals += 1

    match.home_score = home_goals
    match.away_score = away_goals
    db.add(match)
    await db.commit()

    # ВАЖНО: Пересчитываем таблицу после каждого изменения счета
    await StandingsService.recalculate_standings(db, match.tournament_id)


@router.get("/admin/matches/{match_id}/events")
async def admin_match_events(match_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    # Явно подгружаем матч со всеми связями, включая турнир
    stmt = select(Match).where(Match.id == match_id).options(
        selectinload(Match.home_team),
        selectinload(Match.away_team),
        selectinload(Match.tournament)
    )
    match = (await db.execute(stmt)).scalar_one_or_none()

    if not match:
        return RedirectResponse(url="/admin/matches", status_code=303)

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
        event_type: MatchEventType = Form(...), minute: int = Form(...),
        assist_player_id: int = Form(None),  # <--- ДОБАВИЛИ АССИСТА
        comment: str = Form(None), db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    match = await db.get(Match, match_id)
    if not match: return RedirectResponse(url="/admin/matches", status_code=303)

    # 1. Добавляем основное событие (Гол, Карточка)
    event_in = MatchEventCreate(
        match_id=match_id, team_id=team_id, player_id=player_id,
        event_type=event_type, minute=minute, comment=comment
    )
    await crud_match_event.create(db, obj_in=event_in)

    # 2. Если это гол и выбран ассистент — добавляем событие ассиста
    if event_type in [MatchEventType.GOAL, MatchEventType.PENALTY_GOAL] and assist_player_id:
        assist_in = MatchEventCreate(
            match_id=match_id, team_id=team_id, player_id=assist_player_id,
            event_type=MatchEventType.ASSIST, minute=minute
        )
        await crud_match_event.create(db, obj_in=assist_in)

    # 3. Пересчитываем счет матча
    await recalculate_match_score(match, db)

    # 4. ГЛАВНОЕ: Запускаем полный пересчет всей статистики
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

            # ДОБАВИТЬ ВОТ ЭТО:
            from app.services.stats_recalc import StatsRecalculator
            await StatsRecalculator.recalculate_all(db, match.tournament_id)

    return RedirectResponse(url=f"/admin/matches/{match_id}/events", status_code=303)


@router.get("/admin/news")
async def admin_news_list(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    # Сортируем по sort_order, а затем по дате
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
            # --- СБОР ГОЛОВ И ПЕРЕДАЧ ---
            if e.player_id:
                if e.player_id not in events_data:
                    events_data[e.player_id] = {'goals': [], 'assists': []}

                if e.event_type in [MatchEventType.GOAL, MatchEventType.PENALTY_GOAL]:
                    events_data[e.player_id]['goals'].append(e.minute)
                elif e.event_type == MatchEventType.ASSIST:
                    events_data[e.player_id]['assists'].append(e.minute)

            # --- СБОР АВТОГОЛОВ ---
            if e.event_type == MatchEventType.OWN_GOAL and e.player:
                own_goals_data.append({
                    "minute": e.minute,
                    "player_name": f"{e.player.last_name} {e.player.first_name}",
                    "team_name": e.team.name if e.team else "Неизвестно"
                })

            # --- СБОР КАРТОЧЕК ---
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


from sqlalchemy import select, func, or_, and_, case
from sqlalchemy.orm import selectinload
from datetime import datetime


@router.get("/teams/{team_id}")
async def team_page(team_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    team = await db.get(Team, team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Команда не найдена")

    # 1. Новости команды
    stmt_news = select(News).where(
        News.is_published == True,
        News.teams.any(id=team.id)
    ).order_by(News.published_at.desc()).limit(6)
    team_news = (await db.execute(stmt_news)).scalars().all()

    # 2. Все матчи команды
    stmt = select(Match).where(
        or_(Match.home_team_id == team.id, Match.away_team_id == team.id)
    ).options(
        selectinload(Match.home_team),
        selectinload(Match.away_team),
        selectinload(Match.tournament)
    ).order_by(Match.match_date.desc())
    all_matches = (await db.execute(stmt)).scalars().all()
    all_matches = sorted(all_matches, key=lambda x: x.match_date.timestamp() if x.match_date else 0, reverse=True)

    # 3. Статистика
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

    # 4. Группируем матчи по турнирам
    grouped_matches = {}
    for m in all_matches:
        t_name = m.tournament.name if m.tournament else "Товарищеские матчи"
        if t_name not in grouped_matches:
            grouped_matches[t_name] = []
        grouped_matches[t_name].append(m)

    # 5. Текущий турнир и таблица
    active_tournament = next((m.tournament for m in all_matches if m.tournament), None)
    standings = []
    if active_tournament:
        stmt_standings = select(TournamentStanding).where(
            TournamentStanding.tournament_id == active_tournament.id
        ).options(selectinload(TournamentStanding.team)).order_by(
            TournamentStanding.points.desc()
        ).limit(5)
        standings = (await db.execute(stmt_standings)).scalars().all()

    # 6. ПОЛУЧАЕМ СОСТАВ (РОСТЕР)
    stmt_roster = select(TeamPlayer).where(
        TeamPlayer.team_id == team.id
    ).options(selectinload(TeamPlayer.player)).order_by(TeamPlayer.jersey_number)

    roster_raw = (await db.execute(stmt_roster)).scalars().all()

    # Убираем дубликаты, если игрок заявлен за команду в разных турнирах
    roster = []
    seen_players = set()
    for tp in roster_raw:
        if tp.player_id not in seen_players:
            seen_players.add(tp.player_id)
            roster.append(tp)

    # Считаем возраст для каждого игрока
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

    # --- НОВОЕ: КРАСИВАЯ СОРТИРОВКА ---
    # Задаем вес для каждой позиции (кто первый в списке)
    position_order = {
        PlayerPosition.GOALKEEPER: 1,  # Вратари
        PlayerPosition.DEFENDER: 2,    # Защитники
        PlayerPosition.MIDFIELDER: 3,  # Полузащитники
        PlayerPosition.FORWARD: 4,     # Нападающие
        None: 5                        # Без позиции
    }

    def sort_roster(tp):
        # Сначала по позиции, потом по номеру (если номера нет - ставим 999), потом по фамилии
        pos = position_order.get(tp.player.position, 5)
        num = tp.jersey_number if tp.jersey_number is not None else 999
        name = tp.player.last_name or ""
        return (pos, num, name)

    # Применяем сортировку
    active_players.sort(key=sort_roster)
    released_players.sort(key=sort_roster)
    # ----------------------------------

    # 4. Считаем средний возраст (только для активных)
    today = date.today()
    ages = []
    for tp in active_players:
        if tp.player.date_of_birth:
            age = today.year - tp.player.date_of_birth.year - (
                        (today.month, today.day) < (tp.player.date_of_birth.month, tp.player.date_of_birth.day))
            ages.append(age)

    avg_age = round(sum(ages) / len(ages), 2) if ages else 0

    # 5. Формируем словарь статистики (для передачи в шаблон, если понадобится)
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

    # Берем топ-N команд по очкам и разнице мячей
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

    # Определяем раунд
    if num_teams == 16: round_num = 1; stage_n = "1/8 Финала"; stage_t = MatchStage.PLAYOFF
    elif num_teams == 8: round_num = 2; stage_n = "1/4 Финала"; stage_t = MatchStage.PLAYOFF
    elif num_teams == 4: round_num = 3; stage_n = "1/2 Финала"; stage_t = MatchStage.PLAYOFF
    else: round_num = 4; stage_n = "Финал"; stage_t = MatchStage.FINAL

    # Создаем пары: 1-й с последним (8-м), 2-й с 7-м и т.д.
    for i in range(num_teams // 2):
        home_team = top_teams[i].team_id
        away_team = top_teams[num_teams - 1 - i].team_id

        # Проверяем, нет ли уже такого матча
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

    # Все команды
    all_teams = (await db.execute(select(Team).order_by(Team.name))).scalars().all()

    # Команды в турнире
    stmt = select(TournamentTeam).where(TournamentTeam.tournament_id == tournament_id).options(
        selectinload(TournamentTeam.team))
    tournament_teams = (await db.execute(stmt)).scalars().all()

    added_team_ids = [tt.team_id for tt in tournament_teams]

    # Группируем команды для шаблона
    grouped_teams = {}
    for tt in tournament_teams:
        group_name = tt.group_name if tt.group_name else "Общая таблица"
        if group_name not in grouped_teams:
            grouped_teams[group_name] = []
        grouped_teams[group_name].append(tt)

    # Сортируем группы по алфавиту, но "Общая таблица" пусть будет в конце (если есть)
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

    # Удаляем связь с турниром
    stmt = select(TournamentTeam).where(TournamentTeam.tournament_id == tournament_id,
                                        TournamentTeam.team_id == team_id)
    tt = (await db.execute(stmt)).scalar_one_or_none()
    if tt:
        await db.delete(tt)

    # Удаляем из таблицы
    stmt_s = select(TournamentStanding).where(TournamentStanding.tournament_id == tournament_id,
                                              TournamentStanding.team_id == team_id)
    ts = (await db.execute(stmt_s)).scalar_one_or_none()
    if ts:
        await db.delete(ts)

    await db.commit()
    return RedirectResponse(url=f"/admin/tournaments/{tournament_id}/teams", status_code=303)


# Ручной пересчет таблицы (кнопка "Пересчитать")
@router.post("/admin/tournaments/{tournament_id}/recalculate")
async def admin_tournament_recalculate(tournament_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    # Импортируем наш сервис, который мы написали ранее
    from app.services.standings import StandingsService
    await StandingsService.recalculate_standings(db, tournament_id)

    return RedirectResponse(url=f"/admin/tournaments/{tournament_id}/edit", status_code=303)


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

    # Обновляем данные события
    event.team_id = team_id
    event.player_id = player_id
    event.event_type = event_type
    event.minute = minute
    event.comment = comment
    db.add(event)
    await db.commit()

    # Пересчитываем счет матча (голы могли измениться)
    match = await db.get(Match, match_id)
    await recalculate_match_score(match, db)

    # Полный пересчет всей статистики турнира
    from app.services.stats_recalc import StatsRecalculator
    await StatsRecalculator.recalculate_all(db, match.tournament_id)

    return RedirectResponse(url=f"/admin/matches/{match_id}/events", status_code=303)


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

    # Проверяем, есть ли уже
    existing = await db.scalar(select(TournamentTeam).where(
        TournamentTeam.tournament_id == tournament_id, TournamentTeam.team_id == team_id
    ))
    if not existing:
        # Добавляем в турнир с указанной группой
        db.add(TournamentTeam(tournament_id=tournament_id, team_id=team_id, group_name=group_name))

        # Создаем пустую строку в таблице для этой группы
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

    # Меняем группу в заявке
    tt = await db.scalar(select(TournamentTeam).where(
        TournamentTeam.tournament_id == tournament_id, TournamentTeam.team_id == team_id
    ))
    if tt:
        tt.group_name = new_group_name
        db.add(tt)

    # Меняем группу в таблице
    ts = await db.scalar(select(TournamentStanding).where(
        TournamentStanding.tournament_id == tournament_id, TournamentStanding.team_id == team_id
    ))
    if ts:
        ts.group_name = new_group_name
        db.add(ts)

    await db.commit()
    return RedirectResponse(url=f"/admin/tournaments/{tournament_id}/teams", status_code=303)


@router.get("/admin/disqualifications")
async def admin_disqualifications_list(
        request: Request,
        tournament_id: int = None,
        search: str = None,
        db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    # Базовый запрос
    stmt = select(Disqualification).options(
        selectinload(Disqualification.player),
        selectinload(Disqualification.team),
        selectinload(Disqualification.tournament)
    ).order_by(Disqualification.created_at.desc())

    # Фильтр по турниру
    if tournament_id:
        stmt = stmt.where(Disqualification.tournament_id == tournament_id)

    # Фильтр по фамилии или имени игрока
    if search:
        stmt = stmt.join(Player).where(
            or_(
                Player.last_name.ilike(f"%{search}%"),
                Player.first_name.ilike(f"%{search}%")
            )
        )

    disqualifications = (await db.execute(stmt)).scalars().all()
    tournaments = (await db.execute(select(Tournament).order_by(Tournament.name))).scalars().all()

    # Разделяем на активные и завершенные
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


# 2. Форма создания дисквалификации
@router.get("/admin/disqualifications/create")
async def admin_disqualification_create_form(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    # Получаем данные для селектов
    tournaments = (await db.execute(select(Tournament).order_by(Tournament.name))).scalars().all()
    teams = (await db.execute(select(Team).order_by(Team.name))).scalars().all()

    # Игроков может быть очень много, лучше подгружать их через AJAX по выбранной команде,
    # но для простоты выведем всех (если база большая, лучше оптимизировать)
    players = (await db.execute(select(Player).order_by(Player.last_name))).scalars().all()

    return await render_page(request, db, "admin_disqualification_form.html", context={
        "user": user,
        "tournaments": tournaments,
        "teams": teams,
        "players": players
    })


# 3. Обработка создания
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


# 4. Удаление (если ошиблись)
@router.post("/admin/disqualifications/{disq_id}/delete")
async def admin_disqualification_delete(disq_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    disq = await db.get(Disqualification, disq_id)
    if disq:
        await db.delete(disq)
        await db.commit()

    return RedirectResponse(url="/admin/disqualifications", status_code=303)

@router.post("/admin/matches/{match_id}/finish")
async def admin_match_finish(
        match_id: int, request: Request,
        home_penalty: int = Form(None), away_penalty: int = Form(None),
        tech_loss_team_id: int = Form(None),  # 0 = нет ТП, иначе ID команды
        db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    match = await db.get(Match, match_id)
    if not match: return RedirectResponse(url="/admin/matches", status_code=303)

    match.status = MatchStatus.FINISHED
    match.home_penalty = home_penalty
    match.away_penalty = away_penalty

    if tech_loss_team_id and tech_loss_team_id > 0:
        match.tech_loss = True
        if tech_loss_team_id == match.home_team_id:
            match.home_score = 0;
            match.away_score = 3
        else:
            match.home_score = 3;
            match.away_score = 0

    await db.commit()

    # Полный пересчет
    await StatsRecalculator.recalculate_all(db, match.tournament_id)

    return RedirectResponse(url="/admin/matches", status_code=303)


@router.get("/participants/hall-of-fame")
async def participants_hall_page(request: Request, db: AsyncSession = Depends(get_db)):
    # ==================== 1. СБОР СТАТИСТИКИ КОМАНД ====================
    stmt_matches = select(Match).where(Match.status == MatchStatus.FINISHED)
    all_matches = (await db.execute(stmt_matches)).scalars().all()

    team_stats = {}  # {team_id: {'games': 0, 'wins': 0, 'goals': 0, 'clean_sheets': 0}}

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

    # ==================== 2. СБОР СТАТИСТИКИ ИГРОКОВ ====================
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

    players_list = list(player_stats.values())

    top_players_games = sorted(players_list, key=lambda x: x['games'], reverse=True)[:5]
    top_players_ga = sorted(players_list, key=lambda x: (x['goals'] + x['assists']), reverse=True)[:5]
    top_players_goals = sorted(players_list, key=lambda x: x['goals'], reverse=True)[:5]
    top_players_assists = sorted(players_list, key=lambda x: x['assists'], reverse=True)[:5]

    return await render_page(request, db, "hall_of_fame.html", context={
        "teams_games": top_teams_games,
        "teams_wins": top_teams_wins,
        "teams_cs": top_teams_cs,
        "teams_goals": top_teams_goals,
        "players_games": top_players_games,
        "players_ga": top_players_ga,
        "players_goals": top_players_goals,
        "players_assists": top_players_assists,
    })


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


# 1. Страница с перетаскиванием
@router.get("/admin/tournaments/reorder")
async def admin_tournaments_reorder(
        request: Request,
        season: str = None,
        db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    # Получаем все сезоны
    seasons_result = await db.execute(select(Tournament.season).distinct().order_by(Tournament.season.desc()))
    available_seasons = [s[0] for s in seasons_result.all() if s[0]]

    # Если сезон не выбран, берем последний
    if not season and available_seasons:
        season = available_seasons[0]

    # Запрашиваем турниры за выбранный сезон, сортируя по sort_order
    stmt = select(Tournament).where(Tournament.season == season).order_by(Tournament.sort_order.asc(),
                                                                          Tournament.name.asc())
    tournaments = (await db.execute(stmt)).scalars().all()

    return await render_page(request, db, "admin_tournament_reorder.html", context={
        "user": user,
        "tournaments": tournaments,
        "available_seasons": available_seasons,
        "current_season": season
    })


# 2. Сохранение нового порядка
@router.post("/admin/tournaments/reorder/save")
async def admin_tournaments_reorder_save(
        request: Request,
        db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    try:
        data = await request.json()

        # Обновляем sort_order для каждого турнира
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
        # Принимаем данные из формы
        allow_draws: bool = Form(False),
        points_win: int = Form(3), points_draw: int = Form(1), points_loss: int = Form(0),
        periods_count: int = Form(2), period_duration: int = Form(45),
        auto_disqualifications: bool = Form(False),
        yellow_cards_limit: int = Form(4), yellow_suspension: int = Form(1), red_suspension: int = Form(1),
        tech_loss_goals: bool = Form(False),
        max_starting_players: int = Form(11), max_bench_players: int = Form(5),
        tie_breakers: str = Form("[]"), # Принимаем JSON строку
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

    # Обновляем поля
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

    db.add(tournament)
    await db.commit()

    return RedirectResponse(url=f"/admin/tournaments/{tournament_id}/edit?msg=regulations_saved", status_code=303)


# =========================================================================
# КОНСТРУКТОР СТРУКТУРЫ ТУРНИРА (ТАБЛИЦЫ И СЕТКИ)
# =========================================================================

# =========================================================================
# КОНСТРУКТОР СТРУКТУРЫ ТУРНИРА (ТАБЛИЦЫ И СЕТКИ)
# =========================================================================

# 1. Отображение страницы
@router.get("/admin/tournaments/{tournament_id}/structure")
async def admin_tournament_structure(
        tournament_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    tournament = await db.get(Tournament, tournament_id)
    if not tournament:
        return RedirectResponse(url="/admin/tournaments", status_code=303)

    # Получаем все заявки команд на этот турнир
    stmt_tt = select(TournamentTeam).where(TournamentTeam.tournament_id == tournament_id).options(
        selectinload(TournamentTeam.team)
    )
    tournament_teams = (await db.execute(stmt_tt)).scalars().all()

    # Берем сохраненные пустые группы из настроек турнира (или пустой список)
    saved_groups = tournament.custom_groups or []

    # Создаем словарь со всеми группами (даже пустыми)
    groups = {g: [] for g in saved_groups}
    unassigned_teams = []

    # Распределяем команды
    for tt in tournament_teams:
        if not tt.group_name:
            unassigned_teams.append(tt)
        else:
            if tt.group_name not in groups:
                groups[tt.group_name] = []  # Если группы нет в сохраненных, создаем
            groups[tt.group_name].append(tt)

    # Получаем названия сеток (чтобы вывести их внизу)
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


# 2. Создание новой (пустой) группы
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


# 3. СОХРАНЕНИЕ ВСЕЙ СТРУКТУРЫ (Вызывается по кнопке)
@router.post("/admin/tournaments/{tournament_id}/structure/save")
async def admin_structure_save(
        tournament_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    # Получаем данные из формы
    form_data = await request.form()

    for key, value in form_data.items():
        if key.startswith("team_group_"):
            # Извлекаем ID команды из названия поля (team_group_123 -> 123)
            team_id = int(key.replace("team_group_", ""))

            # Если перетащили в "Не распределены", ставим None
            group_name = value if value and value != "unassigned" else None

            # 1. Обновляем в заявке турнира
            stmt_tt = select(TournamentTeam).where(
                TournamentTeam.tournament_id == tournament_id,
                TournamentTeam.team_id == team_id
            )
            tt = (await db.execute(stmt_tt)).scalar_one_or_none()
            if tt:
                tt.group_name = group_name
                db.add(tt)

            # 2. Обновляем в турнирной таблице
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


# 4. Создание сетки плей-офф
@router.post("/admin/tournaments/{tournament_id}/structure/create-bracket")
async def admin_create_bracket(
        tournament_id: int, request: Request, bracket_name: str = Form(...), db: AsyncSession = Depends(get_db)
):
    user = await get_admin_from_cookie(request, db)
    if isinstance(user, RedirectResponse): return user

    # Создаем один матч-заглушку, чтобы сетка появилась в списке
    # Админ потом отредактирует команды в разделе "Матчи"
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