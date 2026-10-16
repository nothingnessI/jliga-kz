from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.base import ORMModel


class StadiumBase(BaseModel):
    name: str = Field(..., min_length=2, max_length=200)
    address: str | None = Field(None, max_length=300)
    city: str | None = Field(None, max_length=100)


class StadiumCreate(StadiumBase):
    pass


class StadiumUpdate(BaseModel):
    name: str | None = Field(None, min_length=2, max_length=200)
    address: str | None = Field(None, max_length=300)
    city: str | None = Field(None, max_length=100)


class StadiumRead(ORMModel):
    id: int
    name: str
    address: str | None
    city: str | None
    created_at: datetime
    updated_at: datetime


class StadiumReadShort(ORMModel):
    id: int
    name: str
    city: str | None