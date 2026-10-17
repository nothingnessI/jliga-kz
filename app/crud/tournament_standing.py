from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.base import CRUDBase
from app.models.tournament_standing import TournamentStanding
from app.schemas.tournament_standing import TournamentStandingCreate


class CRUDTournamentStanding(
    CRUDBase[TournamentStanding, TournamentStandingCreate, TournamentStandingCreate]
):
    async def get_by_tournament(
        self, db: AsyncSession, *, tournament_id: int
    ) -> list[TournamentStanding]:
        stmt = (
            select(TournamentStanding)
            .where(TournamentStanding.tournament_id == tournament_id)
            .order_by(
                TournamentStanding.points.desc(),
                (TournamentStanding.goals_for - TournamentStanding.goals_against).desc(),
                TournamentStanding.goals_for.desc(),
            )
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get_by_tournament_and_team(
        self, db: AsyncSession, *, tournament_id: int, team_id: int
    ) -> TournamentStanding | None:
        stmt = select(TournamentStanding).where(
            TournamentStanding.tournament_id == tournament_id,
            TournamentStanding.team_id == team_id,
        )
        result = await db.execute(stmt)
        return result.scalar_one_or_none()


tournament_standing = CRUDTournamentStanding(TournamentStanding)