from app.models.enums import UserRole
from sqlalchemy import select, func
from app.models.match import Match
from app.models.tournament import Tournament
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.db.session import get_db
from app.core.security import create_access_token, verify_password
from app.crud.user import user as crud_user
from app.schemas.user import UserCreate, UserRead, Token
from app.models.user import User
from app.models.enums import UserRole

router = APIRouter()


ADMIN_EMAIL = "j-liga-tournaments@mail.ru"


@router.post("/register", response_model=UserRead, status_code=status.HTTP_201_CREATED)
async def register(user_in: UserCreate, db: AsyncSession = Depends(get_db)):
    """Регистрация нового пользователя."""
    user = await crud_user.get_by_email(db, email=user_in.email)
    if user:
        raise HTTPException(status_code=400, detail="Email уже занят")

    # Определяем роль
    role = UserRole.ADMIN if user_in.email == ADMIN_EMAIL else UserRole.USER

    # Передаем роль прямо в функцию создания CRUD
    return await crud_user.create(db, obj_in=user_in, role=role)


@router.post("/login", response_model=Token)
async def login(
    db: AsyncSession = Depends(get_db),
    form_data: OAuth2PasswordRequestForm = Depends(),
):
    """Вход по email (в поле username) и паролю."""
    user = await crud_user.get_by_email(db, email=form_data.username)
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверный email или пароль",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user.is_active:
        raise HTTPException(status_code=400, detail="Пользователь заблокирован")

    # В subject токена кладём email
    access_token = create_access_token(subject=user.email)
    return {"access_token": access_token, "token_type": "bearer"}


@router.get("/me", response_model=UserRead)
async def read_users_me(
    current_user: User = Depends(deps.get_current_active_user),
):
    """Получить данные текущего пользователя."""
    return current_user


@router.get("/dashboard", tags=["Админ"])
async def get_admin_dashboard(
        db: AsyncSession = Depends(get_db),
        current_user: User = Depends(deps.get_current_admin)
):
    """Статистика для главной страницы админки (как на скриншоте)."""
    users_count = await db.scalar(select(func.count()).select_from(User))
    tournaments_count = await db.scalar(select(func.count()).select_from(Tournament))
    matches_count = await db.scalar(select(func.count()).select_from(Match))

    return {
        "total_users": users_count,
        "total_tournaments": tournaments_count,
        "total_matches": matches_count,
        "pending_requests": 0,  # Заглушка (пока нет системы заявок на модерацию)
        "pending_news": 0  # Заглушка
    }