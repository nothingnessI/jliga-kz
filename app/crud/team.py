from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.base import CRUDBase
from app.models.team import Team
from app.schemas.team import TeamCreate, TeamUpdate


class CRUDTeam(CRUDBase[Team, TeamCreate, TeamUpdate]):
    async def get_by_slug(self, db: AsyncSession, *, slug: str) -> Team | None:
        stmt = select(Team).where(Team.slug == slug)
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_active(
        self, db: AsyncSession, *, skip: int = 0, limit: int = 100
    ) -> list[Team]:
        stmt = (
            select(Team)
            .where(Team.is_active.is_(True))
            .order_by(Team.name)
            .offset(skip)
            .limit(limit)
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())


team = CRUDTeam(Team)