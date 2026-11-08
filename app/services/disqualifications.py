from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.match import Match
from app.models.disqualification import Disqualification
from app.models.enums import MatchStatus, MatchEventType
from app.models.match_event import MatchEvent
from app.models.team_player import TeamPlayer
from datetime import datetime


class DisqualificationService:

    @staticmethod
    async def process_match_finished(db: AsyncSession, match: Match):
        """
        Вызывается при смене статуса матча на FINISHED.
        Списывает 1 матч у всех активных дисквалификаций команд, участвующих в матче.
        """
        if match.status != MatchStatus.FINISHED:
            return

        # Ищем активные дисквалификации для обеих команд в рамках этого турнира
        stmt = select(Disqualification).where(
            Disqualification.tournament_id == match.tournament_id,
            Disqualification.matches_remaining > 0,
            Disqualification.team_id.in_([match.home_team_id, match.away_team_id])
        )

        active_disq = (await db.execute(stmt)).scalars().all()

        for disq in active_disq:
            # Списываем 1 матч
            disq.matches_remaining -= 1
            db.add(disq)

            print(f"⚠️ Дисквалификация игрока ID {disq.player_id} списана. Осталось: {disq.matches_remaining}")

        # Коммит делаем на уровне выше (в роуте)

        @staticmethod
        async def auto_suspend_by_cards(db: AsyncSession, match: Match):
            """
            Проверяет карточки и выдает дисквалификации:
            - Красная карточка = 1 матч дисквалификации.
            - 4 желтые карточки в одном турнире = 1 матч дисквалификации (и сброс счетчика).
            """
            # Получаем события этого матча
            events = (await db.execute(select(MatchEvent).where(MatchEvent.match_id == match.id))).scalars().all()

            for event in events:
                if not event.player_id: continue

                # 1. За красную карточку
                if event.event_type == MatchEventType.RED_CARD:
                    # Проверяем, нет ли уже активной дисквалификации за этот матч
                    existing = await db.scalar(select(Disqualification).where(
                        Disqualification.player_id == event.player_id,
                        Disqualification.tournament_id == match.tournament_id,
                        Disqualification.matches_remaining > 0
                    ))
                    if not existing:
                        db.add(Disqualification(
                            tournament_id=match.tournament_id,
                            team_id=event.team_id,
                            player_id=event.player_id,
                            matches_total=1,
                            matches_remaining=1,
                            reason="Красная карточка",
                            decision_date=datetime.now().date()
                        ))

                # 2. За 4 желтые карточки (накопление)
                elif event.event_type == MatchEventType.YELLOW_CARD:
                    # Считаем все желтые карточки игрока в этом турнире
                    yellow_count = await db.scalar(select(func.count()).select_from(MatchEvent).join(Match).where(
                        MatchEvent.player_id == event.player_id,
                        Match.tournament_id == match.tournament_id,
                        MatchEvent.event_type == MatchEventType.YELLOW_CARD,
                        Match.status == MatchStatus.FINISHED  # Считаем только в завершенных
                    ))

                    if yellow_count and yellow_count % 4 == 0:  # Если количество кратно 4 (4, 8, 12...)
                        existing = await db.scalar(select(Disqualification).where(
                            Disqualification.player_id == event.player_id,
                            Disqualification.tournament_id == match.tournament_id,
                            Disqualification.matches_remaining > 0
                        ))
                        if not existing:
                            db.add(Disqualification(
                                tournament_id=match.tournament_id,
                                team_id=event.team_id,
                                player_id=event.player_id,
                                matches_total=1,
                                matches_remaining=1,
                                reason="4 желтые карточки",
                                decision_date=datetime.now().date()
                            ))