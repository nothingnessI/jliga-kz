from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.base import CRUDBase
from app.models.document import Document
from app.schemas.document import DocumentCreate, DocumentUpdate


class CRUDDocument(CRUDBase[Document, DocumentCreate, DocumentUpdate]):
    async def get_by_tournament(
        self, db: AsyncSession, *, tournament_id: int
    ) -> list[Document]:
        stmt = (
            select(Document)
            .where(Document.tournament_id == tournament_id)
            .order_by(Document.created_at.desc())
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get_general(self, db: AsyncSession) -> list[Document]:
        stmt = select(Document).where(Document.tournament_id.is_(None))
        result = await db.execute(stmt)
        return list(result.scalars().all())


document = CRUDDocument(Document)