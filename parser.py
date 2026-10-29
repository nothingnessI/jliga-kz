import asyncio
from playwright.async_api import async_playwright


async def main():
    # Ссылка на вкладку "Таблицы и сетки" (tables)
    url = "https://jliga.ru/tournament/1066065/tables"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        print(f"Загружаем {url}...")
        await page.goto(url, wait_until="networkidle")
        await page.wait_for_timeout(3000)  # Ждем, пока прогрузятся все вкладки групп и сетка

        html = await page.content()
        with open("tables_debug.html", "w", encoding="utf-8") as f:
            f.write(html)

        await browser.close()
        print("✅ Готово! Скинь мне файл tables_debug.html")


if __name__ == "__main__":
    asyncio.run(main())