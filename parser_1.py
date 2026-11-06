import asyncio
import re
from datetime import datetime
import httpx
from bs4 import BeautifulSoup
from sqlalchemy import select

from app.db.session import session_factory
from app.models.player import Player
from app.models.team import Team
from app.models.team_player import TeamPlayer
from app.models.match import Match
from app.models.match_event import MatchEvent
from app.models.tournament import Tournament
from app.models.enums import MatchStatus, MatchEventType, TournamentStatus
from app.services.standings import StandingsService

SEASONS = {
    "2026": "10129829", "2025": "10128371", "2024": "10123964", "2023": "10121263",
    "2022": "10118362", "2021": "1008904", "2020": "1004684", "2019": "1003111"
}

MONTHS = {
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "май": 5, "июн": 6,
    "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12
}

CONCURRENT_TABS = 20
TIMEOUT = 20.0


def parse_date(date_str: str, time_str: str, year: int) -> datetime | None:
    try:
        if "Не задано" in date_str or not time_str:
            return None
        d_match = re.search(r'(\d+)\s+([а-яa-z]+)', date_str.lower())
        if not d_match: return None
        day = int(d_match.group(1))
        month_str = d_match.group(2).rstrip('.')
        month = MONTHS.get(month_str)
        if not month: return None
        t_match = re.search(r'(\d+):(\d+)', time_str)
        if not t_match: return datetime(year, month, day)
        return datetime(year, month, day, int(t_match.group(1)), int(t_match.group(2)))
    except Exception:
        return None


async def fetch_team_calendar(client: httpx.AsyncClient, team_id: int, year_str: str, season_id: str,
                              semaphore: asyncio.Semaphore):
    async with semaphore:
        matches_data = {}
        year = int(year_str)
        url = f"https://jliga.ru/team/{team_id}/calendar?season_id={season_id}"

        for attempt in range(3):
            try:
                resp = await client.get(url)
                if resp.status_code == 200:
                    soup = BeautifulSoup(resp.text, "html.parser")
                    units = soup.find_all("div", class_="blk-lschedule__unit")

                    for unit in units:
                        head = unit.find(class_="blk-lschedule__head-text")
                        if not head: continue
                        t_name = head.text.strip()

                        for item in unit.find_all("li", class_="blk-lschedule__item"):
                            score_link = item.find("a", class_="blk-lschedule__score")
                            if not score_link: continue

                            href = score_link.get("href") or ""
                            m_id_match = re.search(r"/match/(\d+)", href)
                            if not m_id_match: continue
                            jm_id = int(m_id_match.group(1))

                            date_elem = item.find("span", class_="blk-lschedule__time")
                            place_elem = item.find("span", class_="blk-lschedule__place-name")

                            date_str = date_elem.text.strip() if date_elem else ""
                            time_match = re.search(r'\d+:\d+', date_str)
                            time_str = time_match.group(0) if time_match else ""
                            m_date = parse_date(date_str, time_str, year)
                            m_stadium = place_elem.text.strip() if place_elem else None

                            left_team = item.find("a", class_="blk-lschedule__team--left")
                            right_team = item.find("a", class_="blk-lschedule__team--right")
                            if not left_team or not right_team: continue

                            l_name_div = left_team.find("div", class_="blk-lschedule__team-name")
                            r_name_div = right_team.find("div", class_="blk-lschedule__team-name")
                            l_name = l_name_div.text.strip() if l_name_div else "Unknown"
                            r_name = r_name_div.text.strip() if r_name_div else "Unknown"

                            l_id_match = re.search(r"team_id=(\d+)", left_team.get("href", ""))
                            r_id_match = re.search(r"team_id=(\d+)", right_team.get("href", ""))
                            if not l_id_match or not r_id_match: continue

                            l_id = int(l_id_match.group(1))
                            r_id = int(r_id_match.group(1))

                            score_main = score_link.find("div", class_="blk-lschedule__score-main")
                            home_score, away_score = None, None
                            if score_main and "-" in score_main.text and ":" not in score_main.text:
                                parts = score_main.text.split("-")
                                if parts[0].strip().isdigit() and parts[1].strip().isdigit():
                                    home_score, away_score = int(parts[0].strip()), int(parts[1].strip())

                            matches_data[jm_id] = {
                                "tournament_name": t_name, "season": year_str,
                                "home_id": l_id, "home_name": l_name,
                                "away_id": r_id, "away_name": r_name,
                                "date": m_date, "stadium": m_stadium,
                                "home_score": home_score, "away_score": away_score
                            }
                    return matches_data
            except Exception:
                await asyncio.sleep(2)
        return matches_data


async def fetch_match_details(client: httpx.AsyncClient, jm_id: int, semaphore: asyncio.Semaphore):
    async with semaphore:
        data = {"protocol": [], "events": []}
        try:
            # 1. Протокол (с парсингом фото)
            resp = await client.get(f"https://jliga.ru/match/{jm_id}?active=protocol")
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                units = soup.find_all("div", class_="protocol__unit")
                if len(units) >= 2:
                    for idx in [0, 1]:
                        for p_item in units[idx].find_all("li", class_="protocol__item"):
                            link = p_item.find("a", class_="protocol__link")
                            if not link: continue
                            pid_match = re.search(r"/player/(\d+)", link.get("href"))
                            if not pid_match: continue
                            pid = int(pid_match.group(1))

                            name_div = p_item.find("div", class_="protocol__name")
                            name = name_div.text.strip() if name_div else ""
                            num_div = p_item.find("div", class_="protocol__number")
                            num = num_div.text.strip() if num_div else ""

                            # Ищем фото игрока
                            photo_url = None
                            img_el = p_item.find("img")
                            if img_el and img_el.get("src"):
                                photo_url = img_el.get("src")
                                if photo_url.startswith("/"):
                                    photo_url = f"https://jliga.ru{photo_url}"

                            data["protocol"].append({
                                "side": idx, "pid": pid, "name": name,
                                "jersey": int(num) if num.isdigit() else None,
                                "photo_url": photo_url
                            })

            # 2. События
            resp = await client.get(f"https://jliga.ru/match/{jm_id}?active=events")
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                for ev in soup.select(".timeline__item"):
                    minute_div = ev.select_one(".timeline__minute-text")
                    if not minute_div: continue
                    minute_match = re.search(r"(\d+)", minute_div.text)
                    if not minute_match: continue
                    minute = int(minute_match.group(1))

                    side_el = ev.select_one(".timeline__side")
                    is_home = False
                    if side_el and "timeline__side--left" in side_el.get("class", []):
                        is_home = True

                    name_a = ev.select_one(".timeline__name")
                    player_name = name_a.text.strip() if name_a else ""
                    pid = None
                    if name_a and "href" in name_a.attrs:
                        pid_match = re.search(r"/player/(\d+)", name_a["href"])
                        if pid_match: pid = int(pid_match.group(1))

                    icons = ev.select(".timeline__icon svg")
                    for icon in icons:
                        classes = " ".join(icon.get("class", [])).lower()
                        e_type = None
                        if "yellow" in classes:
                            e_type = MatchEventType.YELLOW_CARD
                        elif "red" in classes:
                            e_type = MatchEventType.RED_CARD
                        elif "penalty" in classes:
                            e_type = MatchEventType.PENALTY_GOAL
                        elif "own" in classes:
                            e_type = MatchEventType.OWN_GOAL
                        elif "goal" in classes or "ball" in classes:
                            e_type = MatchEventType.GOAL

                        if e_type:
                            data["events"].append({
                                "pid": pid, "name": player_name,
                                "minute": minute, "type": e_type, "is_home": is_home
                            })
        except Exception:
            pass
        return data


async def collect_all_data(teams_jliga_ids):
    unique_matches = {}
    match_details = {}

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36"}
    limits = httpx.Limits(max_connections=CONCURRENT_TABS, max_keepalive_connections=CONCURRENT_TABS)

    async with httpx.AsyncClient(headers=headers, limits=limits, timeout=TIMEOUT) as client:
        semaphore = asyncio.Semaphore(CONCURRENT_TABS)

        async def calendar_worker(team_id, year_str, season_id):
            res = await fetch_team_calendar(client, team_id, year_str, season_id, semaphore)
            unique_matches.update(res)

        tasks = []
        for year_str, season_id in SEASONS.items():
            for tid in teams_jliga_ids:
                tasks.append(calendar_worker(tid, year_str, season_id))

        print(f"📡 Сбор календарей команд ({len(tasks)} запросов)...")
        await asyncio.gather(*tasks)
        print(f"✅ Найдено уникальных матчей: {len(unique_matches)}")

        finished_matches = [m_id for m_id, m in unique_matches.items() if m["home_score"] is not None]

        async def match_worker(m_id):
            res = await fetch_match_details(client, m_id, semaphore)
            if res: match_details[m_id] = res

        tasks = [match_worker(m_id) for m_id in finished_matches]
        print(f"📡 Загрузка протоколов и событий ({len(tasks)} матчей)...")
        await asyncio.gather(*tasks)
        print("✅ Все данные успешно собраны в память!")

    return unique_matches, match_details


async def save_to_db(unique_matches, match_details):
    affected_tournament_ids = set()

    async with session_factory() as db:
        print("💾 Загрузка справочников из БД в память...")

        db_tournaments = {(t.name, t.season): t for t in (await db.scalars(select(Tournament))).all()}
        all_teams = (await db.scalars(select(Team))).all()

        db_teams_by_jid = {t.jliga_id: t for t in all_teams if t.jliga_id}
        db_teams_by_name = {t.name: t for t in all_teams}
        existing_slugs = {t.slug for t in all_teams}

        db_matches_by_jid = {m.jliga_id: m for m in
                             (await db.scalars(select(Match).where(Match.jliga_id.is_not(None)))).all() if m.jliga_id}
        db_players_by_jid = {p.jliga_id: p for p in
                             (await db.scalars(select(Player).where(Player.jliga_id.is_not(None)))).all() if p.jliga_id}

        print("  -> Кэширование существующих заявок и событий...")
        existing_tps = (await db.scalars(select(TeamPlayer))).all()
        db_team_players_set = {(tp.player_id, tp.team_id, tp.tournament_id) for tp in existing_tps}
        tp_lookup = {(tp.player_id, tp.tournament_id): tp.team_id for tp in existing_tps}

        existing_events = (await db.scalars(select(MatchEvent))).all()
        db_match_events = {(me.match_id, me.player_id, me.minute, me.event_type) for me in existing_events}

        print(f"💾 Обработка {len(unique_matches)} матчей...")

        for jm_id, m_data in unique_matches.items():
            t_key = (m_data["tournament_name"], m_data["season"])
            db_t = db_tournaments.get(t_key)
            if not db_t:
                db_t = Tournament(name=t_key[0], season=t_key[1], division="UNKNOWN", age_category="UNKNOWN",
                                  status=TournamentStatus.FINISHED, max_teams=64, logo_url="")
                db.add(db_t)
                await db.flush()
                db_tournaments[t_key] = db_t

            async def get_team(j_id, name):
                t = db_teams_by_jid.get(j_id)
                if t: return t
                t = db_teams_by_name.get(name)
                if t:
                    if not t.jliga_id:
                        t.jliga_id = j_id
                        db_teams_by_jid[j_id] = t
                        return t
                    else:
                        new_name = f"{name} ({j_id})"
                        t_new = db_teams_by_name.get(new_name)
                        if t_new: return t_new
                        name = new_name

                base_slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-') or f"team-{j_id}"
                slug = base_slug
                i = 1
                while slug in existing_slugs:
                    slug = f"{base_slug}-{i}"
                    i += 1
                existing_slugs.add(slug)

                t = Team(name=name, jliga_id=j_id, slug=slug, is_active=True)
                db.add(t)
                await db.flush()
                db_teams_by_jid[j_id] = t
                db_teams_by_name[name] = t
                return t

            db_h = await get_team(m_data["home_id"], m_data["home_name"])
            db_a = await get_team(m_data["away_id"], m_data["away_name"])

            db_m = db_matches_by_jid.get(jm_id)
            is_new_or_changed = False

            if not db_m:
                db_m = Match(jliga_id=jm_id, tournament_id=db_t.id, home_team_id=db_h.id, away_team_id=db_a.id)
                db.add(db_m)
                is_new_or_changed = True
            else:
                if db_m.home_score != m_data["home_score"] or db_m.away_score != m_data["away_score"]:
                    is_new_or_changed = True

            db_m.match_date = m_data["date"]
            db_m.stadium = m_data["stadium"]
            if m_data["home_score"] is not None:
                db_m.home_score = m_data["home_score"]
                db_m.away_score = m_data["away_score"]
                db_m.status = MatchStatus.FINISHED
            await db.flush()
            db_matches_by_jid[jm_id] = db_m

            if is_new_or_changed:
                affected_tournament_ids.add(db_t.id)

            if jm_id in match_details:
                # Сохраняем составы и фото
                for p in match_details[jm_id]["protocol"]:
                    team_id = db_m.home_team_id if p["side"] == 0 else db_m.away_team_id
                    player = db_players_by_jid.get(p["pid"])

                    if not player:
                        clean = re.sub(r'[\d\W]+', ' ', p["name"]).strip().split()
                        player = Player(jliga_id=p["pid"], last_name=clean[0] if clean else "Unknown",
                                        first_name=clean[1] if len(clean) > 1 else "")
                        db.add(player)
                        await db.flush()
                        db_players_by_jid[p["pid"]] = player

                    # Обновляем фото, если его не было или появилось новое
                    if p.get("photo_url") and player.photo_url != p["photo_url"]:
                        player.photo_url = p["photo_url"]

                    tp_key = (player.id, team_id, db_t.id)
                    if tp_key not in db_team_players_set:
                        db.add(TeamPlayer(player_id=player.id, team_id=team_id, tournament_id=db_t.id,
                                          jersey_number=p["jersey"], is_active=True))
                        db_team_players_set.add(tp_key)
                        tp_lookup[(player.id, db_t.id)] = team_id

                # Сохраняем события
                for ev in match_details[jm_id]["events"]:
                    player = db_players_by_jid.get(ev["pid"]) if ev["pid"] else None
                    if not player and ev.get("name"):
                        clean = re.sub(r'[\d\W]+', ' ', ev["name"]).strip().split()
                        player = Player(jliga_id=ev["pid"] if ev["pid"] else None,
                                        last_name=clean[0] if clean else "Unknown",
                                        first_name=clean[1] if len(clean) > 1 else "")
                        db.add(player)
                        await db.flush()
                        if ev["pid"]: db_players_by_jid[ev["pid"]] = player

                    team_id = db_m.home_team_id if ev["is_home"] else db_m.away_team_id

                    ev_key = (db_m.id, player.id if player else None, ev["minute"], ev["type"])
                    if ev_key not in db_match_events:
                        db.add(MatchEvent(match_id=db_m.id, player_id=player.id if player else None, team_id=team_id,
                                          event_type=ev["type"], minute=ev["minute"]))
                        db_match_events.add(ev_key)

        print("💾 Коммит изменений...")
        await db.commit()

        if affected_tournament_ids:
            print(f"📊 Обновление турнирных таблиц (затронуто: {len(affected_tournament_ids)})...")
            for t_id in affected_tournament_ids:
                await StandingsService.recalculate_standings(db, t_id)
                print(f"  ✔ Турнир ID {t_id} пересчитан.")

        print("✅ УСПЕХ!")


async def main():
    async with session_factory() as db:
        teams = (await db.scalars(select(Team).where(Team.jliga_id.is_not(None)))).all()
        teams_jliga_ids = [t.jliga_id for t in teams]

    if not teams_jliga_ids:
        print("❌ В БД нет команд с jliga_id.")
        return

    print(f"🚀 СТАРТ: {len(teams_jliga_ids)} команд x {len(SEASONS)} сезонов")

    unique_matches, match_details = await collect_all_data(teams_jliga_ids)
    await save_to_db(unique_matches, match_details)

    print("🎉 ВСЁ ГОТОВО! База данных полностью синхронизирована.")


if __name__ == "__main__":
    asyncio.run(main())


