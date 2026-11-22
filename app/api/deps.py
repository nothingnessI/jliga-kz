
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import get_db
from app.core.security import ALGORITHM
from app.crud.user import user as crud_user
from app.models.user import User
from app.models.enums import UserRole
from app.schemas.user import TokenData


oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl="/auth/login",
    auto_error=False,
)


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
    bearer_token: str | None = Depends(oauth2_scheme),
) -> User:

    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    # 1. Сначала проверяем Bearer JWT.
    token = bearer_token

    # 2. Если Bearer отсутствует, проверяем cookie.
    if not token:
        token = request.cookies.get("access_token")

    if not token:
        raise credentials_exception

    # Поддержка cookie, если в ней хранится
    # строка с префиксом Bearer.
    if token.lower().startswith("bearer "):
        token = token[7:].strip()

    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[ALGORITHM],
        )

        email = payload.get("sub")

        if not isinstance(email, str) or not email:
            raise credentials_exception

        token_data = TokenData(email=email)

    except JWTError:
        raise credentials_exception

    user = await crud_user.get_by_email(
        db,
        email=token_data.email,
    )

    if user is None:
        raise credentials_exception

    return user


async def get_current_active_user(
    current_user: User = Depends(get_current_user),
) -> User:

    if not current_user.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Inactive user",
        )

    return current_user


async def get_current_admin(
    current_user: User = Depends(get_current_active_user),
) -> User:

    if current_user.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not enough permissions",
        )

    return current_user
