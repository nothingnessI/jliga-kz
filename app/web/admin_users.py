
"""J-LIGA: управление пользователями из административной панели.

Подключить один раз в app/main.py:
    from app.web.admin_users import router as admin_users_router
    app.include_router(admin_users_router)

Используются существующие users и teams, миграций нет.
"""

import re
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_current_admin
from app.core.security import get_password_hash
from app.db.session import get_db
from app.models.enums import UserRole
from app.models.team import Team
from app.models.user import User


router = APIRouter(tags=["Admin Users"])
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parents[1] / "templates")
)
ADMIN_LOCK_KEY = 73910652
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class UserCreateIn(BaseModel):
    email: str = Field(min_length=5, max_length=255)
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    phone: str | None = Field(default=None, max_length=20)
    role: UserRole = UserRole.USER
    team_id: int | None = Field(default=None, gt=0)
    is_verified: bool = True

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        value = value.strip().lower()
        if not EMAIL_PATTERN.fullmatch(value):
            raise ValueError("Укажите корректный email")
        return value

    @field_validator("first_name", "last_name")
    @classmethod
    def nonempty_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Поле не может быть пустым")
        return value

    @field_validator("phone")
    @classmethod
    def normalize_phone(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None


class UserUpdateIn(BaseModel):
    email: str | None = Field(default=None, min_length=5, max_length=255)
    first_name: str | None = Field(default=None, min_length=1, max_length=100)
    last_name: str | None = Field(default=None, min_length=1, max_length=100)
    phone: str | None = Field(default=None, max_length=20)
    role: UserRole | None = None
    is_active: bool | None = None
    is_verified: bool | None = None
    team_id: int | None = Field(default=None, gt=0)

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str | None) -> str | None:
        return UserCreateIn.valid_email(value) if value is not None else None

    @field_validator("first_name", "last_name")
    @classmethod
    def nonempty_name(cls, value: str | None) -> str | None:
        return UserCreateIn.nonempty_name(value) if value is not None else None

    @field_validator("phone")
    @classmethod
    def normalize_phone(cls, value: str | None) -> str | None:
        return UserCreateIn.normalize_phone(value)


def check_write_request(request: Request) -> None:
    """Защита cookie-сессии от сторонних POST/PATCH-запросов."""
    if request.headers.get("x-jliga-admin-action") != "1":
        raise HTTPException(status_code=403, detail="Отсутствует защитный заголовок")

    origin = request.headers.get("origin") or request.headers.get("referer")
    if origin:
        source = urlsplit(origin)
        if (
            source.scheme not in {"http", "https"}
            or source.netloc.lower() != request.headers.get("host", "").lower()
        ):
            raise HTTPException(status_code=403, detail="Недопустимый источник запроса")


def serialize_user(user: User) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "phone": user.phone,
        "role": user.role.value,
        "is_active": user.is_active,
        "is_verified": user.is_verified,
        "team_id": user.team_id,
        "team_name": user.team.name if user.team else None,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


async def validate_team(db: AsyncSession, team_id: int | None) -> None:
    if team_id is not None and await db.get(Team, team_id) is None:
        raise HTTPException(status_code=422, detail="Команда не найдена")


async def lock_admin_changes(db: AsyncSession) -> None:
    # Сериализует изменения ролей/активности при параллельном редактировании.
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": ADMIN_LOCK_KEY})


async def require_another_active_admin(db: AsyncSession, user: User) -> None:
    if user.role != UserRole.ADMIN or not user.is_active:
        return
    number = await db.scalar(
        select(func.count()).select_from(User).where(
            User.role == UserRole.ADMIN,
            User.is_active.is_(True),
        )
    )
    if (number or 0) <= 1:
        raise HTTPException(status_code=409, detail="Нельзя отключить последнего активного администратора")


@router.get("/admin/users", response_class=HTMLResponse)
async def admin_users_page(
    request: Request,
    admin: User = Depends(get_current_admin),
):
    return templates.TemplateResponse(
        request=request,
        name="admin_users.html",
        context={"user": admin},
    )


@router.get("/admin/api/users")
async def users_list(
    response: Response,
    q: str = Query(default="", max_length=120),
    role: UserRole | None = None,
    active: str = Query(default="all", pattern="^(all|active|blocked)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=15, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    response.headers["Cache-Control"] = "no-store"
    where = []
    if q.strip():
        # % и _ не должны превращать ввод в шаблон SQL LIKE.
        escaped = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        fragment = f"%{escaped}%"
        where.append(or_(
            User.email.ilike(fragment, escape="\\"),
            User.first_name.ilike(fragment, escape="\\"),
            User.last_name.ilike(fragment, escape="\\"),
            User.phone.ilike(fragment, escape="\\"),
        ))
    if role is not None:
        where.append(User.role == role)
    if active != "all":
        where.append(User.is_active.is_(active == "active"))

    total = await db.scalar(select(func.count()).select_from(User).where(*where)) or 0
    users = (await db.scalars(
        select(User)
        .where(*where)
        .options(selectinload(User.team))
        .order_by(User.created_at.desc(), User.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )).all()

    counts = (await db.execute(
        select(User.is_active, func.count()).group_by(User.is_active)
    )).all()
    active_count = sum(count for active_value, count in counts if active_value)
    all_count = sum(count for _, count in counts)

    return {
        "items": [serialize_user(u) for u in users],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": max(1, (total + page_size - 1) // page_size),
        "stats": {
            "total": all_count,
            "active": active_count,
            "blocked": all_count - active_count,
        },
        "current_admin_id": admin.id,
    }


@router.get("/admin/api/users/teams")
async def users_teams(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    teams = (await db.scalars(select(Team).order_by(Team.name).limit(1000))).all()
    return [{"id": team.id, "name": team.name} for team in teams]


@router.post("/admin/api/users", status_code=status.HTTP_201_CREATED)
async def create_user(
    data: UserCreateIn,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    check_write_request(request)
    await validate_team(db, data.team_id)

    existing = await db.scalar(select(User.id).where(func.lower(User.email) == data.email))
    if existing is not None:
        raise HTTPException(status_code=409, detail="Этот email уже занят")

    # Не отправляем пароль на email автоматически: показываем админу один раз.
    temporary_password = secrets.token_urlsafe(20)
    user = User(
        email=data.email,
        hashed_password=get_password_hash(temporary_password),
        first_name=data.first_name,
        last_name=data.last_name,
        phone=data.phone,
        role=data.role,
        is_active=True,
        is_verified=data.is_verified,
        team_id=data.team_id,
    )
    db.add(user)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Email уже существует или неверная команда") from exc

    await db.refresh(user)
    response.headers["Cache-Control"] = "no-store"
    return {"user": {"id": user.id, "email": user.email}, "temporary_password": temporary_password}


@router.patch("/admin/api/users/{user_id}")
async def update_user(
    user_id: int,
    data: UserUpdateIn,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    check_write_request(request)
    changes = data.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail="Нет изменений")

    if any(k in changes and changes[k] is None for k in (
        "email", "first_name", "last_name", "role", "is_active", "is_verified"
    )):
        raise HTTPException(status_code=422, detail="Обязательное поле не может быть пустым")

    await lock_admin_changes(db)
    user = await db.scalar(select(User).where(User.id == user_id).with_for_update())
    if user is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    new_role = changes.get("role", user.role)
    new_active = changes.get("is_active", user.is_active)

    if user_id == admin.id:
        if "email" in changes and changes["email"] != user.email:
            raise HTTPException(status_code=409, detail="Смените собственный email через профиль, чтобы не потерять сессию")
        if new_role != UserRole.ADMIN or new_active is False:
            raise HTTPException(status_code=409, detail="Нельзя заблокировать себя или снять свою роль администратора")

    if user.role == UserRole.ADMIN and user.is_active and (
        new_role != UserRole.ADMIN or not new_active
    ):
        await require_another_active_admin(db, user)

    if "team_id" in changes:
        await validate_team(db, changes["team_id"])

    if "email" in changes and changes["email"] != user.email:
        exists = await db.scalar(select(User.id).where(
            func.lower(User.email) == changes["email"], User.id != user.id,
        ))
        if exists is not None:
            raise HTTPException(status_code=409, detail="Email уже занят")

    for field, value in changes.items():
        setattr(user, field, value)

    # Старые ссылки сброса/верификации не должны оставаться рабочими после смены email.
    if "email" in changes:
        user.reset_token = None
        user.verification_token = None

    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Конфликт email или команды") from exc
    response.headers["Cache-Control"] = "no-store"
    return {"ok": True, "id": user_id}


@router.post("/admin/api/users/{user_id}/reset-password")
async def reset_user_password(
    user_id: int,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    check_write_request(request)
    user = await db.scalar(select(User).where(User.id == user_id).with_for_update())
    if user is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    if user.id == admin.id:
        raise HTTPException(status_code=409, detail="Свой пароль изменяйте через личные настройки")

    temporary_password = secrets.token_urlsafe(20)
    user.hashed_password = get_password_hash(temporary_password)
    user.reset_token = None
    await db.commit()
    response.headers["Cache-Control"] = "no-store"
    return {"temporary_password": temporary_password, "email": user.email}
