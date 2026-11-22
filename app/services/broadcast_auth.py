
"""
Авторизация WebSocket-подключений Broadcast Studio.
"""

from fastapi import WebSocket
from jose import JWTError, jwt
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import ALGORITHM
from app.crud.user import user as crud_user
from app.db.session import get_db
from app.models.enums import UserRole


async def authorize_broadcast_viewer(
    websocket: WebSocket,
    session_id: int,
) -> bool:
    """
    Проверяет, что WebSocket-клиент является
    активным администратором.

    JWT передаётся через cookie access_token.
    """

    token = websocket.cookies.get("access_token")

    if not token:
        return False

    if token.lower().startswith("bearer "):
        token = token[7:]

    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[ALGORITHM],
        )

        email = payload.get("sub")

        if not isinstance(email, str) or not email:
            return False

    except JWTError:
        return False

    # Используем ту же модель доступа, что и deps.py.
    async for db in get_db():
        user = await crud_user.get_by_email(
            db,
            email=email,
        )

        return bool(
            user
            and user.is_active
            and user.role == UserRole.ADMIN
        )

    return False
