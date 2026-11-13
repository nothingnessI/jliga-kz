import time
import re
import psycopg2
from bs4 import BeautifulSoup
from curl_cffi import requests
from app.core.config import settings

DELAY_BETWEEN_TEAMS = 2.0

# Маппинг русских позиций в значения твоего Enum (как в app/models/enums.py)
POSITION_MAP = {
    "вратарь": "GOALKEEPER",
    "защитник": "DEFENDER",
    "полузащитник": "MIDFIELDER",
    "нападающий": "FORWARD"
}


def get_db_connection():
    return psycopg2.connect(
        dbname=settings.DB_NAME,
        user=settings.DB_USER,
        password=settings.DB_PASSWORD,
        host=settings.DB_HOST,
        port=settings.DB_PORT
    )


def parse_team_positions(http_session, team_jliga_id):
    url = f"https://jliga.ru/team/{team_jliga_id}/application"
    try:
        resp = http_session.get(url, timeout=15)
        if resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, "html.parser")

        players_data = []

        # Ищем карточки игроков (они есть как в текущем составе, так и в ушедших)
        player_cards = soup.find_all("a", class_="blk-teamroster__card", href=True)

        for card in player_cards:
            # 1. Вытаскиваем jliga_id игрока
            match = re.search(r"/player/(\d+)", card["href"])
            if not match:
                continue
            p_jliga_id = int(match.group(1))

            # 2. Ищем блок с амплуа
            amplua_tag = card.find("p", class_="blk-teamroster__amplua")

            if amplua_tag:
                position_text = amplua_tag.text.strip().lower()
                # Проверяем, есть ли такая позиция в нашем словаре
                if position_text in POSITION_MAP:
                    db_position = POSITION_MAP[position_text]
                    players_data.append((p_jliga_id, db_position))

        return players_data

    except Exception as e:
        print(f"  ❌ Ошибка загрузки {url}: {e}")
        return None


def main():
    print("🏁 Старт синхронизации амплуа игроков...")

    conn = get_db_connection()
    cursor = conn.cursor()

    # Берем все команды
    cursor.execute("SELECT id, jliga_id, name FROM teams WHERE jliga_id IS NOT NULL ORDER BY name")
    teams = cursor.fetchall()

    print(f"Найдено команд: {len(teams)}\n")

    http_session = requests.Session(impersonate="chrome110")

    total_updated = 0

    for team_db_id, team_jliga_id, team_name in teams:
        print(f"🔄 Проверяем: {team_name}...")

        players_positions = parse_team_positions(http_session, team_jliga_id)

        if not players_positions:
            print("  ⚠️ Игроки с амплуа не найдены (или ошибка).")
            time.sleep(DELAY_BETWEEN_TEAMS)
            continue

        changes_count = 0

        for p_jliga_id, position in players_positions:
            # Обновляем позицию игрока в глобальной таблице players
            # (Обновляем только если позиция отличается или пустая)
            cursor.execute("""
                UPDATE players 
                SET position = %s 
                WHERE jliga_id = %s AND (position IS NULL OR position::text != %s)
            """, (position, p_jliga_id, position))

            if cursor.rowcount > 0:
                changes_count += 1

        if changes_count > 0:
            conn.commit()
            total_updated += changes_count
            print(f"  💾 Обновлено амплуа: {changes_count}")
        else:
            print("  ➖ Изменений нет")

        time.sleep(DELAY_BETWEEN_TEAMS)

    cursor.close()
    conn.close()
    http_session.close()

    print(f"\n🎉 ГОТОВО! Всего обновлено позиций: {total_updated}")


if __name__ == "__main__":
    main()