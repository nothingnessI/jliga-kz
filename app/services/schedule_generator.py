"""Генератор календаря для круговых турниров (с группами и до 4 кругов)."""

from dataclasses import dataclass


MAX_CIRCLES = 4


@dataclass
class RoundPair:
    """Одна пара в календаре."""
    round: int
    home_team_id: int
    away_team_id: int
    group_name: str | None = None


def _one_circle(team_ids: list[int]) -> list[list[tuple[int, int]]]:
    """
    Один круг круговой системы (метод круга).
    Возвращает туры как список пар (home, away).
    При нечётном числе команд один игрок в каждом туре отдыхает.
    """
    teams = list(team_ids)
    if len(teams) < 2:
        return []

    bye = None
    if len(teams) % 2:
        teams.append(bye)

    n = len(teams)
    fixed, rotating = teams[0], teams[1:]
    rounds: list[list[tuple[int, int]]] = []

    for r in range(n - 1):
        pairs: list[tuple[int, int]] = []

        a, b = fixed, rotating[0]
        if a is not None and b is not None:
            pairs.append((a, b) if r % 2 == 0 else (b, a))

        for i in range(1, n // 2):
            a, b = rotating[i], rotating[-i]
            if a is not None and b is not None:
                pairs.append((a, b) if i % 2 == 0 else (b, a))

        rounds.append(pairs)
        rotating = rotating[1:] + [rotating[0]]

    return rounds


def generate_round_robin(
    team_ids: list[int],
    circles: int = 1,
    group_name: str | None = None,
) -> list[RoundPair]:
    """
    Круговая система, до MAX_CIRCLES кругов.

    circles=1 → N-1 туров (N — чётное) или N туров (нечётное).
    circles=2 → те же туры + home/away перевёрнуты (двойной круг).
    circles=3,4 → повторение циклов 1 и 2.

    group_name: если задано — проставляется в каждой паре
    (для турниров с группами).
    """
    if circles < 1 or circles > MAX_CIRCLES:
        raise ValueError(f"circles должен быть от 1 до {MAX_CIRCLES}")
    if len(team_ids) < 2:
        return []

    base = _one_circle(team_ids)
    if not base:
        return []

    result: list[RoundPair] = []
    offset = 0

    for c in range(circles):
        swap = c % 2 == 1  # нечётный цикл — меняем home/away
        for idx, pairs in enumerate(base, start=1):
            for home, away in pairs:
                if swap:
                    home, away = away, home
                result.append(
                    RoundPair(
                        round=offset + idx,
                        home_team_id=home,
                        away_team_id=away,
                        group_name=group_name,
                    )
                )
        offset += len(base)

    return result


def generate_for_tournament(
    all_team_ids: list[int],
    circles: int = 1,
    groups_info: dict[str, list[int]] | None = None,
) -> list[RoundPair]:
    """
    Универсальная точка входа для роутера.

    Если заданы группы — генерация идёт по каждой группе отдельно,
    туры нумеруются с 1 внутри каждой группы.

    Если групп нет — общий круг по всем командам турнира.
    """
    if groups_info:
        result: list[RoundPair] = []
        for gname, tids in groups_info.items():
            result.extend(generate_round_robin(tids, circles=circles, group_name=gname))
        return result
    return generate_round_robin(all_team_ids, circles=circles)