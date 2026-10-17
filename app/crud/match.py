from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.crud.base import CRUDBase
from app.models.enums import MatchStatus
from app.models.match import Match
from app.schemas.match import MatchCreate, MatchUpdate


class CRUDMatch(CRUDBase[Match, MatchCreate, MatchUpdate]):
    async def get_with_teams(self, db: AsyncSession, *, match_id: int) -> Match | None:
        stmt = (
            select(Match)
            .where(Match.id == match_id)
            .options(selectinload(Match.home_team), selectinload(Match.away_team))
        )
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_tournament(
        self, db: AsyncSession, *, tournament_id: int
    ) -> list[Match]:
        stmt = (
            select(Match)
            .where(Match.tournament_id == tournament_id)
            .order_by(Match.match_date)
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get_upcoming(
        self, db: AsyncSession, *, limit: int = 10
    ) -> list[Match]:
        stmt = (
            select(Match)
            .where(Match.status == MatchStatus.SCHEDULED)
            .order_by(Match.match_date)
            .limit(limit)
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())


match = CRUDMatch(Match)