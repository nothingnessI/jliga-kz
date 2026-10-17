from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.base import CRUDBase
from app.models.match_event import MatchEvent
from app.schemas.match_event import MatchEventCreate, MatchEventUpdate


class CRUDMatchEvent(CRUDBase[MatchEvent, MatchEventCreate, MatchEventUpdate]):
    async def get_by_match(self, db: AsyncSession, *, match_id: int) -> list[MatchEvent]:
        stmt = (
            select(MatchEvent)
            .where(MatchEvent.match_id == match_id)
            .order_by(MatchEvent.minute)
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())


match_event = CRUDMatchEvent(MatchEvent)