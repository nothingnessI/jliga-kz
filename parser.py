import asyncio
import re
from datetime import datetime
from playwright.async_api import async_playwright

BASE_URL = "https://jliga.ru"


def parse_date(date_str: str, time_str: str) -> datetime:
    """Парсит '17 августа' и '22:10' в datetime."""
    months = {
        "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
        "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12
    }
    try:
        day = int(date_str.split()[0])
        month_name = date_str.split()[1].lower()
        month = months[month_name]
        hour, minute = map(int, time_str.split(':'))
        year = datetime.now().year
        return datetime(year, month, day, hour, minute)
    except Exception as e:
        print(f"Ошибка парсинга даты: {e}")
        return None


async def parse_match(page, url: str):
    """Открывает страницу матча и извлекает данные."""
    print(f"Открываем {url}...")
    await page.goto(url, wait_until="networkidle")
    await page.wait_for_timeout(2000)  # Ждем, пока всё точно отрисуется

    # 1. Дата и время
    date_text = await page.locator('.blk-matchheader__date').inner_text()
    time_text = await page.locator('.blk-matchheader__time').inner_text()
    match_date = parse_date(date_text.strip(), time_text.strip())

    # 2. Стадион
    stadium_el = page.locator('.blk-matchheader__stadium')
    stadium = await stadium_el.inner_text() if await stadium_el.count() > 0 else None
    stadium = stadium.strip() if stadium else None

    # 3. Команды
    team_links = page.locator('.blk-matchheader__team-link')
    home_team_name = await team_links.nth(0).get_attribute('title')
    away_team_name = await team_links.nth(1).get_attribute('title')

    home_url = await team_links.nth(0).get_attribute('href')
    away_url = await team_links.nth(1).get_attribute('href')

    home_team_id = None
    m = re.search(r'team_id=(\d+)', home_url)
    if m: home_team_id = int(m.group(1))

    away_team_id = None
    m = re.search(r'team_id=(\d+)', away_url)
    if m: away_team_id = int(m.group(1))

    # 4. Счет
    score_items = page.locator('.blk-matchheader__score-item')
    home_score = None
    away_score = None
    if await score_items.count() >= 3:
        try:
            home_score = int((await score_items.nth(0).inner_text()).strip())
            away_score = int((await score_items.nth(2).inner_text()).strip())
        except ValueError:
            pass

    # 5. Тур
    round_el = page.locator('.blk-matchheader__tour')
    round_name = await round_el.inner_text() if await round_el.count() > 0 else None
    round_name = round_name.strip() if round_name else None

    print(f"  Дата: {match_date}")
    print(f"  Стадион: {stadium}")
    print(f"  {home_team_name} ({home_score}) - ({away_score}) {away_team_name}")
    print(f"  Тур: {round_name}")

    # 6. События
    print("\n  --- СОБЫТИЯ ---")
    events = []
    timeline_items = page.locator('.timeline__item')
    item_count = await timeline_items.count()

    for i in range(item_count):
        item = timeline_items.nth(i)
        minute = await item.locator('.timeline__minute-text').inner_text()

        # Находим все блоки событий в строке (обычно 2: левый и правый)
        ev_divs = item.locator('.timeline__event')
        ev_count = await ev_divs.count()

        for j in range(ev_count):
            ev = ev_divs.nth(j)
            icon = ev.locator('.timeline__icon')
            if await icon.count() > 0:
                title = await icon.get_attribute('title')  # "Гол", "Жёлтая карточка" и т.д.
                player_link = ev.locator('.timeline__name')
                player_name = await player_link.inner_text() if await player_link.count() > 0 else ""
                href = await player_link.get_attribute('href') if await player_link.count() > 0 else ""
                player_id = None
                if href:
                    m = re.search(r'/player/(\d+)', href)
                    if m: player_id = int(m.group(1))

                event_type = "UNKNOWN"
                if "Гол" in title:
                    event_type = "GOAL"
                elif "Жёлтая" in title:
                    event_type = "YELLOW_CARD"
                elif "Красная" in title:
                    event_type = "RED_CARD"
                elif "Замена" in title:
                    event_type = "SUBSTITUTION"

                is_home = (j == 0)  # Первый event - домашняя, второй - гостевая

                events.append({
                    "minute": minute,
                    "player_name": player_name,
                    "player_id": player_id,
                    "type": event_type,
                    "is_home": is_home
                })
                print(f"  {minute} {event_type} ({'Home' if is_home else 'Away'}) - {player_name} (ID: {player_id})")

    return {
        "url": url,
        "match_date": match_date,
        "stadium": stadium,
        "home_team": {"name": home_team_name, "id": home_team_id, "score": home_score},
        "away_team": {"name": away_team_name, "id": away_team_id, "score": away_score},
        "round": round_name,
        "events": events
    }


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)  # headless=False чтобы видеть, что происходит
        page = await browser.new_page()

        url = f"{BASE_URL}/match/5837027"
        data = await parse_match(page, url)

        print("\n✅ Данные успешно извлечены!")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())