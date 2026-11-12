import asyncio
import random
import re
from datetime import datetime
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from sqlalchemy import select, delete
from sqlalchemy.orm import selectinload

from app.db.session import session_factory
from app.models.tournament import Tournament
from app.models.team import Team
from app.models.player import Player
from app.models.match import Match
from app.models.match_event import MatchEvent
from app.models.tournament_standing import TournamentStanding
from app.models.tournament_team import TournamentTeam
from app.models.enums import MatchStatus, MatchEventType

SEASONS = {"2026": "10129829"}
BASE_URL = "https://jliga.ru"
PAGES_POOL_SIZE = 3 # Количество параллельных вкладок
BLOCKED_TYPES = {"image", "media", "font", "stylesheet"}

RU_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12
}


async def setup_page(page):
    async def handle_route(route):
        if route.request.resource_type in BLOCKED_TYPES:
            await route.abort()
        else:
            await route.continue_()

    await page.route("**/*", handle_route)


async def fetch_html(page, url: str) -> str | None:
    try:
        # Уменьшили задержку, так как работаем в несколько потоков
        await asyncio.sleep(random.uniform(0.2, 0.5))
        response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        if response and response.status == 200:
            try:
                await page.wait_for_selector(".blk-tournamentheader, .blk-matchheader", timeout=4000)
            except:
                pass
            return await page.content()
        return None
    except Exception:
        return None


def parse_date(date_str: str, year: str) -> datetime | None:
    if not date_str: return None
    try:
        # Фикс пробелов
        clean_str = re.sub(r'\s+', ' ', date_str).strip()
        parts = clean_str.split()

        day = int(parts[0])
        month = RU_MONTHS.get(parts[1].lower())

        # Ищем время в строке
        time_match = re.search(r'(\d{1,2}):(\d{2})', clean_str)
        if not time_match: return None

        hour, minute = int(time_match.group(1)), int(time_match.group(2))
        return datetime(year=int(year), month=month, day=day, hour=hour, minute=minute)
    except Exception:
        return None


async def sync_tournament_table(page, tournament_id: int):
    # Открываем сессию только для этой функции
    async with session_factory() as db:
        try:
            t = await db.scalar(select(Tournament).where(Tournament.jliga_id == tournament_id))
            if not t: return

            print(f"\n📊 Обновление таблицы для: {t.name}")
            html = await fetch_html(page, f"{BASE_URL}/tournament/{tournament_id}/tables")
            if not html: return

            soup = BeautifulSoup(html, "html.parser")
            scraped_teams = {}

            for row in soup.find_all("tr"):
                cols = row.find_all("td")
                if len(cols) < 8: continue
                try:
                    team_link = row.find("a", href=re.compile(r"team_id=\d+"))
                    if not team_link: continue
                    team_jliga_id = int(team_link["href"].split("team_id=")[-1])
                    team_name = team_link.text.strip()
                    team_col_idx = cols.index(team_link.parent) if team_link.parent in cols else 1

                    def safe_int(idx):
                        if idx < len(cols):
                            v = cols[idx].text.strip()
                            return int(v) if v.isdigit() else 0
                        return 0

                    scraped_teams[team_jliga_id] = {
                        "name": team_name, "games": safe_int(team_col_idx + 1),
                        "wins": safe_int(team_col_idx + 2), "draws": safe_int(team_col_idx + 3),
                        "losses": safe_int(team_col_idx + 4), "goals_for": safe_int(team_col_idx + 5),
                        "goals_against": safe_int(team_col_idx + 6), "points": safe_int(team_col_idx + 7),
                    }
                except Exception:
                    continue

            for t_jliga_id, stats in scraped_teams.items():
                team = await db.scalar(select(Team).where(Team.jliga_id == t_jliga_id))
                if not team:
                    team = Team(jliga_id=t_jliga_id, name=stats["name"], slug=f"team-{t_jliga_id}")
                    db.add(team)
                    await db.flush()

                tt = await db.scalar(select(TournamentTeam).where(TournamentTeam.tournament_id == t.id,
                                                                  TournamentTeam.team_id == team.id))
                if not tt:
                    db.add(TournamentTeam(tournament_id=t.id, team_id=team.id))

                standing = await db.scalar(select(TournamentStanding).where(TournamentStanding.tournament_id == t.id,
                                                                            TournamentStanding.team_id == team.id))
                if not standing:
                    standing = TournamentStanding(tournament_id=t.id, team_id=team.id)
                    db.add(standing)

                standing.games_played = stats["games"]
                standing.wins = stats["wins"]
                standing.draws = stats["draws"]
                standing.losses = stats["losses"]
                standing.goals_for = stats["goals_for"]
                standing.goals_against = stats["goals_against"]
                standing.points = stats["points"]

            await db.commit()
            print(f"  ✅ Таблица обновлена для {t.name}")
        except Exception as e:
            print(f"  ❌ Ошибка в таблице {tournament_id}: {e}")
            await db.rollback()


async def sync_match(page, match_url: str, year: str, tournament_id: int):
    match_id_str = match_url.split("/")[-1]
    if not match_id_str.isdigit(): return
    match_jliga_id = int(match_id_str)

    # Открываем независимую сессию для параллельного потока
    async with session_factory() as db:
        try:
            html = await fetch_html(page, f"{BASE_URL}/match/{match_jliga_id}?active=events")
            if not html: return

            soup = BeautifulSoup(html, "html.parser")

            team_links = soup.select(".blk-matchheader__logo")
            if len(team_links) < 2: return
            home_team_jliga_id = int(team_links[0]["href"].split("team_id=")[-1])
            away_team_jliga_id = int(team_links[1]["href"].split("team_id=")[-1])

            home_team = await db.scalar(select(Team).where(Team.jliga_id == home_team_jliga_id))
            away_team = await db.scalar(select(Team).where(Team.jliga_id == away_team_jliga_id))
            if not home_team or not away_team: return

            date_el = soup.select_one(".blk-matchheader__date")
            time_el = soup.select_one(".blk-matchheader__time")
            stadium_el = soup.select_one(".blk-matchheader__stadium")
            round_el = soup.select_one(".blk-matchheader__tour")

            date_str = f"{date_el.text.strip()} {time_el.text.strip()}" if date_el and time_el else ""
            match_date = parse_date(date_str, year)
            stadium = stadium_el.text.strip() if stadium_el else None

            round_num = None
            if round_el:
                r_match = re.search(r'\d+', round_el.text)
                if r_match: round_num = int(r_match.group())

            score_items = soup.select(".blk-matchheader__score-item")
            home_score, away_score = None, None
            tech_loss = False
            match_status = MatchStatus.SCHEDULED

            if "Техническое поражение" in soup.get_text(): tech_loss = True

            if len(score_items) >= 3:
                h_str = score_items[0].text.strip()
                a_str = score_items[2].text.strip()
                if h_str.isdigit() and a_str.isdigit():
                    home_score = int(h_str)
                    away_score = int(a_str)
                    match_status = MatchStatus.FINISHED

            # Если ТП, но счета нет — ставим 3:0 в пользу того, кто не виноват
            if tech_loss and match_status != MatchStatus.FINISHED:
                match_status = MatchStatus.FINISHED
                if home_score is None: home_score = 3
                if away_score is None: away_score = 0
                # В реальности тут нужно смотреть, кто именно получил ТП. Если в HTML есть маркер, парсим его.

            # Пенальти
            home_penalty, away_penalty = None, None
            match_header = soup.select_one(".blk-matchheader")
            if match_header:
                header_text = match_header.get_text(" ", strip=True)
                patterns = [r"(\d+)\s*[:\-]\s*(\d+)\s*по\s*пенальти", r"по\s*пенальти\s*(\d+)\s*[:\-]\s*(\d+)",
                            r"пенальти\s*(\d+)\s*[:\-]\s*(\d+)"]
                for pattern in patterns:
                    m = re.search(pattern, header_text, re.IGNORECASE)
                    if m:
                        home_penalty = int(m.group(1))
                        away_penalty = int(m.group(2))
                        break

            existing_match = await db.scalar(select(Match).where(Match.jliga_id == match_jliga_id))

            if not existing_match:
                existing_match = Match(
                    jliga_id=match_jliga_id, tournament_id=tournament_id,
                    home_team_id=home_team.id, away_team_id=away_team.id,
                    match_date=match_date, stadium=stadium, round=round_num,
                    status=match_status, home_score=home_score, away_score=away_score, tech_loss=tech_loss,
                    home_penalty=home_penalty, away_penalty=away_penalty,
                )
                db.add(existing_match)
                await db.flush()
            else:
                existing_match.tournament_id = tournament_id
                existing_match.home_team_id = home_team.id
                existing_match.away_team_id = away_team.id
                existing_match.match_date = match_date
                existing_match.stadium = stadium
                existing_match.round = round_num
                existing_match.status = match_status
                existing_match.home_score = home_score
                existing_match.away_score = away_score
                existing_match.tech_loss = tech_loss
                existing_match.home_penalty = home_penalty
                existing_match.away_penalty = away_penalty
                db.add(existing_match)

            # Удаляем старые события
            await db.execute(delete(MatchEvent).where(MatchEvent.match_id == existing_match.id))

            events_added = 0
            if match_status == MatchStatus.FINISHED and not tech_loss:
                seen_events = set()
                with db.no_autoflush:
                    for item in soup.select(".timeline__item"):
                        minute_el = item.select_one(".timeline__minute-text")
                        if not minute_el: continue

                        minute_str = minute_el.text.replace("'", "").strip()
                        try:
                            if '+' in minute_str:
                                parts = minute_str.split('+')
                                minute = int(parts[0]) + int(parts[1])
                            else:
                                minute = int(minute_str)
                        except ValueError:
                            continue

                        blocks = item.select(".timeline__event")
                        if len(blocks) < 2: continue

                        left_ev, right_ev = blocks[0], blocks[1]
                        is_left, event_data = False, None

                        if left_ev.find("svg") or left_ev.find("a", class_="timeline__name"):
                            event_data, is_left = left_ev, True
                        elif right_ev.find("svg") or right_ev.find("a", class_="timeline__name"):
                            event_data, is_left = right_ev, False
                        if not event_data: continue

                        icon = event_data.select_one(".timeline__icon")
                        title = icon.get("title", "") if icon else ""

                        event_type = None
                        if "Гол" in title and "Автогол" not in title:
                            event_type = MatchEventType.GOAL
                        elif "Автогол" in title:
                            event_type = MatchEventType.OWN_GOAL
                        elif "Пенальти" in title:
                            event_type = MatchEventType.PENALTY_GOAL
                        elif "Жёлтая" in title:
                            event_type = MatchEventType.YELLOW_CARD
                        elif "Красная" in title:
                            event_type = MatchEventType.RED_CARD
                        if not event_type: continue

                        player_link = event_data.select_one(".timeline__name")
                        if not player_link: continue

                        player_jliga_id = int(player_link["href"].split("/")[-1])
                        player_name = player_link.text.strip()

                        event_key = (player_jliga_id, minute, event_type.value)
                        if event_key in seen_events: continue
                        seen_events.add(event_key)

                        player = await db.scalar(select(Player).where(Player.jliga_id == player_jliga_id))
                        if not player:
                            name_parts = player_name.split(" ", 1)
                            first_name = name_parts[0] if len(name_parts) > 0 else "Unknown"
                            last_name = name_parts[1] if len(name_parts) > 1 else name_parts[0]
                            player = Player(jliga_id=player_jliga_id, first_name=first_name, last_name=last_name)
                            db.add(player)
                            await db.flush()

                        # ⚠️ ФИКС АВТОГОЛОВ: team_id должен быть командой ИГРОКА
                        actual_team_id = existing_match.home_team_id if is_left else existing_match.away_team_id

                        db.add(MatchEvent(
                            match_id=existing_match.id, team_id=actual_team_id,
                            player_id=player.id, event_type=event_type, minute=minute
                        ))
                        events_added += 1

            await db.commit()

            # Логируем только завершенные
            if match_status == MatchStatus.FINISHED:
                print(f"  ✅ {home_team.name} {home_score}:{away_score} {away_team.name} | Событий: {events_added}")

        except Exception as e:
            print(f"  ❌ Ошибка при парсинге матча {match_jliga_id}: {e}")
            await db.rollback()


async def match_worker(queue: asyncio.Queue, page, year: str, tournament_id: int):
    """Воркер, который берет матчи из очереди и парсит их"""
    while True:
        try:
            match_url = queue.get_nowait()
        except asyncio.QueueEmpty:
            break

        await sync_match(page, match_url, year, tournament_id)
        queue.task_done()


async def main():
    print("🚀 Старт Оптимизированного Парсера 2026 (Многопоточность)...")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )

        # Создаем пул вкладок
        pages = []
        for _ in range(PAGES_POOL_SIZE):
            page = await context.new_page()
            await setup_page(page)
            pages.append(page)

        for year, season_id in SEASONS.items():
            print(f"\n📅 СЕЗОН {year}")
            url = f"{BASE_URL}/tournaments?season_id={season_id}"

            # Используем первую вкладку для получения списка турниров
            html = await fetch_html(pages[0], url)
            if not html: continue

            soup = BeautifulSoup(html, "html.parser")
            t_links = set()
            for a in soup.find_all("a", href=True):
                if "/tournament/" in a["href"]:
                    t_id = a["href"].split("/tournament/")[-1].split("?")[0].split("/")[0]
                    if t_id.isdigit():
                        t_links.add(int(t_id))

            for t_id in t_links:
                async with session_factory() as db:
                    t = await db.scalar(select(Tournament).where(Tournament.jliga_id == t_id))
                    if not t: continue
                    t_db_id = t.id
                    t_name = t.name

                print(f"\n🏆 Турнир: {t_name}")

                # Обновляем таблицу на 1-й вкладке
                await sync_tournament_table(pages[0], t_id)

                # Получаем список матчей на 1-й вкладке
                cal_url = f"{BASE_URL}/tournament/{t_id}/calendar"
                cal_html = await fetch_html(pages[0], cal_url)
                if not cal_html: continue

                cal_soup = BeautifulSoup(cal_html, "html.parser")
                match_links = set()
                for a in cal_soup.find_all("a", href=True):
                    if "/match/" in a["href"]:
                        m_id = a["href"].split("/match/")[-1].split("?")[0].split("/")[0]
                        if m_id.isdigit():
                            match_links.add(f"{BASE_URL}/match/{m_id}")

                print(f"  -> Найдено матчей: {len(match_links)}. Запускаем парсинг в {PAGES_POOL_SIZE} потоков...")

                # Создаем очередь из матчей
                queue = asyncio.Queue()
                for link in match_links:
                    queue.put_nowait(link)

                # Запускаем воркеры
                tasks = []
                for page in pages:
                    task = asyncio.create_task(match_worker(queue, page, year, t_db_id))
                    tasks.append(task)

                # Ждем, пока все матчи турнира спарсятся
                await asyncio.gather(*tasks)

        # Закрываем всё
        for page in pages:
            await page.close()
        await context.close()
        await browser.close()

    print("\n🎉 ГОТОВО!")


if __name__ == "__main__":
    asyncio.run(main())