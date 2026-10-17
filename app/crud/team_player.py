from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.base import CRUDBase
from app.models.team_player import TeamPlayer
from app.schemas.team_player import TeamPlayerCreate, TeamPlayerUpdate


class CRUDTeamPlayer(CRUDBase[TeamPlayer, TeamPlayerCreate, TeamPlayerUpdate]):
    async def get_by_tournament_and_team(
        self, db: AsyncSession, *, tournament_id: int, team_id: int
    ) -> list[TeamPlayer]:
        stmt = (
            select(TeamPlayer)
            .where(
                TeamPlayer.tournament_id == tournament_id,
                TeamPlayer.team_id == team_id,
                TeamPlayer.is_active.is_(True),
            )
            .order_by(TeamPlayer.jersey_number)
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get_by_player(
        self, db: AsyncSession, *, player_id: int
    ) -> list[TeamPlayer]:
        stmt = select(TeamPlayer).where(TeamPlayer.player_id == player_id)
        result = await db.execute(stmt)
        return list(result.scalars().all())


team_player = CRUDTeamPlayer(TeamPlayer)