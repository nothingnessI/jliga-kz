from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.base import CRUDBase
from app.models.stadium import Stadium
from app.schemas.stadium import StadiumCreate, StadiumUpdate


class CRUDStadium(CRUDBase[Stadium, StadiumCreate, StadiumUpdate]):
    async def get_by_city(self, db: AsyncSession, *, city: str) -> list[Stadium]:
        stmt = select(Stadium).where(Stadium.city == city).order_by(Stadium.name)
        result = await db.execute(stmt)
        return list(result.scalars().all())


stadium = CRUDStadium(Stadium)