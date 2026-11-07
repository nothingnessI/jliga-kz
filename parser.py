import asyncio
import random
import re
import time
from datetime import datetime
from bs4 import BeautifulSoup
from curl_cffi import requests  # <--- НАША ГЛАВНАЯ БИБЛИОТЕКА
from sqlalchemy import select

from app.db.session import session_factory
from app.models.match import Match
from app.models.tournament import Tournament
from app.models.enums import MatchStatus

MONTHS = {
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "май": 5, "июн": 6,
    "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12
}


def parse_date_string(raw_text: str, default_year: int):
    raw_text = raw_text.lower().strip()
    hour, minute = 0, 0

    time_match = re.search(r'(\d{1,2}):(\d{2})', raw_text)
    if time_match:
        hour, minute = int(time_match.group(1)), int(time_match.group(2))

    # Формат DD.MM.YYYY
    date_dot_match = re.search(r'(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?', raw_text)
    if date_dot_match:
        day = int(date_dot_match.group(1))
        month = int(date_dot_match.group(2))
        year = default_year
        if date_dot_match.group(3):
            y_str = date_dot_match.group(3)
            year = int(y_str) if len(y_str) == 4 else 2000 + int(y_str)
        return datetime(year, month, day, hour, minute)

    # Текстовый формат "27 сентября"
    text_without_time = re.sub(r'\d{1,2}:\d{2}', '', raw_text)
    date_text_match = re.search(r'(\d{1,2})\s+([а-яa-z]+)', text_without_time)
    if date_text_match:
        day = int(date_text_match.group(1))
        month_str = date_text_match.group(2)[:3]
        month = MONTHS.get(month_str)
        if month:
            return datetime(default_year, month, day, hour, minute)

    return None


async def main():
    async with session_factory() as db:
        # 1. Получаем все турниры
        tournaments = (await db.scalars(select(Tournament))).all()
        print(f"📡 Найдено {len(tournaments)} турниров. Начинаем STEALTH-парсинг...\n")

        # 2. Создаем сессию curl_cffi (она сохраняет куки, как реальный браузер)
        # impersonate="chrome110" подделывает отпечаток Chrome 110
        session = requests.Session(impersonate="chrome110")

        for idx, t in enumerate(tournaments):
            print(f"[{idx + 1}/{len(tournaments)}] {t.name} (Сезон {t.season})")
            url = f"https://jliga.ru/tournament/{t.id}/calendar"

            try:
                # Делаем запрос, притворяясь браузером
                resp = session.get(url, timeout=30.0)

                if resp.status_code != 200:
                    print(f"  ❌ Ошибка {resp.status_code}. Возможно, бан. Ждем 15 сек...")
                    time.sleep(15)
                    continue

                soup = BeautifulSoup(resp.text, "html.parser")
                items = soup.select("li.blk-tcalendar__item")

                updated_count = 0

                for item in items:
                    score_link = item.find("a", class_="blk-tcalendar__score")
                    if not score_link: continue

                    m_id_match = re.search(r"/match/(\d+)", score_link.get("href", ""))
                    if not m_id_match: continue
                    jm_id = int(m_id_match.group(1))

                    # Дата
                    raw_date_text = item.text.strip()
                    parsed_date = parse_date_string(raw_date_text, int(t.season))

                    # Счет
                    score_text = score_link.text.strip()
                    score_match = re.search(r'(\d+)\s*[-:]\s*(\d+)', score_text)

                    # Ищем матч в базе
                    db_m = await db.scalar(select(Match).where(Match.jliga_id == jm_id))
                    if not db_m: continue

                    # Обновляем дату
                    if parsed_date:
                        db_m.match_date = parsed_date

                    # Обновляем счет и статус
                    if score_match:
                        db_m.home_score = int(score_match.group(1))
                        db_m.away_score = int(score_match.group(2))
                        db_m.status = MatchStatus.FINISHED
                    else:
                        db_m.status = MatchStatus.SCHEDULED

                    updated_count += 1

                await db.commit()
                print(f"  ✅ Обновлено матчей: {updated_count}")

            except Exception as e:
                print(f"  ❌ Ошибка: {e}")

            # 3. Человекоподобная пауза перед следующим турниром (4-8 секунд)
            sleep_time = random.uniform(4.0, 8.0)
            print(f"  💤 Отдыхаем {sleep_time:.1f} сек...\n")
            time.sleep(sleep_time)

        print("🎉 STEALTH-ПАРСИНГ ЗАВЕРШЕН!")


if __name__ == "__main__":
    asyncio.run(main())