import asyncio
import re
import requests
from bs4 import BeautifulSoup
from sqlalchemy import select, delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from app.db.session import session_factory
from app.models.match import Match
from app.models.team import Team
from app.models.player import Player
from app.models.team_player import TeamPlayer
from app.models.match_event import MatchEvent
from app.models.enums import MatchEventType

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


async def get_or_create_player(db, jliga_id: int, name_text: str) -> Player:
    with db.no_autoflush:
        p = await db.scalar(select(Player).where(Player.jliga_id == jliga_id))
        if p: return p

        clean = re.sub(r'[\d\W]+', ' ', name_text).replace("BP", "").strip()
        parts = clean.split()
        last_name = parts[0] if parts else "Unknown"
        first_name = parts[1] if len(parts) > 1 else ""

        p = Player(jliga_id=jliga_id, last_name=last_name, first_name=first_name)
        db.add(p)
        await db.flush()  # Даем ID игроку
    return p


async def main():
    async with session_factory() as db:
        # Берем только ID матчей, чтобы не держать в памяти expired объекты
        match_ids = (await db.execute(
            select(Match.id).where(Match.jliga_id.isnot(None))
        )).scalars().all()

        print(f"⚽ Найдено матчей для обработки: {len(match_ids)}\n")

        for idx, match_id in enumerate(match_ids, 1):
            try:
                # Получаем свежий объект матча
                match = await db.get(Match, match_id)
                if not match: continue

                print(f"[{idx}/{len(match_ids)}] Матч ID {match.id} (jliga_id: {match.jliga_id})")

                # --- 1. ПАРСИМ ПРОТОКОЛ (РОСТЕР) ---
                protocol_url = f"https://jliga.ru/match/{match.jliga_id}?active=protocol"
                resp = requests.get(protocol_url, headers=HEADERS, timeout=10)
                if resp.status_code == 200:
                    soup = BeautifulSoup(resp.text, "html.parser")
                    units = soup.find_all("div", class_="protocol__unit")

                    if len(units) >= 2:
                        for idx_unit, team_id in [(0, match.home_team_id), (1, match.away_team_id)]:
                            for p_item in units[idx_unit].select(".protocol__item"):
                                link = p_item.select_one(".protocol__link")
                                if not link: continue

                                pm = re.search(r"/player/(\d+)", link.get("href", ""))
                                if not pm: continue
                                pid = int(pm.group(1))

                                name_div = p_item.select_one(".protocol__name")
                                name_text = name_div.text.strip() if name_div else ""

                                num_div = p_item.select_one(".protocol__number")
                                num_text = num_div.text.strip() if num_div else ""
                                jersey = int(num_text) if num_text.isdigit() else None

                                player = await get_or_create_player(db, pid, name_text)

                                # Вставляем через ON CONFLICT DO NOTHING
                                stmt = pg_insert(TeamPlayer).values(
                                    player_id=player.id,
                                    team_id=team_id,
                                    tournament_id=match.tournament_id,
                                    jersey_number=jersey,
                                    is_active=True
                                ).on_conflict_do_nothing(constraint='uq_team_player_tournament')
                                await db.execute(stmt)
                        await db.commit()
                        print(f"   👥 Ростер обработан.")

                # --- 2. ПАРСИМ СОБЫТИЯ (ГОЛЫ/КАРТОЧКИ) ---
                events_url = f"https://jliga.ru/match/{match.jliga_id}?active=events"
                resp = requests.get(events_url, headers=HEADERS, timeout=10)
                if resp.status_code == 200:
                    soup = BeautifulSoup(resp.text, "html.parser")

                    # Очищаем старые события для этого матча (чтобы обновить)
                    await db.execute(delete(MatchEvent).where(MatchEvent.match_id == match.id))
                    await db.commit()

                    timeline_items = soup.select(".timeline__item")
                    events_count = 0

                    for item in timeline_items:
                        minute_div = item.select_one(".timeline__minute-text")
                        if not minute_div: continue
                        minute_match = re.search(r"(\d+)", minute_div.text)
                        if not minute_match: continue
                        minute = int(minute_match.group(1))

                        name_a = item.select_one(".timeline__name")
                        if not name_a or "href" not in name_a.attrs: continue
                        pid_match = re.search(r"/player/(\d+)", name_a["href"])
                        if not pid_match: continue
                        pid = int(pid_match.group(1))

                        icon_svg = item.select_one(".timeline__icon svg")
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

                        # Определяем команду игрока
                        tp = await db.scalar(select(TeamPlayer).where(
                            TeamPlayer.player_id == player.id,
                            TeamPlayer.tournament_id == match.tournament_id
                        ))
                        team_id = tp.team_id if tp else match.home_team_id

                        # Вставляем через ON CONFLICT DO NOTHING
                        stmt = pg_insert(MatchEvent).values(
                            match_id=match.id,
                            player_id=player.id,
                            team_id=team_id,
                            event_type=e_type,
                            minute=minute
                        ).on_conflict_do_nothing(constraint='uq_match_event')
                        await db.execute(stmt)
                        events_count += 1

                    await db.commit()
                    print(f"   ⚽ Событий обработано: {events_count}")

                await asyncio.sleep(0.5)

            except Exception as e:
                await db.rollback()
                print(f"   ❌ Ошибка в матче {match_id}: {e}")
                continue

        print("\n🎉 ГОТОВО!")


if __name__ == "__main__":
    asyncio.run(main())