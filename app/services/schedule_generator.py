"""Генератор календаря кругового турнира (метод круга / Berger tables).

Каждая команда играет ровно один матч за тур — «стыковки» исключены по построению.
Нечётное количество — одна команда отдыхает каждый тур (BYE).
Каждый следующий круг — зеркалка (хозяева/гости меняются).
"""


def generate_round_robin(
    team_ids: list[int], circles: int = 1
) -> list[list[tuple[int, int]]]:
    """Возвращает список туров.

    Каждый элемент списка — тур, внутри список пар (home_id, away_id).

    Пример: 6 команд, 1 круг → 5 туров × 3 матча
    Пример: 7 команд, 1 круг → 7 туров × 3 матча (1 отдыхает)
    Пример: 6 команд, 2 круга → 10 туров × 3 матча (пары дважды, home/away меняются)
    """
    if len(team_ids) < 2 or circles < 1:
        return []

    teams: list = list(team_ids)
    if len(teams) % 2 == 1:
        teams.append(None)  # BYE

    n = len(teams)
    all_rounds: list[list[tuple[int, int]]] = []

    for circle in range(circles):
        order = list(range(n))
        mirror = (circle % 2 == 1)  # каждый нечётный круг — зеркальный

        for r in range(n - 1):
            pairs: list[tuple[int, int]] = []
            for i in range(n // 2):
                a = teams[order[i]]
                b = teams[order[n - 1 - i]]
                if a is None or b is None:
                    continue
                # Чередуем хозяев, чтобы не было серий
                home, away = (a, b) if r % 2 == 0 else (b, a)
                if mirror:
                    home, away = away, home
                pairs.append((home, away))

            all_rounds.append(pairs)
            # Ротация: первый фиксирован, остальные сдвигаются
            order = [order[0]] + [order[-1]] + order[1:-1]

    return all_rounds