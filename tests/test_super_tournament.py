import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tournament import Tournament
from app.models.team import Team
from app.models.player import Player
from app.models.tournament_team import TournamentTeam
from app.models.team_player import TeamPlayer
from app.models.tournament_standing import TournamentStanding
from app.models.match import Match
from app.models.match_event import MatchEvent
from app.models.enums import (
    MatchStatus, MatchStage, MatchEventType,
    TournamentStatus, TournamentFormat, PlayerPosition
)
from app.services.stats_recalc import StatsRecalculator


async def test_super_tournament_full_scenario(db: AsyncSession):
    """
    🏆 СУПЕР-ТЕСТ: полный сценарий турнира с 5 командами.

    Сценарий:
    - 1 турнир (круговой, 5 команд)
    - 10 игроков (по 2 на команду)
    - 10 матчей (круговая система: каждая команда играет с каждой)
    - Голы, ассисты, жёлтые и красные карточки
    - Проверка ВСЕЙ статистики после пересчёта
    """

    # ============================================================
    # ШАГ 1. СОЗДАЁМ ТУРНИР
    # ============================================================
    tournament = Tournament(
        name="SUPER TEST CUP 2026",
        season="2026",
        division="PRIMERA",
        age_category="ADULTS",
        status=TournamentStatus.ACTIVE,
        max_teams=5,
        logo_url="",
        tournament_format=TournamentFormat.ROUND_ROBIN,
    )
    db.add(tournament)
    await db.flush()

    # ============================================================
    # ШАГ 2. СОЗДАЁМ 5 КОМАНД
    # ============================================================
    team_names = [
        ("Alpha FC", "alpha-fc"),
        ("Bravo United", "bravo-united"),
        ("Charlie City", "charlie-city"),
        ("Delta Rovers", "delta-rovers"),
        ("Echo Athletic", "echo-athletic"),
    ]
    teams = []
    for name, slug in team_names:
        t = Team(name=name, slug=slug, is_active=True)
        db.add(t)
        teams.append(t)
    await db.flush()

    # Привязываем команды к турниру (TournamentTeam)
    for t in teams:
        db.add(TournamentTeam(tournament_id=tournament.id, team_id=t.id))
    await db.flush()

    # ============================================================
    # ШАГ 3. СОЗДАЁМ 10 ИГРОКОВ (по 2 в каждую команду)
    # ============================================================
    # players_map[(team_index, player_number)] = Player
    players_map = {}
    for team_idx, team in enumerate(teams):
        for p_num in (1, 2):
            player = Player(
                first_name=f"Player{p_num}",
                last_name=f"Team{chr(65 + team_idx)}",  # TeamA, TeamB, ...
                position=PlayerPosition.FORWARD if p_num == 1 else PlayerPosition.MIDFIELDER,
            )
            db.add(player)
            await db.flush()
            players_map[(team_idx, p_num)] = player

            # Привязываем игрока к команде в этом турнире
            db.add(TeamPlayer(
                player_id=player.id,
                team_id=team.id,
                tournament_id=tournament.id,
                is_active=True,
            ))
    await db.flush()

    # ============================================================
    # ШАГ 4. СОЗДАЁМ 10 МАТЧЕЙ (круговой турнир, 5 команд)
    # ============================================================
    # Индексы команд: A=0, B=1, C=2, D=3, E=4
    # Формат: (home_idx, away_idx, home_score, away_score)
    matches_data = [
        (0, 1, 2, 1),  # R1: A-B 2:1
        (2, 3, 1, 1),  # R1: C-D 1:1
        (0, 2, 3, 0),  # R2: A-C 3:0
        (1, 4, 2, 2),  # R2: B-E 2:2
        (0, 3, 1, 0),  # R3: A-D 1:0
        (2, 4, 0, 0),  # R3: C-E 0:0
        (0, 4, 4, 0),  # R4: A-E 4:0
        (1, 3, 3, 1),  # R4: B-D 3:1
        (1, 2, 0, 0),  # R5: B-C 0:0
        (3, 4, 2, 0),  # R5: D-E 2:0
    ]
    matches = []
    for home_idx, away_idx, hs, a_s in matches_data:
        m = Match(
            tournament_id=tournament.id,
            home_team_id=teams[home_idx].id,
            away_team_id=teams[away_idx].id,
            home_score=hs,
            away_score=a_s,
            status=MatchStatus.FINISHED,
            stage=MatchStage.GROUP,
            round=1,
        )
        db.add(m)
        matches.append(m)
    await db.flush()

    # ============================================================
    # ШАГ 5. ДОБАВЛЯЕМ СОБЫТИЯ (голы, ассисты, карточки)
    # ============================================================
    def ev(match_idx, team_idx, player_num, e_type, minute):
        return MatchEvent(
            match_id=matches[match_idx].id,
            team_id=teams[team_idx].id,
            player_id=players_map[(team_idx, player_num)].id,
            event_type=e_type,
            minute=minute,
        )

    # --- R1: Alpha 2:1 Bravo ---
    db.add_all([
        ev(0, 0, 1, MatchEventType.GOAL, 15),
        ev(0, 0, 2, MatchEventType.ASSIST, 15),
        ev(0, 0, 1, MatchEventType.GOAL, 45),
        ev(0, 1, 1, MatchEventType.GOAL, 60),
        ev(0, 1, 2, MatchEventType.ASSIST, 60),
    ])

    # --- R1: Charlie 1:1 Delta ---
    db.add_all([
        ev(1, 2, 1, MatchEventType.GOAL, 30),
        ev(1, 3, 1, MatchEventType.GOAL, 75),
    ])

    # --- R2: Alpha 3:0 Charlie ---
    db.add_all([
        ev(2, 0, 1, MatchEventType.GOAL, 10),
        ev(2, 0, 2, MatchEventType.ASSIST, 10),
        ev(2, 0, 1, MatchEventType.GOAL, 50),
        ev(2, 0, 2, MatchEventType.ASSIST, 50),
        ev(2, 0, 2, MatchEventType.GOAL, 80),
        ev(2, 2, 1, MatchEventType.YELLOW_CARD, 55),
    ])

    # --- R2: Bravo 2:2 Echo ---
    db.add_all([
        ev(3, 1, 1, MatchEventType.GOAL, 20),
        ev(3, 4, 1, MatchEventType.GOAL, 40),
        ev(3, 1, 2, MatchEventType.GOAL, 70),
        ev(3, 1, 1, MatchEventType.ASSIST, 70),
        ev(3, 4, 1, MatchEventType.GOAL, 90),
    ])

    # --- R3: Alpha 1:0 Delta ---
    db.add_all([
        ev(4, 0, 2, MatchEventType.GOAL, 55),
        ev(4, 0, 1, MatchEventType.ASSIST, 55),
        ev(4, 3, 1, MatchEventType.YELLOW_CARD, 40),
        ev(4, 3, 2, MatchEventType.RED_CARD, 88),
    ])

    # --- R3: Charlie 0:0 Echo ---
    db.add_all([
        ev(5, 2, 1, MatchEventType.YELLOW_CARD, 30),
    ])

    # --- R4: Alpha 4:0 Echo ---
    db.add_all([
        ev(6, 0, 1, MatchEventType.GOAL, 5),
        ev(6, 0, 2, MatchEventType.ASSIST, 5),
        ev(6, 0, 1, MatchEventType.GOAL, 30),
        ev(6, 0, 2, MatchEventType.ASSIST, 30),
        ev(6, 0, 1, MatchEventType.GOAL, 60),
        ev(6, 0, 2, MatchEventType.GOAL, 85),
        ev(6, 0, 1, MatchEventType.ASSIST, 85),
    ])

    # --- R4: Bravo 3:1 Delta ---
    db.add_all([
        ev(7, 1, 1, MatchEventType.GOAL, 10),
        ev(7, 1, 2, MatchEventType.ASSIST, 10),
        ev(7, 1, 1, MatchEventType.GOAL, 40),
        ev(7, 1, 2, MatchEventType.GOAL, 70),
        ev(7, 3, 1, MatchEventType.GOAL, 90),
    ])

    # --- R5: Bravo 0:0 Charlie ---
    db.add_all([
        ev(8, 1, 1, MatchEventType.YELLOW_CARD, 50),
        ev(8, 2, 1, MatchEventType.YELLOW_CARD, 70),
    ])

    # --- R5: Delta 2:0 Echo ---
    db.add_all([
        ev(9, 3, 1, MatchEventType.GOAL, 25),
        ev(9, 3, 2, MatchEventType.ASSIST, 25),
        ev(9, 3, 1, MatchEventType.GOAL, 65),
        ev(9, 3, 2, MatchEventType.ASSIST, 65),
    ])

    await db.commit()

    # ============================================================
    # ШАГ 6. ЗАПУСКАЕМ ПЕРЕСЧЁТ ВСЕЙ СТАТИСТИКИ
    # ============================================================
    await StatsRecalculator.recalculate_all(db, tournament.id)

    # ============================================================
    # ШАГ 7. ПРОВЕРЯЕМ ТУРНИРНУЮ ТАБЛИЦУ
    # ============================================================
    standings = (await db.execute(
        select(TournamentStanding).where(TournamentStanding.tournament_id == tournament.id)
    )).scalars().all()

    assert len(standings) == 5, f"Ожидали 5 команд, получили {len(standings)}"

    by_team = {s.team_id: s for s in standings}

    # --- Alpha (1 место): 4 игры, 4W, 0D, 0L, 10-1, 12 очков ---
    alpha = by_team[teams[0].id]
    assert alpha.games_played == 4
    assert alpha.wins == 4
    assert alpha.draws == 0
    assert alpha.losses == 0
    assert alpha.goals_for == 10, f"Alpha GF ожидали 10, получили {alpha.goals_for}"
    assert alpha.goals_against == 1, f"Alpha GA ожидали 1, получили {alpha.goals_against}"
    assert alpha.points == 12

    # --- Bravo (2 место): 4 игры, 1W, 2D, 1L, 6-5, 5 очков ---
    bravo = by_team[teams[1].id]
    assert bravo.games_played == 4
    assert bravo.wins == 1
    assert bravo.draws == 2
    assert bravo.losses == 1
    assert bravo.goals_for == 6
    assert bravo.goals_against == 5
    assert bravo.points == 5

    # --- Delta (3 место): 4 игры, 1W, 1D, 2L, 4-5, 4 очка ---
    delta = by_team[teams[3].id]
    assert delta.points == 4
    assert delta.wins == 1
    assert delta.draws == 1
    assert delta.losses == 2
    assert delta.goals_for == 4
    assert delta.goals_against == 5

    # --- Charlie (4 место): 4 игры, 0W, 3D, 1L, 1-4, 3 очка ---
    charlie = by_team[teams[2].id]
    assert charlie.points == 3
    assert charlie.draws == 3
    assert charlie.losses == 1
    assert charlie.goals_for == 1
    assert charlie.goals_against == 4

    # --- Echo (5 место): 4 игры, 0W, 2D, 2L, 2-8, 2 очка ---
    echo = by_team[teams[4].id]
    assert echo.points == 2
    assert echo.draws == 2
    assert echo.losses == 2
    assert echo.goals_for == 2
    assert echo.goals_against == 8

    # ============================================================
    # ШАГ 8. ПРОВЕРЯЕМ КАРТОЧКИ В ТАБЛИЦЕ
    # ============================================================
    # Charlie: 3 ЖК (C1 в 3 матчах)
    assert charlie.yellow_cards == 3, f"Charlie ЖК ожидали 3, получили {charlie.yellow_cards}"

    # Delta: 1 ЖК (D1) + 1 КК (D2)
    assert delta.yellow_cards == 1
    assert delta.red_cards == 1

    # Bravo: 1 ЖК (B1)
    assert bravo.yellow_cards == 1

    # ============================================================
    # ШАГ 9. ПРОВЕРЯЕМ СТАТИСТИКУ ИГРОКОВ
    # ============================================================
    team_players = (await db.execute(
        select(TeamPlayer).where(TeamPlayer.tournament_id == tournament.id)
    )).scalars().all()
    tp_by_player = {tp.player_id: tp for tp in team_players}

    # --- A1: 7 голов, 2 паса (голы в R1×2, R2×2, R4×3) ---
    a1 = tp_by_player[players_map[(0, 1)].id]
    assert a1.stats_goals == 7, f"A1 голы ожидали 7, получили {a1.stats_goals}"
    assert a1.stats_assists == 2, f"A1 пасы ожидали 2, получили {a1.stats_assists}"
    assert a1.stats_yellow_cards == 0
    assert a1.stats_red_cards == 0

    # --- A2: 3 гола, 5 пасов ---
    a2 = tp_by_player[players_map[(0, 2)].id]
    assert a2.stats_goals == 3, f"A2 голы ожидали 3, получили {a2.stats_goals}"
    assert a2.stats_assists == 5, f"A2 пасы ожидали 5, получили {a2.stats_assists}"

    # --- B1: 4 гола, 1 пас, 1 ЖК ---
    b1 = tp_by_player[players_map[(1, 1)].id]
    assert b1.stats_goals == 4, f"B1 голы ожидали 4, получили {b1.stats_goals}"
    assert b1.stats_assists == 1
    assert b1.stats_yellow_cards == 1

    # --- B2: 2 гола, 2 паса ---
    b2 = tp_by_player[players_map[(1, 2)].id]
    assert b2.stats_goals == 2
    assert b2.stats_assists == 2

    # --- C1: 1 гол, 3 ЖК ---
    c1 = tp_by_player[players_map[(2, 1)].id]
    assert c1.stats_goals == 1
    assert c1.stats_assists == 0
    assert c1.stats_yellow_cards == 3, f"C1 ЖК ожидали 3, получили {c1.stats_yellow_cards}"

    # --- D1: 4 гола, 1 ЖК ---
    d1 = tp_by_player[players_map[(3, 1)].id]
    assert d1.stats_goals == 4
    assert d1.stats_yellow_cards == 1

    # --- D2: 0 голов, 2 паса, 1 КК ---
    d2 = tp_by_player[players_map[(3, 2)].id]
    assert d2.stats_goals == 0
    assert d2.stats_assists == 2
    assert d2.stats_red_cards == 1

    # --- E1: 2 гола ---
    e1 = tp_by_player[players_map[(4, 1)].id]
    assert e1.stats_goals == 2

    # ============================================================
    # ШАГ 10. ФИНАЛЬНАЯ ПРОВЕРКА И КРАСИВЫЙ ВЫВОД
    # ============================================================
    # Проверяем, что сумма голов игроков = сумме голов в матчах
    total_goals_from_matches = sum(m.home_score + m.away_score for m in matches)
    total_goals_from_players = sum(tp.stats_goals for tp in team_players)
    assert total_goals_from_matches == total_goals_from_players, (
        f"Голы не сходятся: в матчах {total_goals_from_matches}, у игроков {total_goals_from_players}"
    )

    # Сортировка таблицы как в реальном приложении
    standings_sorted = sorted(standings, key=lambda s: (-s.points, -s.goals_for))

    # Красивый вывод в консоль
    print("\n" + "=" * 80)
    print("🏆  СУПЕР-ТЕСТ ПРОЙДЕН! ВСЯ СТАТИСТИКА СЧИТАЕТСЯ ПРАВИЛЬНО")
    print("=" * 80)
    print(f"  Турнир:   {tournament.name}")
    print(f"  Команд:   {len(teams)}")
    print(f"  Игроков:  {len(players_map)}")
    print(f"  Матчей:   {len(matches)}")
    print(f"  Голов:    {total_goals_from_matches}")
    print("-" * 80)
    print("  📊 ИТОГОВАЯ ТУРНИРНАЯ ТАБЛИЦА:")
    print(f"  {'#':<3} {'Команда':<18} {'И':<3} {'В':<3} {'Н':<3} {'П':<3} {'Голы':<8} {'Очки':<5} {'ЖК':<4} {'КК'}")
    for i, s in enumerate(standings_sorted, 1):
        team = next(t for t in teams if t.id == s.team_id)
        print(
            f"  {i:<3} {team.name:<18} {s.games_played:<3} {s.wins:<3} {s.draws:<3} "
            f"{s.losses:<3} {s.goals_for}-{s.goals_against:<4} {s.points:<5} "
            f"{s.yellow_cards:<4} {s.red_cards}"
        )
    print("-" * 80)
    print("  ⚽  ТОП-5 БОМБАРДИРОВ:")
    scorers_sorted = sorted(team_players, key=lambda tp: (-tp.stats_goals, -tp.stats_assists))[:5]
    for i, tp in enumerate(scorers_sorted, 1):
        print(
            f"  {i}. {tp.player.last_name} {tp.player.first_name}: "
            f"{tp.stats_goals} голов, {tp.stats_assists} пасов, "
            f"{tp.stats_yellow_cards} ЖК, {tp.stats_red_cards} КК"
        )
    print("=" * 80 + "\n")