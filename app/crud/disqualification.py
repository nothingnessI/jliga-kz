from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.base import CRUDBase
from app.models.disqualification import Disqualification
from app.schemas.disqualification import DisqualificationCreate, DisqualificationUpdate


class CRUDDisqualification(
    CRUDBase[Disqualification, DisqualificationCreate, DisqualificationUpdate]
):
    async def get_by_tournament(
        self, db: AsyncSession, *, tournament_id: int
    ) -> list[Disqualification]:
        stmt = (
            select(Disqualification)
            .where(Disqualification.tournament_id == tournament_id)
            .order_by(Disqualification.decision_date.desc())
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get_active_by_player(
        self, db: AsyncSession, *, player_id: int
    ) -> list[Disqualification]:
        stmt = select(Disqualification).where(
            Disqualification.player_id == player_id,
            Disqualification.matches_remaining > 0,
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())


disqualification = CRUDDisqualification(Disqualification)