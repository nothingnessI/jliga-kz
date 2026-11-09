import pytest
from sqlalchemy import select
from app.models.enums import MatchEventType, MatchStatus
from app.models.match import Match
from app.models.match_event import MatchEvent
from app.models.tournament_standing import TournamentStanding
from app.models.disqualification import Disqualification
from app.services.standings import StandingsService
from app.services.stats_recalc import StatsRecalculator
from app.services.disqualifications import DisqualificationService
# Импортируем функцию обновления счёта из роутов
from app.api.v1.pages import recalculate_match_score


@pytest.mark.asyncio
async def test_standings_with_own_goal(db, seed_basic):
    teams, t = seed_basic["teams"], seed_basic["tournament"]
    m = seed_basic["match"]
    m.status = MatchStatus.FINISHED

    # Игрок команды А забивает автогол (гол в пользу команды Б)
    db.add(MatchEvent(match_id=m.id, team_id=teams[0].id, player_id=seed_basic["players"][0].id,
                      event_type=MatchEventType.OWN_GOAL, minute=10))
    await db.commit()

    # ВАЖНО: Сначала обновляем счет матча на основе событий, потом считаем таблицу
    await recalculate_match_score(m, db)
    await StandingsService.recalculate_standings(db, t.id)

    st_a = await db.scalar(select(TournamentStanding).where(TournamentStanding.team_id == teams[0].id))
    st_b = await db.scalar(select(TournamentStanding).where(TournamentStanding.team_id == teams[1].id))
    assert st_a.goals_against == 1
    assert st_b.goals_for == 1


@pytest.mark.asyncio
async def test_stats_recalc_with_assist(db, seed_basic):
    teams, t, players, m = seed_basic["teams"], seed_basic["tournament"], seed_basic["players"], seed_basic["match"]
    m.status = MatchStatus.FINISHED

    # Создаем второго игрока для команды А (чтобы был реальный ассистент из той же команды)
    from app.models.team_player import TeamPlayer
    from app.models.player import Player
    from app.models.enums import PlayerPosition

    p_assist = Player(first_name="Assist", last_name="Master", position=PlayerPosition.MIDFIELDER)
    db.add(p_assist)
    await db.flush()
    db.add(TeamPlayer(
        player_id=p_assist.id, team_id=teams[0].id, tournament_id=t.id,
        jersey_number=99, is_active=True
    ))
    await db.commit()
    await db.refresh(p_assist)

    # Игрок 0 забивает, новый игрок p_assist отдает передачу (оба - команда А)
    db.add(MatchEvent(match_id=m.id, team_id=teams[0].id, player_id=players[0].id, event_type=MatchEventType.GOAL, minute=10))
    db.add(MatchEvent(match_id=m.id, team_id=teams[0].id, player_id=p_assist.id, event_type=MatchEventType.ASSIST, minute=10))
    await db.commit()

    await StatsRecalculator.recalculate_all(db, t.id)

    from app.models.team_player import TeamPlayer
    tp_scorer = await db.scalar(select(TeamPlayer).where(TeamPlayer.player_id == players[0].id))
    tp_assist = await db.scalar(select(TeamPlayer).where(TeamPlayer.player_id == p_assist.id))
    await db.refresh(tp_scorer)
    await db.refresh(tp_assist)

    assert tp_scorer.stats_goals == 1, "Гол не засчитался забившему игроку"
    assert tp_assist.stats_assists == 1, "Ассист не засчитался отдавшему игроку"


@pytest.mark.asyncio
async def test_disqualification_red_card(db, seed_basic):
    teams, t, players, m = seed_basic["teams"], seed_basic["tournament"], seed_basic["players"], seed_basic["match"]
    m.status = MatchStatus.FINISHED

    db.add(MatchEvent(match_id=m.id, team_id=teams[0].id, player_id=players[0].id, event_type=MatchEventType.RED_CARD,
                      minute=30))
    await db.commit()

    await DisqualificationService.auto_suspend_by_cards(db, m)
    await db.commit()

    disq = await db.scalar(select(Disqualification).where(Disqualification.player_id == players[0].id))
    assert disq is not None
    assert disq.matches_remaining == 1