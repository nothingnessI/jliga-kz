import httpx


def dump_html():
    # Берем турнир 1063876 и команду 1533900 из твоего скриншота
    url = "https://jliga.ru/tournament/1063876/teams/application?team_id=1533900"

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36"}

    print(f"Скачиваем {url}...")
    with httpx.Client(headers=headers, timeout=30.0) as client:
        resp = client.get(url)
        with open("application.html", "w", encoding="utf-8") as f:
            f.write(resp.text)
    print("✅ HTML сохранен в файл application.html")


if __name__ == "__main__":
    dump_html()