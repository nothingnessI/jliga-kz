from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import uuid
from app.crud.base import CRUDBase
from app.models.user import User
from app.schemas.user import UserCreate, UserUpdate
from app.core.security import get_password_hash
from app.models.enums import UserRole


class CRUDUser(CRUDBase[User, UserCreate, UserUpdate]):
    async def get_by_email(self, db: AsyncSession, *, email: str) -> User | None:
        stmt = select(User).where(User.email == email)
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def create(
        self, db: AsyncSession, *, obj_in: UserCreate, role: UserRole = UserRole.USER
    ) -> User:
        # Переопределяем метод create, чтобы хешировать пароль и ставить роль
        db_obj = User(
            email=obj_in.email,
            hashed_password=get_password_hash(obj_in.password),
            full_name=obj_in.full_name,
            role=role,  # <-- Берем роль из аргумента
            is_active=True,
        )
        db.add(db_obj)
        await db.commit()
        await db.refresh(db_obj)
        return db_obj


    async def create(self, db: AsyncSession, *, obj_in: UserCreate, role: UserRole = UserRole.USER) -> User:
        # Генерируем токен для подтверждения почты
        verify_token = str(uuid.uuid4())

        db_obj = User(
            email=obj_in.email,
            hashed_password=get_password_hash(obj_in.password),
            first_name=obj_in.first_name,
            last_name=obj_in.last_name,
            phone=obj_in.phone,
            role=role,
            is_active=True,
            is_verified=False,  # Изначально почта не подтверждена
            verification_token=verify_token
        )
        db.add(db_obj)
        await db.commit()
        await db.refresh(db_obj)
        return db_obj

user = CRUDUser(User)