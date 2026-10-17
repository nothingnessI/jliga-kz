from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.base import CRUDBase
from app.models.enums import TournamentStatus
from app.models.tournament import Tournament
from app.schemas.tournament import TournamentCreate, TournamentUpdate


class CRUDTournament(CRUDBase[Tournament, TournamentCreate, TournamentUpdate]):
    async def get_by_season(self, db: AsyncSession, *, season: str) -> list[Tournament]:
        stmt = select(Tournament).where(Tournament.season == season).order_by(Tournament.name)
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get_active(self, db: AsyncSession) -> list[Tournament]:
        stmt = (
            select(Tournament)
            .where(Tournament.status == TournamentStatus.ACTIVE)
            .order_by(Tournament.start_at.desc())
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get_by_division(
        self, db: AsyncSession, *, division: str
    ) -> list[Tournament]:
        stmt = select(Tournament).where(Tournament.division == division)
        result = await db.execute(stmt)
        return list(result.scalars().all())


tournament = CRUDTournament(Tournament)