
from datetime import date

from fastapi import HTTPException
from sqlalchemy import func, select
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


def _normalize_name(value: str | None) -> str:
    return " ".join((value or "").casefold().split())


async def get_editable_registration(
    db: AsyncSession,
    registration_id: int,
) -> Registration:
    """
    Получает заявку с блокировкой для редактирования.

    Проверку прав администратора выполняет маршрут.
    Одобренные заявки здесь не редактируются.
    """
    registration = await db.scalar(
        select(Registration)
        .where(Registration.id == registration_id)
        .with_for_update()
    )

    if registration is None:
        raise HTTPException(
            status_code=404,
            detail="Заявка не найдена",
        )

    if registration.status not in EDITABLE:
        raise HTTPException(
            status_code=409,
            detail=(
                "Редактировать можно только заявку "
                "на проверке или на доработке"
            ),
        )

    return registration


def validate_name(
    value: str,
    field: str,
) -> str:
    """Проверяет и нормализует имя или фамилию."""
    if not isinstance(value, str):
        raise HTTPException(
            status_code=422,
            detail=f"Некорректное поле: {field}",
        )

    normalized = " ".join(value.split())

    if not normalized or len(normalized) > 100:
        raise HTTPException(
            status_code=422,
            detail=f"Некорректное поле: {field}",
        )

    return normalized


def validate_position(value: str) -> str:
    """
    Проверяет позицию игрока.

    PlayerPosition использует русские значения:
    Вратарь, Защитник, Полузащитник, Нападающий.
    """
    if isinstance(value, PlayerPosition):
        return value.value

    if not isinstance(value, str):
        raise HTTPException(
            status_code=422,
            detail="Неизвестная позиция игрока",
        )

    value = value.strip()
    valid_positions = {
        position.value
        for position in PlayerPosition
    }

    if value not in valid_positions:
        raise HTTPException(
            status_code=422,
            detail="Неизвестная позиция игрока",
        )

    return value


async def validate_player_link(
    db: AsyncSession,
    existing_player_id: int | None,
) -> Player | None:
    """Проверяет ссылку на существующего футболиста."""
    if existing_player_id is None:
        return None

    if (
        not isinstance(existing_player_id, int)
        or isinstance(existing_player_id, bool)
        or existing_player_id <= 0
    ):
        raise HTTPException(
            status_code=422,
            detail="Некорректный ID футболиста",
        )

    player = await db.get(
        Player,
        existing_player_id,
    )

    if player is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "Выбранный игрок не найден "
                "в основной базе"
            ),
        )

    return player


async def validate_roster_entry(
    db: AsyncSession,
    registration_id: int,
    jersey_number: int,
    existing_player_id: int | None,
    exclude_player_id: int | None = None,
) -> None:
    """
    Проверяет номер и повторное добавление игрока.

    exclude_player_id — ID строки RegistrationPlayer,
    которую сейчас редактируют.
    """
    if (
        not isinstance(jersey_number, int)
        or isinstance(jersey_number, bool)
        or not 1 <= jersey_number <= 99
    ):
        raise HTTPException(
            status_code=422,
            detail="Игровой номер должен быть от 1 до 99",
        )

    if existing_player_id is not None:
        await validate_player_link(
            db,
            existing_player_id,
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
                status_code=409,
                detail=f"Номер {jersey_number} уже занят",
            )

        if (
            existing_player_id is not None
            and item.existing_player_id
            == existing_player_id
        ):
            raise HTTPException(
                status_code=409,
                detail="Этот игрок уже добавлен в заявку",
            )


async def validate_roster_capacity(
    db: AsyncSession,
    registration_id: int,
) -> None:
    """Не допускает добавление 26-го футболиста."""
    count = await db.scalar(
        select(func.count(RegistrationPlayer.id))
        .where(
            RegistrationPlayer.registration_id
            == registration_id
        )
    )

    if (count or 0) >= MAX_PLAYERS:
        raise HTTPException(
            status_code=409,
            detail=(
                f"В заявке не может быть "
                f"больше {MAX_PLAYERS} игроков"
            ),
        )


async def get_registration_player(
    db: AsyncSession,
    registration_id: int,
    player_id: int,
) -> RegistrationPlayer:
    """Получает футболиста строго из указанной заявки."""
    player = await db.scalar(
        select(RegistrationPlayer)
        .where(
            RegistrationPlayer.id == player_id,
            RegistrationPlayer.registration_id
            == registration_id,
        )
        .with_for_update()
    )

    if player is None:
        raise HTTPException(
            status_code=404,
            detail="Игрок не найден в этой заявке",
        )

    return player


def validate_birth_date(value: date) -> None:
    """Проверяет дату рождения футболиста."""
    if not isinstance(value, date):
        raise HTTPException(
            status_code=422,
            detail="Некорректная дата рождения",
        )

    if value > date.today():
        raise HTTPException(
            status_code=422,
            detail="Дата рождения не может быть в будущем",
        )


def validate_coach_birth_year(year: int) -> None:
    """Проверяет год рождения тренера."""
    current_year = date.today().year

    if (
        not isinstance(year, int)
        or isinstance(year, bool)
        or not 1900 <= year <= current_year
    ):
        raise HTTPException(
            status_code=422,
            detail="Некорректный год рождения тренера",
        )
