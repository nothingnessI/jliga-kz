import os

os.environ["DB_HOST"] = "localhost"
os.environ["DB_PORT"] = "5432"
os.environ["DB_USER"] = "postgres"
os.environ["DB_PASSWORD"] = "1234"
os.environ["DB_NAME"] = "jliga-kz"
os.environ.setdefault("SECRET_KEY", "parser-secret")
os.environ.setdefault("APP_NAME", "J-Liga")
os.environ.setdefault("APP_HOST", "0.0.0.0")
os.environ.setdefault("APP_PORT", "8000")
os.environ.setdefault("ACCESS_TOKEN_EXPIRE_MINUTES", "60")

import asyncio
import re
import random
from curl_cffi.requests import AsyncSession as CurlSession
from bs4 import BeautifulSoup
from sqlalchemy import select
from app.db.session import session_factory
from app.models.match import Match
from app.models.tournament import Tournament
from app.models.enums import MatchStage

# Все ID сезонов
SEASONS = {
    "2026": "10129829", "2025": "10128371", "2024": "10123964", "2023": "10121263",
    "2022": "10118362", "2021": "1008904", "2020": "1004684", "2019": "1003111"
}


def parse_stage(text):
    """Определяет стадию и номер раунда по тексту с сайта."""
    rt = text.lower().strip()
    if "1/8" in rt: return MatchStage.PLAYOFF, 1
    if "1/4" in rt or "четвертьфинал" in rt: return MatchStage.PLAYOFF, 2
    if "1/2" in rt or "полуфинал" in rt: return MatchStage.PLAYOFF, 3
    if "финал" in rt and "1/" not in rt: return MatchStage.FINAL, 4
    if "3 место" in rt or "за 3" in rt: return MatchStage.PLAYOFF, 5
    if "тур" in rt:
        m = re.search(r'(\d+)', rt)
        return MatchStage.GROUP, int(m.group(1)) if m else 1
    return MatchStage.GROUP, 1


async def main():
    print("🚀 ИСПРАВЛЕНИЕ СЕТОК ПЛЕЙ-ОФФ ДЛЯ ВСЕХ КУБКОВ (ВСЕ СЕЗОНЫ)...")

    async with session_factory() as db:
        async with CurlSession(impersonate="chrome120") as client:
            for season_name, season_id in SEASONS.items():
                print(f"\n📅 Сезон {season_name}...")

                # 1. Получаем список турниров сезона
                try:
                    r = await client.get(f"https://jliga.ru/tournaments?season_id={season_id}", timeout=15)
                    if r.status_code in (403, 429, 468):
                        print("⛔ БАН! Смени IP и запусти снова.")
                        return
                    soup = BeautifulSoup(r.text, 'lxml')
                except Exception as e:
                    print(f"⚠️ Ошибка загрузки: {e}")
                    continue

                # 2. Отбираем ТОЛЬКО кубки
                cup_tours = []
                for a in soup.select('a[href^="/tournament/"]'):
                    m = re.search(r'/tournament/(\d+)', a['href'])
                    if not m: continue
                    t_id = int(m.group(1))

                    # Проверяем название
                    t_name = a.text.strip().upper()
                    if any(w in t_name for w in ["CUP", "КУБОК", "CHAMPIONS", "SUPERCUP", "KUBOGY"]):
                        # Проверяем, есть ли турнир в базе
                        tour = (await db.execute(
                            select(Tournament).where(Tournament.jliga_id == t_id))).scalar_one_or_none()
                        if tour:
                            cup_tours.append((t_id, tour.name))

                print(f"   Найдено кубковых турниров в БД: {len(cup_tours)}")

                for t_id, t_name in cup_tours:
                    print(f"   🏆 {t_name}")

                    try:
                        cal_r = await client.get(f"https://jliga.ru/tournament/{t_id}/calendar", timeout=15)
                        if cal_r.status_code == 200:
                            cal_soup = BeautifulSoup(cal_r.text, 'lxml')

                            # Ищем все доступные раунды (group, playoff, final)
                            round_ids = set(re.findall(r'round_id=(\d+)', cal_r.text))
                            urls = [f"https://jliga.ru/tournament/{t_id}/calendar"] + [
                                f"https://jliga.ru/tournament/{t_id}/calendar?round_id={rid}&type=dates" for rid in
                                round_ids]

                            updated = 0
                            for url in list(set(urls)):
                                await asyncio.sleep(random.uniform(0.3, 0.6))
                                m_r = await client.get(url, timeout=15)
                                if m_r.status_code in (403, 429, 468):
                                    print("   ⛔ БАН! Смени IP.")
                                    return

                                m_soup = BeautifulSoup(m_r.text, 'lxml')
                                for item in m_soup.select('li.blk-tcalendar__item'):
                                    sl = item.select_one('a.blk-tcalendar__score')
                                    if not sl: continue

                                    m_match = re.search(r'/match/(\d+)', sl.get('href', ''))
                                    if not m_match: continue
                                    m_jid = int(m_match.group(1))

                                    round_span = item.select_one('span.blk-tcalendar__round')
                                    if not round_span: continue

                                    exact_text = round_span.text.strip()
                                    stage, rnum = parse_stage(exact_text)

                                    # Обновляем матч
                                    match = (await db.execute(
                                        select(Match).where(Match.jliga_id == m_jid))).scalar_one_or_none()
                                    if match:
                                        match.stage = stage
                                        match.round = rnum
                                        match.stage_name = exact_text
                                        updated += 1

                            await db.commit()
                            if updated > 0:
                                print(f"      ✅ Обновлено матчей: {updated}")

                    except Exception as e:
                        print(f"      ⚠️ Ошибка: {e}")
                        await db.rollback()

    print("\n🎉 ГОТОВО! Сетки плей-офф восстановлены во всех кубках. Чемпионаты не тронуты.")


if __name__ == "__main__":
    asyncio.run(main())