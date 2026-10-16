from pydantic import BaseModel, ConfigDict


class ORMModel(BaseModel):
    """Базовый класс для схем, читающих данные из ORM-объектов."""
    model_config = ConfigDict(from_attributes=True)