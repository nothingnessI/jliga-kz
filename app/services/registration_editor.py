
from datetime import date

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import PlayerPosition
from app.models.player import Player
from app.models.registration import (
    Registration,
    RegistrationPlayer,
    RegistrationStatus,
)


EDITABLE = {
    RegistrationStatus.SUBMITTED.value,
    RegistrationStatus.REVISION.value,
}

MAX_PLAYERS = 25


async def get_editable_registration(
    db: AsyncSession,
    registration_id: int,
) -> Registration:
    registration = await db.scalar(
        select(Registration)
        .where(Registration.id == registration_id)
        .with_for_update()
    )

    if registration is None:
        raise HTTPException(404, "Заявка не найдена")

    if registration.status not in EDITABLE:
        raise HTTPException(
            409,
            "Редактировать можно только заявку "
            "на проверке или на доработке",
        )

    return registration


def validate_name(value: str, field: str) -> str:
    value = " ".join(value.strip().split())

    if not value or len(value) > 100:
        raise HTTPException(
            422,
            f"Некорректное поле: {field}",
        )

    return value


def validate_position(value: str) -> str:
    positions = {item.value for item in PlayerPosition}

    if value not in positions:
        raise HTTPException(422, "Неизвестная позиция")

    return value


async def validate_player_link(
    db: AsyncSession,
    existing_player_id: int | None,
) -> Player | None:
    if existing_player_id is None:
        return None

    player = await db.get(Player, existing_player_id)

    if player is None:
        raise HTTPException(
            422,
            "Выбранный игрок не найден в основной базе",
        )

    return player


async def validate_roster_entry(
    db: AsyncSession,
    registration_id: int,
    jersey_number: int,
    existing_player_id: int | None,
    exclude_player_id: int | None = None,
) -> None:
    if not 1 <= jersey_number <= 99:
        raise HTTPException(
            422,
            "Игровой номер должен быть от 1 до 99",
        )

    stmt = select(RegistrationPlayer).where(
        RegistrationPlayer.registration_id
        == registration_id
    )

    if exclude_player_id is not None:
        stmt = stmt.where(
            RegistrationPlayer.id != exclude_player_id
        )

    roster = (await db.scalars(stmt)).all()

    for item in roster:
        if item.jersey_number == jersey_number:
            raise HTTPException(
                409,
                f"Номер {jersey_number} уже занят",
            )

        if (
            existing_player_id is not None
            and item.existing_player_id
            == existing_player_id
        ):
            raise HTTPException(
                409,
                "Этот игрок уже добавлен в заявку",
            )


async def validate_roster_capacity(
    db: AsyncSession,
    registration_id: int,
) -> None:
    players = (
        await db.scalars(
            select(RegistrationPlayer.id).where(
                RegistrationPlayer.registration_id
                == registration_id
            )
        )
    ).all()

    if len(players) >= MAX_PLAYERS:
        raise HTTPException(
            409,
            "В заявке не может быть больше 25 игроков",
        )


async def get_registration_player(
    db: AsyncSession,
    registration_id: int,
    player_id: int,
) -> RegistrationPlayer:
    player = await db.scalar(
        select(RegistrationPlayer).where(
            RegistrationPlayer.id == player_id,
            RegistrationPlayer.registration_id
            == registration_id,
        )
    )

    if player is None:
        raise HTTPException(
            404,
            "Игрок не найден в этой заявке",
        )

    return player


def validate_birth_date(value: date) -> None:
    if value > date.today():
        raise HTTPException(
            422,
            "Дата рождения не может быть в будущем",
        )


def validate_coach_birth_year(year: int) -> None:
    current_year = date.today().year

    if not 1900 <= year <= current_year:
        raise HTTPException(
            422,
            "Некорректный год рождения тренера",
        )
