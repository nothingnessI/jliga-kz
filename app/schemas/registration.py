
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, model_validator


Position = Literal[
    "Вратарь",
    "Защитник",
    "Полузащитник",
    "Нападающий",
]


class RegistrationPlayerInput(BaseModel):
    existing_player_id: int | None = Field(default=None, gt=0)

    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)

    birth_date: date
    position: Position

    jersey_number: int = Field(ge=1, le=99)

    photo_url: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def check_birth_date(self):
        if self.birth_date > date.today():
            raise ValueError("Дата рождения не может быть в будущем")
        return self


class CoachInput(BaseModel):
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)

    birth_year: int = Field(ge=1900, le=2100)

    photo_url: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def check_birth_year(self):
        if self.birth_year > date.today().year:
            raise ValueError("Некорректный год рождения")
        return self


class RegistrationInput(BaseModel):
    team_id: int = Field(gt=0)
    tournament_id: int = Field(gt=0)

    coach: CoachInput

    players: list[RegistrationPlayerInput] = Field(
        min_length=1,
        max_length=25,
    )

    @model_validator(mode="after")
    def check_duplicates(self):
        numbers = [p.jersey_number for p in self.players]

        if len(numbers) != len(set(numbers)):
            raise ValueError(
                "Игровые номера в одной заявке не должны повторяться"
            )

        player_ids = [
            p.existing_player_id
            for p in self.players
            if p.existing_player_id is not None
        ]

        if len(player_ids) != len(set(player_ids)):
            raise ValueError(
                "Нельзя добавить одного игрока дважды"
            )

        return self
