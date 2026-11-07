import asyncio
import random
import re
import time
from bs4 import BeautifulSoup
from curl_cffi import requests
from sqlalchemy import select

from app.db.session import session_factory
from app.models.tournament import Tournament
from app.models.match import Match
from app.models.enums import MatchStage, MatchStatus


def safe_get(session, url):
    try:
        r = session.get(url, timeout=25)
        if r.status_code == 200: return r
    except Exception:
        pass
    return None


async def main():
    async with session_factory() as db:
        session = requests.Session(impersonate="chrome124")
        tournaments = (await db.scalars(select(Tournament).where(Tournament.jliga_id.is_not(None)))).all()
        print(f"📡 Парсим кубковые стадии и пенальти для {len(tournaments)} турниров...")

        for idx, t in enumerate(tournaments):
            url = f"https://jliga.ru/tournament/{t.jliga_id}/calendar?type=dates"
            r = safe_get(session, url)
            if not r: continue

            soup = BeautifulSoup(r.text, "html.parser")
            items = soup.select(".blk-tcalendar__item")

            updated = 0
            for item in items:
                score_link = item.select_one("a.blk-tcalendar__score")
                if not score_link: continue

                m_id_match = re.search(r"/match/(\d+)", score_link.get("href", ""))
                if not m_id_match: continue
                jm_id = int(m_id_match.group(1))

                db_m = await db.scalar(select(Match).where(Match.jliga_id == jm_id))
                if not db_m: continue

                # 1. Парсим стадию (справа в списке)
                # Обычно текст стадии лежит в конце item, или в специальном div
                stage_elem = item.select_one(".blk-tcalendar__stage-name") or item.select_one(".blk-tcalendar__stage")
                if stage_elem:
                    stage_text = stage_elem.text.strip()
                    db_m.stage_name = stage_text

                    # Определяем тип стадии для сетки
                    stage_lower = stage_text.lower()
                    if "1/8" in stage_lower:
                        db_m.round = 1; db_m.stage = MatchStage.PLAYOFF
                    elif "1/4" in stage_lower or "четверть" in stage_lower:
                        db_m.round = 2; db_m.stage = MatchStage.PLAYOFF
                    elif "1/2" in stage_lower or "полу" in stage_lower:
                        db_m.round = 3; db_m.stage = MatchStage.PLAYOFF
                    elif "финал" in stage_lower and "1/" not in stage_lower:
                        db_m.round = 4; db_m.stage = MatchStage.FINAL
                    elif "3 место" in stage_lower or "третье" in stage_lower:
                        db_m.round = 5; db_m.stage = MatchStage.FINAL

                # 2. Парсим счет и пенальти
                score_text = score_link.text.strip().replace("\n", " ")
                # Ищем основной счет: 4 - 4
                main_score = re.search(r'(\d+)\s*[-:]\s*(\d+)', score_text)
                if main_score:
                    db_m.home_score = int(main_score.group(1))
                    db_m.away_score = int(main_score.group(2))

                    # Ищем серию пенальти в скобках: (1:2)
                    pen_score = re.search(r'\((\d+):(\d+)\)', score_text)
                    if pen_score:
                        db_m.home_penalty = int(pen_score.group(1))
                        db_m.away_penalty = int(pen_score.group(2))

                    db_m.status = MatchStatus.FINISHED
                    updated += 1

            await db.commit()
            print(f"[{idx + 1}/{len(tournaments)}] {t.name} -> Обновлено: {updated}")
            time.sleep(random.uniform(1.0, 2.0))

    print("🎉 ПАРСИНГ КУБКОВ ЗАВЕРШЕН!")


if __name__ == "__main__":
    asyncio.run(main())