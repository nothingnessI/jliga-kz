import asyncio
import re
from datetime import datetime
from playwright.async_api import async_playwright
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

# Только 2022 - 2019
SEASONS = {
    "2022": "10118362", "2021": "1008904", "2020": "1004684", "2019": "1003111"
}

MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12
}


async def get_or_create_team(db, name: str) -> Team | None:
    name = name.strip()
    if not name or name.upper() == "TBA": return None
    team = await db.scalar(select(Team).where(Team.name == name))
    if team: return team
    base_slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-') or "team"
    slug = base_slug
    i = 1
    while await db.scalar(select(Team).where(Team.slug == slug)):
        slug = f"{base_slug}-{i}";
        i += 1
    team = Team(name=name, slug=slug, is_active=True)
    db.add(team)
    await db.flush()
    return team


async def get_or_create_player(db, jliga_id: int, name_text: str) -> Player:
    p = await db.scalar(select(Player).where(Player.jliga_id == jliga_id))
    if p: return p
    clean = re.sub(r'[\d\W]+', ' ', name_text).replace("BP", "").strip()
    parts = clean.split()
    p = Player(jliga_id=jliga_id, last_name=parts[0] if parts else "Unknown",
               first_name=parts[1] if len(parts) > 1 else "")
    db.add(p)
    await db.flush()
    return p


def parse_date(date_str: str, time_str: str, year: int) -> datetime | None:
    try:
        d_match = re.search(r'(\d+)\s+([а-яa-z]+)', date_str.lower())
        if not d_match: return None
        day = int(d_match.group(1))
        month_str = d_match.group(2)
        eng_months = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9,
                      "oct": 10, "nov": 11, "dec": 12}
        month = MONTHS.get(month_str) or eng_months.get(month_str[:3])
        if not month: return None
        t_match = re.search(r'(\d+):(\d+)', time_str)
        if not t_match: return datetime(year, month, day)
        hour, minute = int(t_match.group(1)), int(t_match.group(2))
        return datetime(year, month, day, hour, minute)
    except Exception:
        return None


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36")
        page = await context.new_page()
        page.set_default_timeout(20000)

        await page.route("**/*", lambda r: (
            r.abort() if any(x in r.request.url for x in
                             ["mc.yandex.ru", "usocial.pro", "google-analytics", "doubleclick"]) else r.continue_()
        ))

        async def goto_safe(url: str, retries=3) -> bool:
            for _ in range(retries):
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                    return True
                except Exception:
                    try:
                        await page.evaluate("window.stop()")
                    except:
                        pass
                    await asyncio.sleep(2)
            return False

        async with session_factory() as db:
            total_matches = 0
            total_rosters = 0
            total_events = 0

            for year_str, season_id in SEASONS.items():
                year = int(year_str)
                print(f"\n📅 Сезон {year}")
                if not await goto_safe(f"https://jliga.ru/tournaments?season_id={season_id}"):
                    continue

                t_ids = set()
                for l in BeautifulSoup(await page.content(), "html.parser").find_all("a", href=re.compile(
                        r"/tournament/\d+")):
                    m = re.search(r"/tournament/(\d+)", l.get("href"))
                    if m: t_ids.add(m.group(1))

                for t_id in t_ids:
                    if not await goto_safe(f"https://jliga.ru/tournament/{t_id}/calendar?type=tours"):
                        continue

                    cal = BeautifulSoup(await page.content(), "html.parser")
                    t_title = cal.find("h2", class_="blk-tournamentheader__title")
                    if not t_title: continue

                    t_name = t_title.text.strip()

                    # ==========================================
                    # ЖЕСТКАЯ ПРОВЕРКА: если года нет в названии - скипаем
                    # ==========================================
                    if year_str not in t_name:
                        continue

                    db_t = await db.scalar(
                        select(Tournament).where(Tournament.name == t_name, Tournament.season == year_str))
                    if not db_t:
                        db_t = Tournament(name=t_name, season=year_str, division="UNKNOWN", age_category="UNKNOWN",
                                          status=TournamentStatus.FINISHED, max_teams=64, logo_url="")
                        db.add(db_t)
                        await db.flush()
                        print(f"  🆕 Турнир: {t_name}")

                    round_units = cal.find_all("div", class_="blk-tcalendar__unit")
                    print(f"  🏆 {t_name}: {len(round_units)} туров")

                    for r_unit in round_units:
                        r_head = r_unit.find(class_="blk-tcalendar__head-text")
                        round_num = 1
                        if r_head:
                            r_match = re.search(r'(\d+)', r_head.text)
                            if r_match: round_num = int(r_match.group(1))

                        match_items = r_unit.find_all("li", class_="blk-tcalendar__item")
                        for item in match_items:
                            score_link = item.find("a", class_="blk-tcalendar__score")
                            if not score_link: continue

                            href = score_link.get("href") or ""
                            m_id_match = re.search(r"/match/(\d+)", href)
                            if not m_id_match: continue
                            jm_id = int(m_id_match.group(1))

                            if not await goto_safe(f"https://jliga.ru/match/{jm_id}"):
                                continue

                            try:
                                ms = BeautifulSoup(await page.content(), "html.parser")

                                date_elem = ms.find("li", class_="blk-matchheader__date")
                                time_elem = ms.find("li", class_="blk-matchheader__time")
                                stadium_elem = ms.find("div", class_="blk-matchheader__stadium")
                                team_names = ms.select(".blk-matchheader__team-name")
                                score_items = ms.select(".blk-matchheader__score-item")

                                if len(team_names) < 2: continue
                                h_name = team_names[0].text.strip()
                                a_name = team_names[1].text.strip()

                                db_h = await get_or_create_team(db, h_name)
                                db_a = await get_or_create_team(db, a_name)
                                if not db_h or not db_a: continue

                                home_score, away_score = None, None
                                if len(score_items) >= 3:
                                    h_s = score_items[0].text.strip()
                                    a_s = score_items[2].text.strip()
                                    if h_s.isdigit() and a_s.isdigit():
                                        home_score, away_score = int(h_s), int(a_s)

                                m_date = None
                                if date_elem and time_elem:
                                    m_date = parse_date(date_elem.text.strip(), time_elem.text.strip(), year)
                                m_stadium = stadium_elem.text.strip() if stadium_elem else None

                                db_m = await db.scalar(select(Match).where(Match.jliga_id == jm_id))
                                if not db_m:
                                    db_m = Match(jliga_id=jm_id, tournament_id=db_t.id, home_team_id=db_h.id,
                                                 away_team_id=db_a.id)
                                    db.add(db_m)
                                    total_matches += 1

                                db_m.match_date = m_date
                                db_m.stadium = m_stadium
                                db_m.home_score = home_score
                                db_m.away_score = away_score
                                db_m.round = round_num
                                if home_score is not None: db_m.status = MatchStatus.FINISHED
                                await db.flush()

                                # --- РОСТЕР ---
                                await goto_safe(f"https://jliga.ru/match/{jm_id}?active=protocol")
                                ps = BeautifulSoup(await page.content(), "html.parser")
                                units = ps.find_all("div", class_="protocol__unit")

                                if len(units) >= 2:
                                    for idx, team_id in [(0, db_m.home_team_id), (1, db_m.away_team_id)]:
                                        for p_item in units[idx].find_all("li", class_="protocol__item"):
                                            link = p_item.find("a", class_="protocol__link")
                                            if not link: continue
                                            pm = re.search(r"/player/(\d+)", link.get("href"))
                                            if not pm: continue
                                            pid = int(pm.group(1))

                                            name_div = p_item.find("div", class_="protocol__name")
                                            name_text = name_div.text.strip() if name_div else ""

                                            num_div = p_item.find("div", class_="protocol__number")
                                            num_text = num_div.text.strip() if num_div else ""
                                            jersey = int(num_text) if num_text.isdigit() else None

                                            player = await get_or_create_player(db, pid, name_text)
                                            exists = await db.scalar(select(TeamPlayer).where(
                                                TeamPlayer.player_id == player.id,
                                                TeamPlayer.team_id == team_id,
                                                TeamPlayer.tournament_id == db_t.id
                                            ))
                                            if not exists:
                                                db.add(TeamPlayer(player_id=player.id, team_id=team_id,
                                                                  tournament_id=db_t.id, jersey_number=jersey,
                                                                  is_active=True))
                                                total_rosters += 1

                                # --- СОБЫТИЯ ---
                                await goto_safe(f"https://jliga.ru/match/{jm_id}?active=events")
                                await page.wait_for_timeout(1500)
                                es = BeautifulSoup(await page.content(), "html.parser")

                                timeline_items = es.select(".timeline__item")
                                match_events_count = 0

                                for ev_item in timeline_items:
                                    minute_div = ev_item.select_one(".timeline__minute-text")
                                    if not minute_div: continue
                                    minute_match = re.search(r"(\d+)", minute_div.text)
                                    if not minute_match: continue
                                    minute = int(minute_match.group(1))

                                    name_a = ev_item.select_one(".timeline__name")
                                    if not name_a or "href" not in name_a.attrs: continue
                                    pid_match = re.search(r"/player/(\d+)", name_a["href"])
                                    if not pid_match: continue
                                    pid = int(pid_match.group(1))

                                    icon_svg = ev_item.select_one(".timeline__icon svg")
                                    e_type = MatchEventType.GOAL
                                    is_valid = True
                                    if icon_svg:
                                        classes = icon_svg.get("class", [])
                                        if "yellow-card" in classes:
                                            e_type = MatchEventType.YELLOW_CARD
                                        elif "red-card" in classes:
                                            e_type = MatchEventType.RED_CARD
                                        elif "goal" in classes:
                                            e_type = MatchEventType.GOAL
                                        else:
                                            is_valid = False

                                    if not is_valid: continue

                                    player = await db.scalar(select(Player).where(Player.jliga_id == pid))
                                    if not player: continue

                                    tp = await db.scalar(select(TeamPlayer).where(TeamPlayer.player_id == player.id,
                                                                                  TeamPlayer.tournament_id == db_t.id))
                                    team_id = tp.team_id if tp else db_m.home_team_id

                                    existing = await db.scalar(select(MatchEvent).where(
                                        MatchEvent.match_id == db_m.id,
                                        MatchEvent.player_id == player.id,
                                        MatchEvent.minute == minute,
                                        MatchEvent.event_type == e_type
                                    ))
                                    if not existing:
                                        db.add(MatchEvent(match_id=db_m.id, player_id=player.id, team_id=team_id,
                                                          event_type=e_type, minute=minute))
                                        total_events += 1
                                        match_events_count += 1

                                await db.commit()
                                print(
                                    f"    ✅ Т{round_num} | {h_name} {home_score}:{away_score} {a_name} | Событий: {match_events_count}")

                            except Exception as e:
                                await db.rollback()
                                print(f"    ❌ Ошибка при парсинге матча {jm_id}: {e}")

                            await asyncio.sleep(0.3)

        await browser.close()
        print(f"\n🎉 ВОССТАНОВЛЕНИЕ ЗАВЕРШЕНО!")
        print(f"   Матчей: {total_matches}")
        print(f"   Заявок: {total_rosters}")
        print(f"   Событий: {total_events}")


if __name__ == "__main__":
    asyncio.run(main())