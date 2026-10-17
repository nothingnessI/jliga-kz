from typing import Any, Generic, Sequence, Type, TypeVar

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import Base  # ← твой путь

ModelType = TypeVar("ModelType", bound=Base)
CreateSchemaType = TypeVar("CreateSchemaType", bound=BaseModel)
UpdateSchemaType = TypeVar("UpdateSchemaType", bound=BaseModel)


class CRUDBase(Generic[ModelType, CreateSchemaType, UpdateSchemaType]):
    def __init__(self, model: Type[ModelType]) -> None:
        self.model = model

    async def get(self, db: AsyncSession, obj_id: int) -> ModelType | None:
        return await db.get(self.model, obj_id)

    async def get_multi(
        self,
        db: AsyncSession,
        *,
        skip: int = 0,
        limit: int = 100,
    ) -> Sequence[ModelType]:
        stmt = select(self.model).offset(skip).limit(limit)
        result = await db.execute(stmt)
        return result.scalars().all()

    async def create(
        self,
        db: AsyncSession,
        *,
        obj_in: CreateSchemaType,
        **extra: Any,
    ) -> ModelType:
        data = obj_in.model_dump(exclude_unset=True)
        data.update(extra)
        db_obj = self.model(**data)
        db.add(db_obj)
        await db.commit()
        await db.refresh(db_obj)
        return db_obj

    async def update(
        self,
        db: AsyncSession,
        *,
        db_obj: ModelType,
        obj_in: UpdateSchemaType | dict[str, Any],
    ) -> ModelType:
        if isinstance(obj_in, dict):
            update_data = obj_in
        else:
            update_data = obj_in.model_dump(exclude_unset=True)

        for field, value in update_data.items():
            setattr(db_obj, field, value)

        db.add(db_obj)
        await db.commit()
        await db.refresh(db_obj)
        return db_obj

    async def delete(self, db: AsyncSession, *, db_obj: ModelType) -> None:
        await db.delete(db_obj)
        await db.commit()