from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime

from app.models.match import Match
from app.models.tournament import Tournament
from app.models.disqualification import Disqualification
from app.models.match_event import MatchEvent
from app.models.enums import MatchStatus, MatchEventType


class DisqualificationService:

    @staticmethod
    async def process_match_finished(db: AsyncSession, match: Match):
        """
        Вызывается при смене статуса матча на FINISHED.
        Списывает 1 матч у всех активных дисквалификаций команд, участвующих в матче.
        """
        if match.status != MatchStatus.FINISHED:
            return

        # Ищем всех, кто отбывает наказание в этом турнире и чья команда играла в этом матче
        stmt = select(Disqualification).where(
            Disqualification.tournament_id == match.tournament_id,
            Disqualification.matches_remaining > 0,
            Disqualification.team_id.in_([match.home_team_id, match.away_team_id])
        )

        active_disq = (await db.execute(stmt)).scalars().all()

        for disq in active_disq:
            disq.matches_remaining -= 1
            db.add(disq)
            print(f"⚠️ Дисквалификация игрока ID {disq.player_id} списана. Осталось: {disq.matches_remaining}")

    @staticmethod
    async def auto_suspend_by_cards(db: AsyncSession, match: Match):
        """
        Автоматически выдает дисквалификации на основе карточек в матче.
        Работает строго по правилам, установленным в регламенте турнира.
        """
        # 1. Проверяем, включены ли авто-дисквалификации в регламенте турнира
        tournament = await db.get(Tournament, match.tournament_id)

        if not tournament:
            return

        if not tournament.auto_disqualifications:
            return  # В регламенте отключено, ничего не делаем

        # 2. Загружаем правила из регламента (или ставим дефолтные значения)
        yellow_limit = tournament.yellow_cards_limit if tournament.yellow_cards_limit is not None else 4
        yellow_susp = tournament.yellow_suspension if tournament.yellow_suspension is not None else 1
        red_susp = tournament.red_suspension if tournament.red_suspension is not None else 1

        # 3. Получаем все события этого матча
        events = (await db.execute(
            select(MatchEvent).where(MatchEvent.match_id == match.id)
        )).scalars().all()

        for event in events:
            if not event.player_id:
                continue

            # ==========================================
            # ОБРАБОТКА КРАСНОЙ КАРТОЧКИ
            # ==========================================
            if event.event_type == MatchEventType.RED_CARD:
                # Проверяем, нет ли уже активного бана (чтобы не выдать дважды)
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
                        matches_total=red_susp,
                        matches_remaining=red_susp,
                        reason="Красная карточка",
                        decision_date=datetime.now().date()
                    ))
                    print(f"🟥 Игрок {event.player_id} дисквалифицирован на {red_susp} матч(а) за КК.")

            # ==========================================
            # ОБРАБОТКА НАКОПЛЕНИЯ ЖЕЛТЫХ КАРТОЧЕК
            # ==========================================
            elif event.event_type == MatchEventType.YELLOW_CARD:
                # Считаем общее количество желтых у игрока за турнир (включая текущий матч)
                yellow_count = await db.scalar(
                    select(func.count()).select_from(MatchEvent).join(Match).where(
                        MatchEvent.player_id == event.player_id,
                        Match.tournament_id == match.tournament_id,
                        MatchEvent.event_type == MatchEventType.YELLOW_CARD,
                        Match.status == MatchStatus.FINISHED
                    )
                )

                # Если количество достигло лимита (и делится на него без остатка - для 4, 8, 12 и т.д.)
                if yellow_count and yellow_count % yellow_limit == 0:
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
                            matches_total=yellow_susp,
                            matches_remaining=yellow_susp,
                            reason=f"{yellow_limit} желтые карточки",
                            decision_date=datetime.now().date()
                        ))
                        print(
                            f"🟨 Игрок {event.player_id} дисквалифицирован на {yellow_susp} матч(а) за {yellow_limit} ЖК.")