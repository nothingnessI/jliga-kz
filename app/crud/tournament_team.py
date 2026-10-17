from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.base import CRUDBase
from app.models.tournament_team import TournamentTeam
from app.schemas.tournament_team import TournamentTeamCreate, TournamentTeamUpdate


class CRUDTournamentTeam(CRUDBase[TournamentTeam, TournamentTeamCreate, TournamentTeamUpdate]):
    async def get_by_tournament(
        self, db: AsyncSession, *, tournament_id: int
    ) -> list[TournamentTeam]:
        stmt = select(TournamentTeam).where(
            TournamentTeam.tournament_id == tournament_id
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get_by_team(
        self, db: AsyncSession, *, team_id: int
    ) -> list[TournamentTeam]:
        stmt = select(TournamentTeam).where(TournamentTeam.team_id == team_id)
        result = await db.execute(stmt)
        return list(result.scalars().all())


tournament_team = CRUDTournamentTeam(TournamentTeam)