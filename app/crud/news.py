from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.base import CRUDBase
from app.models.news import News
from app.schemas.news import NewsCreate, NewsUpdate


class CRUDNews(CRUDBase[News, NewsCreate, NewsUpdate]):
    async def get_by_slug(self, db: AsyncSession, *, slug: str) -> News | None:
        stmt = select(News).where(News.slug == slug)
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_published(
        self, db: AsyncSession, *, skip: int = 0, limit: int = 20
    ) -> list[News]:
        stmt = (
            select(News)
            .where(News.is_published.is_(True))
            .order_by(News.published_at.desc())
            .offset(skip)
            .limit(limit)
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get_by_tournament(
        self, db: AsyncSession, *, tournament_id: int
    ) -> list[News]:
        stmt = (
            select(News)
            .where(News.tournament_id == tournament_id)
            .order_by(News.published_at.desc())
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())


news = CRUDNews(News)