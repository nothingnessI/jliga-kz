from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.base import CRUDBase
from app.models.player import Player
from app.models.team_player import TeamPlayer
from app.schemas.player import PlayerCreate, PlayerUpdate


class CRUDPlayer(CRUDBase[Player, PlayerCreate, PlayerUpdate]):
    async def search_by_last_name(
        self, db: AsyncSession, *, query: str, limit: int = 20
    ) -> list[Player]:
        stmt = (
            select(Player)
            .where(Player.last_name.ilike(f"%{query}%"))
            .order_by(Player.last_name)
            .limit(limit)
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get_by_team(
        self, db: AsyncSession, *, team_id: int, tournament_id: int | None = None
    ) -> list[Player]:
        stmt = select(Player).join(TeamPlayer, TeamPlayer.player_id == Player.id).where(
            TeamPlayer.team_id == team_id
        )
        if tournament_id is not None:
            stmt = stmt.where(TeamPlayer.tournament_id == tournament_id)
        result = await db.execute(stmt)
        return list(result.scalars().all())


player = CRUDPlayer(Player)