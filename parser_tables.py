import asyncio
import re
from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession
from sqlalchemy import select
from app.db.session import session_factory
from app.models.team import Team
from app.models.player import Player
from app.models.team_player import TeamPlayer
from app.models.tournament_team import TournamentTeam
from app.models.enums import PlayerPosition

CONCURRENT_TASKS = 3

# Маппинг русских позиций в наши Enum
POSITION_MAP = {
    "вратарь": PlayerPosition.GOALKEEPER,
    "защитник": PlayerPosition.DEFENDER,
    "полузащитник": PlayerPosition.MIDFIELDER,
    "нападающий": PlayerPosition.FORWARD,
}


async def process_roster(t_id: int, team: Team, http_session: AsyncSession):
    # Заходим на страницу заявки конкретной команды в конкретном турнире
    url = f"https://jliga.ru/tournament/{t_id}/teams/application?team_id={team.jliga_id}"

    try:
        resp = await http_session.get(url, impersonate="chrome110", timeout=15)
        if resp.status_code != 200:
            return

        soup = BeautifulSoup(resp.text, "html.parser")

        # Ищем ссылки на всех игроков
        player_links = soup.find_all("a", href=re.compile(r"/player/\d+"))

        # Множество для хранения ID активных игроков на сайте
        active_player_ids = set()

        for link in player_links:
            # ID игрока
            p_id = int(link["href"].split("/")[-1])

            # Проверяем, в каком блоке находится игрок (Текущие или Ушедшие)
            # Обычно ушедшие игроки лежат в блоке с классом типа *inactive* или *left*
            parent_block = link.find_parent("div", class_=re.compile(r"inactive|left|retired", re.IGNORECASE))

            # Если родитель содержит "ушедш", значит игрок неактивен
            is_active = True
            if parent_block or "ушедш" in str(link.find_parent("div")).lower():
                is_active = False

            if is_active:
                active_player_ids.add(p_id)

            # Ищем амплуа (позицию)
            position = None
            # Позиция обычно лежит текстом рядом с именем (например, "Полузащитник")
            text_blocks = link.find_all(string=True)
            for txt in text_blocks:
                clean_txt = txt.strip().lower()
                if clean_txt in POSITION_MAP:
                    position = POSITION_MAP[clean_txt]
                    break

            # Обновляем в БД
            async with session_factory() as db:
                # 1. Обновляем позицию игрока глобально (если нашли)
                if position:
                    player = await db.scalar(select(Player).where(Player.jliga_id == p_id))
                    if player:
                        player.position = position
                        db.add(player)

                # 2. Обновляем статус в заявке
                team_db = await db.scalar(select(Team).where(Team.jliga_id == team.jliga_id))
                if team_db:
                    tp = await db.scalar(select(TeamPlayer).where(
                        TeamPlayer.tournament_id == t_id,
                        TeamPlayer.team_id == team_db.id,
                        TeamPlayer.player_id == (await db.scalar(select(Player.id).where(Player.jliga_id == p_id)))
                    ))
                    if tp:
                        tp.is_active = is_active
                        db.add(tp)

                await db.commit()

        # 3. Обработка тех, кого нет на странице вообще (отзаявлены давно)
        async with session_factory() as db:
            team_db = await db.scalar(select(Team).where(Team.jliga_id == team.jliga_id))
            if team_db:
                # Берем всех игроков в БД, которые привязаны к этой команде в этом турнире
                tp_list = (await db.execute(select(TeamPlayer).where(
                    TeamPlayer.tournament_id == t_id,
                    TeamPlayer.team_id == team_db.id
                ))).scalars().all()

                for tp in tp_list:
                    # Если игрок был активен, но его нет на сайте в текущем списке — делаем неактивным
                    player = await db.scalar(select(Player).where(Player.id == tp.player_id))
                    if player and player.jliga_id not in active_player_ids:
                        if tp.is_active:
                            tp.is_active = False
                            db.add(tp)
                await db.commit()

        print(f"✅ Заявка обновлена: {team.name} (Турнир {t_id})")

    except Exception as e:
        print(f"❌ Ошибка {team.name}: {e}")
    finally:
        await asyncio.sleep(0.2)


async def worker(queue: asyncio.Queue, http_session: AsyncSession):
    while True:
        try:
            t_id, team = queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        await process_roster(t_id, team, http_session)
        queue.task_done()


async def main():
    print("🚀 Старт синхронизации заявок (Текущие / Ушедшие)...")

    async with session_factory() as db:
        # Берем все связки турнир-команда
        tt_list = (await db.execute(select(TournamentTeam))).scalars().all()

    queue = asyncio.Queue()
    for tt in tt_list:
        async with session_factory() as db:
            team = await db.get(Team, tt.team_id)
            if team and team.jliga_id:
                queue.put_nowait((tt.tournament_id, team))

    print(f"Найдено заявок для обработки: {queue.qsize()}\n")

    http_sessions = [AsyncSession() for _ in range(CONCURRENT_TASKS)]
    tasks = [asyncio.create_task(worker(queue, s)) for s in http_sessions]

    await asyncio.gather(*tasks)

    for s in http_sessions:
        await s.close()

    print("\n🎉 ГОТОВО!")


if __name__ == "__main__":
    asyncio.run(main())