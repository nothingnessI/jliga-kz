import asyncio
import re
from curl_cffi.requests import AsyncSession
from bs4 import BeautifulSoup
from sqlalchemy import select, delete
from app.db.session import session_factory
from app.models.match import Match
from app.models.match_event import MatchEvent
from app.models.player import Player
from app.models.enums import MatchStatus, MatchEventType
from app.services.stats_recalc import StatsRecalculator

BASE_URL = "https://jliga.ru"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Referer": "https://jliga.ru/",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


async def fetch(session: AsyncSession, url: str):
    try:
        resp = await session.get(
            url,
            headers=HEADERS,
            impersonate="chrome110",
            timeout=30,
            allow_redirects=True
        )
        print(f"🔗 {url} -> HTTP {resp.status_code} (размер: {len(resp.text)} символов)")

        if resp.status_code == 200:
            return resp.text
        elif resp.status_code == 403:
            print("  ⚠️ 403 Forbidden. Возможно, Cloudflare блокирует. Попробуйте 'chrome124'.")
    except Exception as e:
        print(f"❌ Ошибка сети {url}: {e}")
    return None


async def main():
    print("🚀 Старт синхронизации матчей за сегодня...")

    # Перебор нескольких вариантов impersonate, если один заблокирован
    for impersonate_version in ["chrome110", "chrome120", "chrome124"]:
        print(f"\n🔍 Пробуем с impersonate={impersonate_version}...")
        async with AsyncSession() as session:
            try:
                resp = await session.get(
                    f"{BASE_URL}/matches?date_range=today",
                    headers=HEADERS,
                    impersonate=impersonate_version,
                    timeout=30,
                    allow_redirects=True
                )
                print(f"   Статус: {resp.status_code}, Размер: {len(resp.text)} символов")

                if resp.status_code == 200 and len(resp.text) > 5000:
                    print(f"   ✅ Успешно! Используем {impersonate_version}")
                    html = resp.text
                    break
            except Exception as e:
                print(f"   ❌ Ошибка: {e}")
    else:
        print("\n❌ Все варианты заблокированы. Сайт jliga.ru защищает контент.")
        return

    # Парсим список матчей
    soup = BeautifulSoup(html, "html.parser")
    match_links = set()
    for a in soup.find_all("a", href=re.compile(r'/match/\d+')):
        m = re.search(r'/match/(\d+)', a['href'])
        if m:
            match_links.add(int(m.group(1)))

    print(f"\n📅 Найдено матчей: {len(match_links)}")
    if not match_links:
        print("⚠️ На странице нет ссылок на матчи. Возможно, сегодня матчей нет или изменился HTML.")
        return

    # Обработка каждого матча
    async with AsyncSession() as session:
        for mid in match_links:
            print(f"\n🔄 Матч {mid}")
            html_match = await fetch(session, f"{BASE_URL}/match/{mid}")
            if not html_match:
                continue

            soup_m = BeautifulSoup(html_match, "html.parser")

            # Ищем счет
            score_text = None
            for tag in soup_m.find_all(string=re.compile(r'^\s*\d+\s*:\s*\d+\s*$')):
                score_text = tag.strip()
                break

            async with session_factory() as db:
                match = await db.scalar(select(Match).where(Match.jliga_id == mid))
                if not match:
                    print(f"  ⚠️ Матч {mid} не в БД, пропускаем.")
                    continue

                if score_text:
                    try:
                        h, a = map(int, score_text.split(':'))
                        match.home_score = h
                        match.away_score = a
                        match.status = MatchStatus.FINISHED
                        print(f"  ⚽ Счет: {h}:{a}")
                    except ValueError:
                        pass

                await db.commit()
                print(f"  ✅ Матч {mid} обновлен.")

                # Пересчет (только для завершенных)
                if match.status == MatchStatus.FINISHED:
                    await StatsRecalculator.recalculate_all(db, match.tournament_id)
                    print(f"  📊 Таблица турнира {match.tournament_id} пересчитана.")


if __name__ == "__main__":
    asyncio.run(main())