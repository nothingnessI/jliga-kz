from playwright.sync_api import sync_playwright


def dump_team_calendar():
    # Берем ABAI (ID 1370605) и сезон 2026 (ID 10129829)
    url = "https://jliga.ru/team/1370605/calendar?season_id=10129829"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)  # Откроем браузер для наглядности
        page = browser.new_page()

        print(f"Загружаем календарь команды: {url}")
        page.goto(url, wait_until="networkidle")

        # Ждем, чтобы прогрузились все динамические элементы
        page.wait_for_timeout(3000)

        with open("team_calendar_example.html", "w", encoding="utf-8") as f:
            f.write(page.content())

        print("✅ HTML сохранен в файл team_calendar_example.html")
        browser.close()


if __name__ == "__main__":
    dump_team_calendar()