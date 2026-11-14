import time
import re
import psycopg2
from bs4 import BeautifulSoup
from curl_cffi import requests
from app.core.config import settings


def get_db_connection():
    return psycopg2.connect(
        dbname=settings.DB_NAME,
        user=settings.DB_USER,
        password=settings.DB_PASSWORD,
        host=settings.DB_HOST,
        port=settings.DB_PORT
    )


def safe_int(val):
    if not val: return 0
    val = val.strip()
    if val == '-' or val == '': return 0
    try:
        return int(val)
    except ValueError:
        return 0


def map_position(pos_text):
    pos_text = pos_text.strip()
    if "Вр" in pos_text: return "GOALKEEPER"
    if "Зщ" in pos_text: return "DEFENDER"
    if "Пз" in pos_text: return "MIDFIELDER"
    if "Нп" in pos_text: return "FORWARD"
    return None


def fetch_with_retry(http_session, url):
    """5 попыток с таймаутом 60 секунд для слабого интернета."""
    for attempt in range(5):
        try:
            resp = http_session.get(url, timeout=60, impersonate="chrome110")
            if resp.status_code == 200:
                return resp
            print(f"  ⚠️ Попытка {attempt + 1}: Статус {resp.status_code}")
        except Exception as e:
            print(f"  ⚠️ Попытка {attempt + 1} (Ошибка сети): {e}")
            time.sleep(5)
    return None


def parse_application_page(http_session, t_jliga_id, team_jliga_id):
    url = f"https://jliga.ru/tournament/{t_jliga_id}/teams/application?team_id={team_jliga_id}"
    resp = fetch_with_retry(http_session, url)

    if not resp:
        return None, None

    soup = BeautifulSoup(resp.text, "html.parser")
    active_players = {}
    inactive_players = {}

    # Активные (players-1)
    pane_active = soup.find("div", id="players-1")
    if pane_active:
        for row in pane_active.find_all("tr", class_="blk-tteams__row"):
            player_link = row.find("a", class_="blk-tteams__player")
            if not player_link: continue
            match = re.search(r"/player/(\d+)", player_link["href"])
            if not match: continue
            p_jliga_id = int(match.group(1))
            pos_text = row.find("td", class_="blk-tteams__cell--amplua").text.strip()
            stats_cells = row.find_all("td", class_="blk-tteams__cell--var")
            stats = {
                "games": safe_int(stats_cells[0].text) if len(stats_cells) > 0 else 0,
                "goals": safe_int(stats_cells[1].text) if len(stats_cells) > 1 else 0,
                "assists": safe_int(stats_cells[2].text) if len(stats_cells) > 2 else 0,
                "yc": safe_int(stats_cells[3].text) if len(stats_cells) > 3 else 0,
                "rc": safe_int(stats_cells[4].text) if len(stats_cells) > 4 else 0,
            }
            active_players[p_jliga_id] = {"position": map_position(pos_text), "stats": stats}

    # Отзаявленные (players-2)
    pane_inactive = soup.find("div", id="players-2")
    if pane_inactive:
        for row in pane_inactive.find_all("tr", class_="blk-tteams__row"):
            player_link = row.find("a", class_="blk-tteams__player")
            if not player_link: continue
            match = re.search(r"/player/(\d+)", player_link["href"])
            if not match: continue
            p_jliga_id = int(match.group(1))
            pos_text = row.find("td", class_="blk-tteams__cell--amplua").text.strip()
            stats_cells = row.find_all("td", class_="blk-tteams__cell--var")
            stats = {
                "games": safe_int(stats_cells[0].text) if len(stats_cells) > 0 else 0,
                "goals": safe_int(stats_cells[1].text) if len(stats_cells) > 1 else 0,
                "assists": safe_int(stats_cells[2].text) if len(stats_cells) > 2 else 0,
                "yc": safe_int(stats_cells[3].text) if len(stats_cells) > 3 else 0,
                "rc": safe_int(stats_cells[4].text) if len(stats_cells) > 4 else 0,
            }
            inactive_players[p_jliga_id] = {"position": map_position(pos_text), "stats": stats}

    return active_players, inactive_players


def main():
    print("🏁 Старт исправления ТОЛЬКО 3-х команд...")

    # ТОЛЬКО ЭТИ ТРИ КОМАНДЫ
    failed_teams = [
        (1066252, 1511968),  # HAN TANIRI 2012 B
        (1066252, 1511955),  # HAN TANIRI 2013 A
        (1066252, 1511954),  # JEYRAN 2012 A
    ]

    conn = get_db_connection()
    cursor = conn.cursor()
    http_session = requests.Session()

    for t_jliga_id, team_jliga_id in failed_teams:
        cursor.execute("SELECT id, name FROM tournaments WHERE jliga_id = %s", (t_jliga_id,))
        t_row = cursor.fetchone()
        cursor.execute("SELECT id, name FROM teams WHERE jliga_id = %s", (team_jliga_id,))
        team_row = cursor.fetchone()

        if not t_row or not team_row:
            print(f"⚠️ Турнир {t_jliga_id} или Команда {team_jliga_id} не найдены в БД.")
            continue

        t_db_id, t_name = t_row
        team_db_id, team_name = team_row

        print(f"🔄 Пробуем: {team_name}...")

        active_data, inactive_data = parse_application_page(http_session, t_jliga_id, team_jliga_id)

        if active_data is None:
            print("  ❌ Не удалось загрузить. Интернет не выдержал 5 попыток.")
            continue

        print(f"  ✅ УСПЕХ! В основе: {len(active_data)}, Отзаявлено: {len(inactive_data)}")

        cursor.execute("""
            SELECT tp.id, p.jliga_id
            FROM team_players tp
            JOIN players p ON p.id = tp.player_id
            WHERE tp.tournament_id = %s AND tp.team_id = %s AND p.jliga_id IS NOT NULL
        """, (t_db_id, team_db_id))
        db_dict = {row[1]: row[0] for row in cursor.fetchall()}

        changes = 0

        # Обновляем активных
        for p_jliga_id, p_data in active_data.items():
            if p_data["position"]:
                cursor.execute(
                    "UPDATE players SET position = %s WHERE jliga_id = %s AND (position IS NULL OR position::text != %s)",
                    (p_data["position"], p_jliga_id, p_data["position"]))

            if p_jliga_id in db_dict:
                tp_id = db_dict[p_jliga_id]
                st = p_data["stats"]
                cursor.execute("""
                    UPDATE team_players SET is_active = TRUE, stats_games = %s, stats_goals = %s, stats_assists = %s, stats_yellow_cards = %s, stats_red_cards = %s 
                    WHERE id = %s
                """, (st["games"], st["goals"], st["assists"], st["yc"], st["rc"], tp_id))
                changes += 1

        # Обновляем отзаявленных
        for p_jliga_id, p_data in inactive_data.items():
            if p_jliga_id in db_dict:
                tp_id = db_dict[p_jliga_id]
                cursor.execute("UPDATE team_players SET is_active = FALSE WHERE id = %s", (tp_id,))
                changes += 1

        conn.commit()
        print(f"  💾 Сохранено: {changes} записей.")
        time.sleep(3)

    cursor.close()
    conn.close()
    print("\n🎉 ГОТОВО! Все три команды синхронизированы.")


if __name__ == "__main__":
    main()