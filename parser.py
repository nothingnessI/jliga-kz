import asyncio
import re
from datetime import datetime, timezone, timedelta
from curl_cffi.requests import AsyncSession
from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession as DbSession

from app.db.session import session_factory
from app.models.match import Match
from app.models.match_event import MatchEvent
from app.models.player import Player
from app.models.team import Team
from app.models.team_player import TeamPlayer
from app.models.enums import MatchStatus, MatchEventType, PlayerPosition
from app.services.stats_recalc import StatsRecalculator

BASE_URL = "https://jliga.ru"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}

MONTHS_RU = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12
}


async def fetch(session, url):
    try:
        resp = await session.get(url, headers=HEADERS, impersonate="chrome124", timeout=30)
        if resp.status_code == 200:
            return resp.text
        else:
            print(f"  ⚠️ HTTP {resp.status_code} на {url}")
    except Exception as e:
        print(f"  ❌ Ошибка сети {url}: {e}")
    return None


def parse_date_time(match_header, tournament_year: int):
    """Парсит дату и время из шапки матча. Возвращает (datetime|None, is_tba)."""
    date_li = match_header.find("li", class_="blk-matchheader__date")
    time_li = match_header.find("li", class_="blk-matchheader__time")

    if not date_li or not time_li:
        return None, True

    date_str = date_li.text.strip()
    time_str = time_li.text.strip()

    # Если TBA / не назначено
    if not date_str or "TBA" in date_str.upper() or not time_str or "TBA" in time_str.upper():
        return None, True

    try:
        # "17 августа" → day=17, month_name="августа"
        parts = date_str.split(" ")
        if len(parts) < 2:
            return None, True
        day = int(parts[0])
        month = MONTHS_RU.get(parts[1].lower())
        if not month:
            return None, True

        hour, minute = map(int, time_str.split(":"))
        local_tz = timezone(timedelta(hours=5))
        return datetime(tournament_year, month, day, hour, minute, tzinfo=local_tz), False
    except Exception as e:
        print(f"  ⚠️ Не удалось распарсить дату '{date_str} {time_str}': {e}")
        return None, True


def get_tournament_year(soup) -> int:
    """Извлекает год из шапки турнира (например, 2026 из '15 августа 2026')"""
    tournament_header = soup.find("ul", class_="blk-tournamentheader__info")
    if tournament_header:
        dates_li = tournament_header.find("li", class_="blk-tournamentheader__info-dates")
        if dates_li:
            y_match = re.search(r'(\d{4})', dates_li.text)
            if y_match:
                return int(y_match.group(1))
    return datetime.now().year


async def parse_match_html(html: str, jliga_id: int, db: DbSession):
    """Парсит основную страницу матча: дату, стадион, счет, события."""
    soup = BeautifulSoup(html, "html.parser")

    match = await db.scalar(select(Match).where(Match.jliga_id == jliga_id))
    if not match:
        print(f"  ⚠️ Матч с jliga_id={jliga_id} не найден в БД.")
        return None

    match_header = soup.find("section", class_="blk-matchheader")
    if not match_header:
        print("  ⚠️ Не найден блок матча (blk-matchheader).")
        return None

    # ===== ГОД ТУРНИРА =====
    tournament_year = get_tournament_year(soup)

    # ===== ДАТА И ВРЕМЯ =====
    parsed_date, is_tba = parse_date_time(match_header, tournament_year)
    if not is_tba and parsed_date:
        match.match_date = parsed_date
        print(f"  📅 Дата: {parsed_date.strftime('%d.%m.%Y %H:%M')}")
    else:
        print(f"  📅 Дата: TBA")
        match.match_date = None

    # ===== СЧЕТ =====
    score_items = match_header.find_all("div", class_="blk-matchheader__score-item")
    home_score = None
    away_score = None

    if len(score_items) >= 3:
        try:
            h = score_items[0].text.strip()
            a = score_items[2].text.strip()
            home_score = int(h) if h else None
            away_score = int(a) if a else None
        except (ValueError, IndexError):
            pass

    # ===== СТАДИОН =====
    stadium_div = match_header.find("div", class_="blk-matchheader__stadium")
    if stadium_div:
        match.stadium = stadium_div.text.strip()

    # ===== СТАТУС И СЧЕТ =====
    if home_score is not None and away_score is not None:
        match.home_score = home_score
        match.away_score = away_score
        match.status = MatchStatus.FINISHED
        print(f"  ✅ Счет {home_score}:{away_score}, стадион: {match.stadium}")
    else:
        match.home_score = None
        match.away_score = None
        # Определяем — запланирован или live
        status_li = match_header.find("li", class_="blk-matchheader__info-middle")
        if status_li and "live" in status_li.text.lower():
            match.status = MatchStatus.LIVE
            print(f"  🔴 Матч LIVE, стадион: {match.stadium}")
        else:
            match.status = MatchStatus.SCHEDULED
            print(f"  🕐 Матч запланирован, стадион: {match.stadium}")

    db.add(match)
    await db.commit()

    # ===== СОБЫТИЯ (только для завершённых матчей) =====
    new_events = 0
    if home_score is not None and away_score is not None:
        timeline_items = soup.find_all("li", class_="timeline__item")

        for item in timeline_items:
            try:
                minute_div = item.find("div", class_="timeline__minute-text")
                if not minute_div: continue

                minute_str = minute_div.text.replace("'", "").strip()
                if not minute_str.isdigit():
                    continue
                minute = int(minute_str)

                icon_div = item.find("div", class_="timeline__icon")
                if not icon_div: continue

                title = icon_div.get("title", "")
                event_type = None
                if "Гол" in title:
                    event_type = MatchEventType.GOAL
                elif "Жёлтая" in title:
                    event_type = MatchEventType.YELLOW_CARD
                elif "Красная" in title:
                    event_type = MatchEventType.RED_CARD
                elif "Замена" in title:
                    event_type = MatchEventType.SUBSTITUTION_IN

                if not event_type: continue

                player_link = item.find("a", class_="timeline__name")
                if not player_link: continue

                p_href = player_link.get("href", "")
                p_id_match = re.search(r'/player/(\d+)', p_href)
                if not p_id_match: continue
                p_jliga_id = int(p_id_match.group(1))

                events = item.find_all("div", class_="timeline__event")
                is_home = len(events) > 0 and events[0].find("div", class_="timeline__icon") is not None

                player = await db.scalar(select(Player).where(Player.jliga_id == p_jliga_id))
                if not player:
                    name_parts = player_link.text.strip().split(" ", 1)
                    player = Player(
                        jliga_id=p_jliga_id,
                        last_name=name_parts[0] if name_parts else "Unknown",
                        first_name=name_parts[1] if len(name_parts) > 1 else ""
                    )
                    db.add(player)
                    await db.commit()
                    await db.refresh(player)

                team_id = match.home_team_id if is_home else match.away_team_id

                existing = await db.scalar(select(MatchEvent).where(
                    MatchEvent.match_id == match.id,
                    MatchEvent.player_id == player.id,
                    MatchEvent.minute == minute,
                    MatchEvent.event_type == event_type
                ))
                if existing:
                    continue

                new_event = MatchEvent(
                    match_id=match.id,
                    team_id=team_id,
                    player_id=player.id,
                    event_type=event_type,
                    minute=minute
                )
                db.add(new_event)
                new_events += 1
            except Exception as e:
                print(f"  ⚠️ Ошибка парсинга события: {e}")
                continue

        await db.commit()
        print(f"  ✅ Добавлено событий: {new_events}")

    return match


async def parse_protocol_html(html: str, jliga_id: int, db: DbSession):
    """Парсит составы команд из вкладки Протокол."""
    soup = BeautifulSoup(html, "html.parser")

    match = await db.scalar(select(Match).where(Match.jliga_id == jliga_id))
    if not match:
        return

    protocol_container = soup.find("div", class_="protocol__container")
    if not protocol_container:
        print("  ⚠️ Протокол не найден (protocol__container)")
        return

    units = protocol_container.find_all("div", class_="protocol__unit")
    if len(units) < 2:
        print(f"  ⚠️ Блоков protocol__unit: {len(units)}. Пропускаем.")
        return

    added_players = 0

    for idx, unit in enumerate(units):
        is_home = (idx == 0)
        team_id = match.home_team_id if is_home else match.away_team_id

        players_list = unit.find("ul", class_="protocol__list")
        if not players_list: continue

        for item in players_list.find_all("li", class_="protocol__item"):
            try:
                link = item.find("a", class_="protocol__link")
                if not link: continue

                p_href = link.get("href", "")
                p_id_match = re.search(r'/player/(\d+)', p_href)
                if not p_id_match: continue
                p_jliga_id = int(p_id_match.group(1))

                name_div = link.find("div", class_="protocol__name")
                player_name = name_div.text.strip() if name_div else "Unknown"

                num_div = item.find("div", class_="protocol__number-text")
                jersey_number = int(num_div.text.strip()) if num_div and num_div.text.strip().isdigit() else None

                role_div = link.find("div", class_="protocol__role")
                role_text = role_div.text.strip() if role_div else ""

                position = None
                if role_text in ["ВР", "Вратарь"]:
                    position = PlayerPosition.GOALKEEPER
                elif role_text in ["З", "Защитник"]:
                    position = PlayerPosition.DEFENDER
                elif role_text in ["П", "Полузащитник"]:
                    position = PlayerPosition.MIDFIELDER
                elif role_text in ["Н", "Нападающий"]:
                    position = PlayerPosition.FORWARD

                player = await db.scalar(select(Player).where(Player.jliga_id == p_jliga_id))
                if not player:
                    name_parts = player_name.split(" ", 1)
                    player = Player(
                        jliga_id=p_jliga_id,
                        last_name=name_parts[0] if name_parts else "Unknown",
                        first_name=name_parts[1] if len(name_parts) > 1 else ""
                    )
                    db.add(player)
                    await db.commit()
                    await db.refresh(player)

                if position and player.position != position:
                    player.position = position
                    db.add(player)

                tp = await db.scalar(select(TeamPlayer).where(
                    TeamPlayer.player_id == player.id,
                    TeamPlayer.team_id == team_id,
                    TeamPlayer.tournament_id == match.tournament_id
                ))

                if not tp:
                    tp = TeamPlayer(
                        player_id=player.id,
                        team_id=team_id,
                        tournament_id=match.tournament_id,
                        is_active=True
                    )
                    db.add(tp)
                    added_players += 1

                if jersey_number:
                    tp.jersey_number = jersey_number
            except Exception as e:
                print(f"  ⚠️ Ошибка парсинга игрока: {e}")
                continue

    await db.commit()
    print(f"  ✅ Протокол обновлён. Новых игроков: {added_players}")


async def process_match(session, jliga_id: int, db: DbSession):
    """Скачивает и парсит один матч. Не падает при ошибках."""
    print(f"\n🔄 Матч ID {jliga_id}")

    # 1. Основная страница
    url_main = f"{BASE_URL}/match/{jliga_id}"
    html_main = await fetch(session, url_main)
    if not html_main:
        print(f"  ❌ Не удалось скачать основную страницу. Пропускаем.")
        return None

    try:
        match = await parse_match_html(html_main, jliga_id, db)
    except Exception as e:
        print(f"  ❌ Ошибка парсинга основной страницы: {e}")
        return None

    if not match:
        return None

    # 2. Протокол (только если матч уже начался/завершился)
    if match.status in (MatchStatus.FINISHED, MatchStatus.LIVE):
        url_protocol = f"{BASE_URL}/match/{jliga_id}?active=protocol"
        html_protocol = await fetch(session, url_protocol)
        if html_protocol:
            try:
                await parse_protocol_html(html_protocol, jliga_id, db)
            except Exception as e:
                print(f"  ❌ Ошибка парсинга протокола: {e}")
    else:
        print(f"  ⏭️ Матч не начался — протокол не парсим.")

    return match


async def main():
    local_tz = timezone(timedelta(hours=5))
    start_date = datetime(2026, 11, 11, 0, 0, 0, tzinfo=local_tz)
    end_date = datetime(2026, 11, 17, 0, 0, 0, tzinfo=local_tz)

    print(f"📅 Период: {start_date.strftime('%d.%m.%Y')} - {end_date.strftime('%d.%m.%Y')}")

    async with AsyncSession() as session:
        async with session_factory() as db:
            stmt = select(Match).where(
                Match.match_date >= start_date,
                Match.match_date < end_date,
                Match.jliga_id.isnot(None)
            ).order_by(Match.match_date.asc())

            matches = (await db.execute(stmt)).scalars().all()
            print(f"🎯 Найдено матчей в БД за период: {len(matches)}")

            if not matches:
                print("⚠️ Матчей за этот период нет в БД.")
                return

            tournament_ids = set()
            success_count = 0
            fail_count = 0

            for m in matches:
                result = await process_match(session, m.jliga_id, db)
                if result:
                    tournament_ids.add(m.tournament_id)
                    success_count += 1
                else:
                    fail_count += 1
                await asyncio.sleep(2)

            print(f"\n📈 Итого: успешно {success_count}, с ошибками {fail_count}")

            if tournament_ids:
                print(f"\n📊 Пересчитываем турнирные таблицы...")
                for t_id in tournament_ids:
                    print(f"  🔄 Турнир ID {t_id}...")
                    try:
                        await StatsRecalculator.recalculate_all(db, t_id)
                    except Exception as e:
                        print(f"  ❌ Ошибка пересчёта турнира {t_id}: {e}")

    print("\n✅ Готово!")


if __name__ == "__main__":
    asyncio.run(main())