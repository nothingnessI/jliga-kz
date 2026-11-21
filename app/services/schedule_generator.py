
"""Генерация календаря кругового турнира J-LIGA (1–4 круга)."""

from dataclasses import dataclass

MAX_CIRCLES = 4


@dataclass
class RoundPair:
    """Одна встреча: номер тура, хозяева, гости и группа."""

    round: int
    home_team_id: int
    away_team_id: int
    group_name: str | None = None


def _one_circle(team_ids: list[int]) -> list[list[tuple[int, int]]]:
    """Метод вращения: каждая пара встречается ровно один раз."""
    if len(team_ids) != len(set(team_ids)):
        raise ValueError("ID команд не должны повторяться")
    if any(team_id is None for team_id in team_ids):
        raise ValueError("ID команды не может быть None")
    if len(team_ids) < 2:
        return []

    teams = list(team_ids)
    if len(teams) % 2:
        teams.append(None)  # Команда без соперника отдыхает.

    n = len(teams)
    rounds: list[list[tuple[int, int]]] = []

    for round_index in range(n - 1):
        pairs: list[tuple[int, int]] = []

        for index in range(n // 2):
            a = teams[index]
            b = teams[n - 1 - index]

            if a is None or b is None:
                continue

            # Чередуем стороны, не изменяя состав пар.
            if (round_index + index) % 2:
                a, b = b, a

            pairs.append((a, b))

        rounds.append(pairs)

        # Первый участник закреплён; остальные вращаются.
        teams = [teams[0], teams[-1], *teams[1:-1]]

    return rounds


def generate_round_robin(
    team_ids: list[int],
    circles: int = 1,
    group_name: str | None = None,
) -> list[RoundPair]:
    """Генерирует до четырёх кругов с зеркальными ответными матчами."""
    if not 1 <= circles <= MAX_CIRCLES:
        raise ValueError(f"circles должен быть от 1 до {MAX_CIRCLES}")

    base_rounds = _one_circle(team_ids)
    result: list[RoundPair] = []
    rounds_per_circle = len(base_rounds)

    for circle_index in range(circles):
        reverse = circle_index % 2 == 1

        for round_index, pairs in enumerate(base_rounds, start=1):
            for home_id, away_id in pairs:
                if reverse:
                    home_id, away_id = away_id, home_id

                result.append(
                    RoundPair(
                        round=(
                            circle_index * rounds_per_circle
                            + round_index
                        ),
                        home_team_id=home_id,
                        away_team_id=away_id,
                        group_name=group_name,
                    )
                )

    return result


def generate_for_tournament(
    all_team_ids: list[int],
    circles: int = 1,
    groups_info: dict[str, list[int]] | None = None,
) -> list[RoundPair]:
    """
    Генерирует календарь целого турнира
    или отдельно каждой группы.

    При наличии групп номера туров начинаются
    с 1 в каждой группе.
    """
    if groups_info:
        result: list[RoundPair] = []
        seen_team_ids: set[int] = set()

        for group_name, team_ids in groups_info.items():
            if seen_team_ids.intersection(team_ids):
                raise ValueError(
                    "Одна команда не может быть в нескольких группах"
                )

            seen_team_ids.update(team_ids)

            result.extend(
                generate_round_robin(
                    team_ids,
                    circles=circles,
                    group_name=group_name,
                )
            )

        return result

    return generate_round_robin(
        all_team_ids,
        circles=circles,
    )
