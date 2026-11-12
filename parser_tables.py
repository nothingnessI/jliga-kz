import asyncio
import random
import re
from datetime import datetime
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from sqlalchemy import select, delete

from app.db.session import session_factory
from app.models.tournament import Tournament
from app.models.team import Team
from app.models.player import Player
from app.models.match import Match
from app.models.match_event import MatchEvent
from app.models.team_player import TeamPlayer
from app.models.enums import MatchStatus, MatchEventType

SEASONS = {"2026": "10129829"}
BASE_URL = "https://jliga.ru"

SEMAPHORE = asyncio.Semaphore(5)
DB_SEMAPHORE = asyncio.Semaphore(1)

BLOCKED_TYPES = {"image", "media", "font", "stylesheet"}
RU_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12
}


def is_garbage(name: str, logo_url: str | None) -> bool:
    if not logo_url: return True
    if re.search(r"\(\d+\)$", name): return True
    return False


def parse_date(date_str: str, year: str) -> datetime | None:
    if not date_str: return None
    try:
        parts = date_str.split()
        day = int(parts[0])
        month = RU_MONTHS.get(parts[1].lower())
        time_parts = parts[2].split(":")
        return datetime(year=int(year), month=month, day=day, hour=int(time_parts[0]), minute=int(time_parts[1]))
    except Exception:
        return None


async def setup_page(page):
    async def handle_route(route):
        if route.request.resource_type in BLOCKED_TYPES:
            await route.abort()
        else:
            await route.continue_()

    await page.route("**/*", handle_route)


async def fetch_html(page, url: str) -> str | None:
    async with SEMAPHORE:
        try:
            await asyncio.sleep(random.uniform(0.5, 1.5))
            response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            if response and response.status == 200:
                try:
                    await page.wait_for_selector(".blk-matchheader, .blk-tournamentheader", timeout=4000)
                except:
                    pass
                return await page.content()
            return None
        except Exception:
            return None


async def sync_match(page, match_url: str, year: str, tournament_id: int):
    match_id_str = match_url.split("/")[-1]
    if not match_id_str.isdigit(): return
    match_jliga_id = int(match_id_str)

    try:
        html = await fetch_html(page, f"{BASE_URL}/match/{match_jliga_id}?active=events")
        if not html: return

        soup = BeautifulSoup(html, "html.parser")

        team_links = soup.select(".blk-matchheader__logo")
        if len(team_links) < 2: return

        home_team_jliga_id = int(team_links[0]["href"].split("team_id=")[-1])
        away_team_jliga_id = int(team_links[1]["href"].split("team_id=")[-1])

        team_names = soup.select(".blk-matchheader__team-name")
        home_name = team_names[0].text.strip() if len(team_names) > 0 else None
        away_name = team_names[1].text.strip() if len(team_names) > 1 else None

        logo_imgs = soup.select(".blk-matchheader__logo-img")
        home_logo_url = logo_imgs[0]["src"] if len(logo_imgs) > 0 else None
        away_logo_url = logo_imgs[1]["src"] if len(logo_imgs) > 1 else None

        if is_garbage(home_name, home_logo_url) or is_garbage(away_name, away_logo_url):
            print(f"  ⏭️ Мусор в матче {match_jliga_id}, пропуск")
            return

        # Дата, стадион
        date_el = soup.select_one(".blk-matchheader__date")
        time_el = soup.select_one(".blk-matchheader__time")
        stadium_el = soup.select_one(".blk-matchheader__stadium")

        date_str = f"{date_el.text.strip()} {time_el.text.strip()}" if date_el and time_el else ""
        match_date = parse_date(date_str, year)
        stadium = stadium_el.text.strip() if stadium_el else None

        # Счет и ТП
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

        if tech_loss:
            match_status = MatchStatus.FINISHED
            if home_score is None: home_score = 3
            if away_score is None: away_score = 0

        # Пенальти
        home_pen, away_pen = None, None
        match_header = soup.select_one(".blk-matchheader")
        if match_header:
            header_text = match_header.get_text(" ", strip=True)
            m = re.search(r"(\d+)\s*[:\-]\s*(\d+)\s*по\s*пенальти", header_text, re.IGNORECASE)
            if m:
                home_pen = int(m.group(1))
                away_pen = int(m.group(2))

        # ============ БД ============
        async with DB_SEMAPHORE:
            async with session_factory() as db:
                home_team = await db.scalar(select(Team).where(Team.jliga_id == home_team_jliga_id))
                if not home_team:
                    home_team = Team(jliga_id=home_team_jliga_id, name=home_name, slug=f"team-{home_team_jliga_id}",
                                     logo_url=home_logo_url)
                    db.add(home_team)
                    await db.flush()

                away_team = await db.scalar(select(Team).where(Team.jliga_id == away_team_jliga_id))
                if not away_team:
                    away_team = Team(jliga_id=away_team_jliga_id, name=away_name, slug=f"team-{away_team_jliga_id}",
                                     logo_url=away_logo_url)
                    db.add(away_team)
                    await db.flush()

                match = await db.scalar(select(Match).where(Match.jliga_id == match_jliga_id))
                if not match:
                    match = Match(
                        jliga_id=match_jliga_id, tournament_id=tournament_id,
                        home_team_id=home_team.id, away_team_id=away_team.id,
                        match_date=match_date, stadium=stadium,
                        status=match_status, home_score=home_score, away_score=away_score,
                        tech_loss=tech_loss, home_penalty=home_pen, away_penalty=away_pen
                    )
                    db.add(match)
                    await db.flush()
                else:
                    match.tournament_id = tournament_id
                    match.home_team_id = home_team.id
                    match.away_team_id = away_team.id
                    match.match_date = match_date
                    match.stadium = stadium
                    match.status = match_status
                    match.home_score = home_score
                    match.away_score = away_score
                    match.tech_loss = tech_loss
                    match.home_penalty = home_pen
                    match.away_penalty = away_pen
                    db.add(match)

                # Удаляем старые события и фиксируем в БД, чтобы не было дублей
                await db.execute(delete(MatchEvent).where(MatchEvent.match_id == match.id))
                await db.commit()

                # ============ СОБЫТИЯ ============
                if match_status == MatchStatus.FINISHED and not tech_loss:
                    seen = set()  # (team_id, player_id, minute, event_type)

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

                            player = await db.scalar(select(Player).where(Player.jliga_id == player_jliga_id))
                            if not player:
                                name_parts = player_name.split(" ", 1)
                                first_name = name_parts[0] if len(name_parts) > 0 else "Unknown"
                                last_name = name_parts[1] if len(name_parts) > 1 else name_parts[0]
                                player = Player(jliga_id=player_jliga_id, first_name=first_name, last_name=last_name)
                                db.add(player)
                                await db.flush()

                            is_home_team = is_left
                            if event_type == MatchEventType.OWN_GOAL:
                                is_home_team = not is_left
                            actual_team_id = match.home_team_id if is_home_team else match.away_team_id

                            # 🔑 ЗАЩИТА ОТ ДУБЛЕЙ (одинаковые события на сайте)
                            event_key = (actual_team_id, player.id, minute, event_type)
                            if event_key in seen:
                                print(f"    ⚠️ Дубль события на сайте: {player.last_name} {minute}' {event_type.value}")
                                continue
                            seen.add(event_key)

                            db.add(MatchEvent(
                                match_id=match.id, team_id=actual_team_id,
                                player_id=player.id, event_type=event_type, minute=minute
                            ))

                await db.commit()
                print(f"  ✅ Матч {match_jliga_id} сохранен")
    except Exception as e:
        print(f"  ❌ Матч {match_jliga_id} упал: {str(e)[:100]}")


async def process_tournament(page, t_id, year):
    try:
        async with DB_SEMAPHORE:
            async with session_factory() as db:
                t = await db.scalar(select(Tournament).where(Tournament.jliga_id == t_id))
                if not t: return
                t_name, t_db_id = t.name, t.id

        print(f"\n🏆 Турнир: {t_name}")
        cal_url = f"{BASE_URL}/tournament/{t_id}/calendar"
        cal_html = await fetch_html(page, cal_url)
        if not cal_html: return

        soup = BeautifulSoup(cal_html, "html.parser")
        match_links = set()
        for a in soup.find_all("a", href=True):
            if "/match/" in a["href"]:
                m_id = a["href"].split("/match/")[-1].split("?")[0].split("/")[0]
                if m_id.isdigit():
                    match_links.add(f"{BASE_URL}/match/{m_id}")

        print(f"  -> Матчей: {len(match_links)}")
        for m_url in sorted(match_links):
            await sync_match(page, m_url, year, t_db_id)
    except Exception as e:
        print(f"  ❌ Турнир {t_id} упал: {str(e)[:100]}")


async def main():
    print("🚀 Парсим календари и матчи (5 вкладок)...")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )

        pages = [await context.new_page() for _ in range(5)]
        for pg in pages:
            await setup_page(pg)

        list_page = await context.new_page()
        await setup_page(list_page)

        for year, season_id in SEASONS.items():
            print(f"\n📅 СЕЗОН {year}")
            html = await fetch_html(list_page, f"{BASE_URL}/tournaments?season_id={season_id}")
            if not html: continue

            soup = BeautifulSoup(html, "html.parser")
            t_links = set()
            for a in soup.find_all("a", href=True):
                if "/tournament/" in a["href"]:
                    t_id = a["href"].split("/tournament/")[-1].split("?")[0].split("/")[0]
                    if t_id.isdigit():
                        t_links.add(int(t_id))

            print(f"Турниров: {len(t_links)}")

            tasks = []
            for i, t_id in enumerate(sorted(t_links)):
                page = pages[i % 5]
                tasks.append(process_tournament(page, t_id, year))

            await asyncio.gather(*tasks)

        for pg in pages + [list_page]:
            await pg.close()
        await context.close()
        await browser.close()

    print("\n🎉 ГОТОВО!")


if __name__ == "__main__":
    asyncio.run(main())