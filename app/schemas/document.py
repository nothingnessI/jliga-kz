from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.base import ORMModel


class DocumentBase(BaseModel):
    title: str = Field(..., min_length=2, max_length=300)
    file_url: str = Field(..., min_length=3, max_length=500)
    tournament_id: int | None = None


class DocumentCreate(DocumentBase):
    pass


class DocumentUpdate(BaseModel):
    title: str | None = Field(None, min_length=2, max_length=300)
    file_url: str | None = Field(None, min_length=3, max_length=500)
    tournament_id: int | None = None


class DocumentRead(ORMModel):
    id: int
    title: str
    file_url: str
    tournament_id: int | None
    created_at: datetime
    updated_at: datetime


class DocumentReadShort(ORMModel):
    id: int
    title: str
    file_url: str