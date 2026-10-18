from datetime import datetime
from pydantic import BaseModel, EmailStr, Field
from app.models.enums import UserRole
from app.schemas.base import ORMModel


class UserBase(BaseModel):
    email: EmailStr
    full_name: str | None = Field(None, max_length=255)
    # role: UserRole = UserRole.USER


class UserCreate(UserBase):
    password: str = Field(..., min_length=8, max_length=100)


class UserUpdate(BaseModel):
    full_name: str | None = Field(None, max_length=255)
    password: str | None = Field(None, min_length=8, max_length=100)
    is_active: bool | None = None


class UserRead(ORMModel):
    id: int
    email: EmailStr
    full_name: str | None
    role: UserRole
    is_active: bool
    team_id: int | None
    created_at: datetime


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class TokenData(BaseModel):
    email: str | None = None
    role: UserRole | None = None