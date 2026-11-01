import asyncio
import re
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup
from sqlalchemy import select
from app.db.session import session_factory
from app.models.team import Team
from app.models.tournament import Tournament
from app.models.match import Match
from app.models.tournament_standing import TournamentStanding
from app.models.enums import MatchStatus, MatchStage, TournamentStatus

SEASONS = {
    "2026": "10129829", "2025": "10128371", "2024": "10123964", "2023": "10121263",
    "2022": "10118362", "2021": "1008904", "2020": "1004684", "2019": "1003111"
}


def parse_jliga_cup(html_content: str) -> dict:
    soup = BeautifulSoup(html_content, "html.parser")
    result = {"tournament_name": "", "season": "2026", "playoff": [], "groups": []}

    title_elem = soup.find("h2", class_="blk-tournamentheader__title")
    if title_elem: result["tournament_name"] = title_elem.text.strip()

    dates_elem = soup.find("li", class_="blk-tournamentheader__info-dates")
    if dates_elem:
        match = re.search(r'20\d{2}', dates_elem.text)
        if match: result["season"] = match.group(0)

    for playoff in soup.find_all("div", class_="playoff"):
        parent_section = playoff.find_parent("section", class_="tabs")
        cup_name = "Плей-офф"
        if parent_section:
            cup_title = parent_section.find("div", class_="tabs__navigation--title")
            if cup_title: cup_name = cup_title.text.strip()

        cup_data = {"cup_name": cup_name, "rounds": {}}
        for col in playoff.find_all("div", class_="playoff-col"):
            title_elem = col.find("div", class_="playoff-col-title")
            if not title_elem: continue
            round_name = title_elem.text.strip()
            matches = []

            for card in col.find_all("div", class_="playoff-match"):
                teams_td = card.find_all("td", class_="team")
                scores_td = card.find_all("td", class_="score")
                additional_td = card.find("td", class_="additional")

                if len(teams_td) >= 2 and len(scores_td) >= 2:
                    h_elem = teams_td[0].find("span", class_="team-name")
                    a_elem = teams_td[1].find("span", class_="team-name")
                    h_name = h_elem.text.strip() if h_elem else "TBA"
                    a_name = a_elem.text.strip() if a_elem else "TBA"
                    hs = scores_td[0].text.strip()
                    as_ = scores_td[1].text.strip()

                    matches.append({
                        "home_team": h_name, "away_team": a_name,
                        "home_score": int(hs) if hs.isdigit() else None,
                        "away_score": int(as_) if as_.isdigit() else None,
                        "additional": additional_td.text.strip() if additional_td else None
                    })
            if matches: cup_data["rounds"][round_name] = matches
        if cup_data["rounds"]: result["playoff"].append(cup_data)

    for table in soup.find_all("div", class_="custom-table-table"):
        parent_section = table.find_parent("section", class_="tabs")
        group_name = "Группа"
        if parent_section:
            g_title = parent_section.find("div", class_="tabs__navigation--title")
            if g_title: group_name = g_title.text.strip()

        group_teams = []
        for row in table.find_all("li", class_="custom-table__line"):
            t_name_elem = row.find("div", class_="custom-table__team-name")
            if not t_name_elem: continue
            cells = row.find_all("div", class_="custom-table__var")
            goals_str = cells[4].text.strip() if len(cells) > 4 else "0 - 0"
            goals = goals_str.split("-")

            group_teams.append({
                "team_name": t_name_elem.text.strip(),
                "played": int(cells[0].text.strip()) if len(cells) > 0 and cells[0].text.strip().isdigit() else 0,
                "wins": int(cells[1].text.strip()) if len(cells) > 1 and cells[1].text.strip().isdigit() else 0,
                "draws": int(cells[2].text.strip()) if len(cells) > 2 and cells[2].text.strip().isdigit() else 0,
                "losses": int(cells[3].text.strip()) if len(cells) > 3 and cells[3].text.strip().isdigit() else 0,
                "goals_for": int(goals[0].strip()) if len(goals) > 0 and goals[0].strip().isdigit() else 0,
                "goals_against": int(goals[1].strip()) if len(goals) > 1 and goals[1].strip().isdigit() else 0,
                "points": int(cells[5].text.strip()) if len(cells) > 5 and cells[5].text.strip().isdigit() else 0
            })
        if group_teams: result["groups"].append({"group_name": group_name, "teams": group_teams})
    return result


async def get_or_create_team(db, name: str) -> Team:
    if not name or name == "TBA": return None
    team = (await db.execute(select(Team).where(Team.name.ilike(name.strip())))).scalar_one_or_none()
    if not team:
        base_slug = re.sub(r'[^a-z0-9]', '-', name.lower()).strip('-')
        slug = base_slug
        counter = 1
        while (await db.execute(select(Team).where(Team.slug == slug))).scalar_one_or_none():
            slug = f"{base_slug}-{counter}"
            counter += 1
        team = Team(name=name.strip(), slug=slug, is_active=True)
        db.add(team)
        await db.flush()
    return team


async def save_to_db(db, parsed_data):
    t_name = parsed_data["tournament_name"]
    t_season = parsed_data["season"]
    if not t_name: return

    tournament = (await db.execute(select(Tournament).where(
        Tournament.name == t_name, Tournament.season == t_season
    ))).scalar_one_or_none()

    if not tournament:
        tournament = Tournament(
            name=t_name, season=t_season, division="CUP", age_category="ADULTS",
            status=TournamentStatus.FINISHED, max_teams=16, logo_url=""
        )
        db.add(tournament)
        await db.flush()

    for group in parsed_data["groups"]:
        for t_data in group["teams"]:
            team = await get_or_create_team(db, t_data["team_name"])
            if not team: continue
            standing = (await db.execute(select(TournamentStanding).where(
                TournamentStanding.tournament_id == tournament.id, TournamentStanding.team_id == team.id
            ))).scalar_one_or_none()
            if not standing:
                standing = TournamentStanding(tournament_id=tournament.id, team_id=team.id)
                db.add(standing)
            standing.group_name = group["group_name"]
            standing.games_played = t_data["played"]
            standing.wins = t_data["wins"]
            standing.draws = t_data["draws"]
            standing.losses = t_data["losses"]
            standing.goals_for = t_data["goals_for"]
            standing.goals_against = t_data["goals_against"]
            standing.points = t_data["points"]

    round_mapping = {"Четвертьфинал": 1, "Полуфинал": 2, "Финал": 3}
    for cup in parsed_data["playoff"]:
        for r_name, matches in cup["rounds"].items():
            round_num = round_mapping.get(r_name, 0)
            stage = MatchStage.FINAL if "Финал" in r_name else MatchStage.PLAYOFF

            for m_data in matches:
                home = await get_or_create_team(db, m_data["home_team"])
                away = await get_or_create_team(db, m_data["away_team"])
                if not home or not away: continue

                match = (await db.execute(select(Match).where(
                    Match.tournament_id == tournament.id, Match.home_team_id == home.id,
                    Match.away_team_id == away.id, Match.round == round_num
                ))).scalar_one_or_none()

                if not match:
                    match = Match(
                        tournament_id=tournament.id, home_team_id=home.id, away_team_id=away.id,
                        stage=stage, round=round_num, group_name=cup["cup_name"]  # <-- ГРУППИРОВКА КУБКА
                    )
                    db.add(match)
                else:
                    match.group_name = cup["cup_name"]  # Обновляем существующий

                match.home_score = m_data["home_score"]
                match.away_score = m_data["away_score"]
                match.status = MatchStatus.FINISHED if m_data["home_score"] is not None else MatchStatus.SCHEDULED

    await db.commit()
    print(f"   💾 [{t_season}] '{t_name}' сохранен в БД.")


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        page = await context.new_page()

        all_tournament_ids = set()
        for year, season_id in SEASONS.items():
            url = f"https://jliga.ru/tournaments?season_id={season_id}"
            print(f"📅 Загружаем сезон {year}...")
            try:
                await page.goto(url, wait_until="domcontentloaded")
                await page.wait_for_timeout(2000)
                links = await page.eval_on_selector_all("a[href^='/tournament/']",
                                                        "els => els.map(e => e.getAttribute('href'))")
                for l in links:
                    parts = l.split('/')
                    if len(parts) >= 3 and parts[1] == 'tournament' and parts[2].isdigit():
                        all_tournament_ids.add(parts[2])
            except Exception as e:
                print(f"   ❌ Ошибка: {e}")

        print(f"\n🏆 Всего турниров: {len(all_tournament_ids)}")

        async with session_factory() as db:
            for i, tid in enumerate(list(all_tournament_ids)):
                url = f"https://jliga.ru/tournament/{tid}/tables"
                print(f"[{i + 1}/{len(all_tournament_ids)}] ID {tid}...")
                try:
                    await page.goto(url, wait_until="domcontentloaded")
                    await page.wait_for_timeout(1000)
                    if await page.locator("div.playoff").count() > 0:
                        html = await page.content()
                        parsed_data = parse_jliga_cup(html)
                        if parsed_data["playoff"]:
                            await save_to_db(db, parsed_data)
                        else:
                            print(f"   ⏩ Сетка пустая.")
                    else:
                        print(f"   ⏩ Лига. Пропуск.")
                except Exception as e:
                    print(f"   ❌ Ошибка: {e}")
                    await db.rollback()

        await browser.close()
        print("\n🎉 ВСЁ ГОТОВО!")


if __name__ == "__main__":
    asyncio.run(main())