import asyncio
import os
import random
from curl_cffi.requests import AsyncSession
from bs4 import BeautifulSoup

BASE_URL = "https://jliga.ru"
SEASON_ID = "10129829"  # Берем 2026 год для примера


async def main():
    async with AsyncSession() as session:
        # 1. Заходим на страницу сезона
        print(f"1. Открываем сезон: {BASE_URL}/tournament/{SEASON_ID}")
        resp = await session.get(f"{BASE_URL}/tournament/{SEASON_ID}", impersonate="chrome110")
        await asyncio.sleep(random.uniform(1.5, 3.0))  # Задержка, чтобы не забанили

        if resp.status_code != 200:
            print(f"❌ Ошибка {resp.status_code}. Возможно, ID сезона неверный.")
            return

        soup = BeautifulSoup(resp.text, "html.parser")

        # Ищем ссылки на турниры (уникальные)
        tournament_links = set()
        for a in soup.find_all("a", href=True):
            if "/tournament/" in a["href"]:
                # Извлекаем ID турнира
                t_id = a["href"].split("/tournament/")[-1].split("?")[0].split("/")[0]
                if t_id.isdigit() and t_id != SEASON_ID:
                    tournament_links.add(t_id)

        print(f"✅ Найдено уникальных турниров: {len(tournament_links)}")
        if not tournament_links:
            print("Ссылки на турниры не найдены. Проверь HTML сезона.")
            return

        # Берем первый попавшийся турнир
        test_tournament_id = list(tournament_links)[0]
        print(f"2. Открываем календарь турнира: {test_tournament_id}")

        # Скачиваем страницу календаря (или главную страницу турнира)
        t_resp = await session.get(f"{BASE_URL}/tournament/{test_tournament_id}/calendar", impersonate="chrome110")
        await asyncio.sleep(random.uniform(1.5, 3.0))

        if t_resp.status_code != 200:
            print(f"❌ Ошибка {t_resp.status_code} при загрузке календаря. Пробуем главную страницу турнира.")
            t_resp = await session.get(f"{BASE_URL}/tournament/{test_tournament_id}", impersonate="chrome110")
            await asyncio.sleep(random.uniform(1.5, 3.0))

        t_soup = BeautifulSoup(t_resp.text, "html.parser")

        # Ищем ссылки на матчи
        match_links = set()
        for a in t_soup.find_all("a", href=True):
            if "/match/" in a["href"]:
                m_id = a["href"].split("/match/")[-1].split("?")[0].split("/")[0]
                if m_id.isdigit():
                    match_links.add(m_id)

        print(f"✅ Найдено уникальных матчей в турнире: {len(match_links)}")
        if not match_links:
            print("Матчи не найдены.")
            return

        # Берем один матч
        test_match_id = list(match_links)[0]
        print(f"3. Скачиваем матч: {test_match_id}")

        m_resp = await session.get(f"{BASE_URL}/match/{test_match_id}", impersonate="chrome110")

        if m_resp.status_code == 200:
            # Сохраняем HTML в файл
            with open("match_example.html", "w", encoding="utf-8") as f:
                f.write(m_resp.text)
            print("✅ HTML матча успешно сохранен в файл `match_example.html`!")

            # Выводим кусок HTML для анализа (например, заголовок и таблицу состава)
            print("\n--- Фрагмент HTML для анализа ---")
            # Ищем блок с протоколом (обычно это таблица или div с классом)
            protocol_block = m_soup.find("table", class_="table") or m_soup.find("div", id="protocol")
            if protocol_block:
                print(protocol_block.prettify()[:1500])
            else:
                print("Не удалось автоматически найти таблицу. Открой файл `match_example.html` в VS Code.")
        else:
            print(f"❌ Ошибка {m_resp.status_code} при скачивании матча.")


if __name__ == "__main__":
    asyncio.run(main())