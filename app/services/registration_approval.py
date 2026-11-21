
import logging
from datetime import datetime, timezone

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
from app.models.team import Team
from app.models.team_player import TeamPlayer
from app.models.tournament import Tournament
from app.models.tournament_team import TournamentTeam


logger = logging.getLogger(__name__)


def normalize_name(value: str | None) -> str:
    """Нормализует ФИО для сравнения."""
    return " ".join((value or "").casefold().split())


def parse_position(value: str | PlayerPosition) -> PlayerPosition:
    """Преобразует позицию заявки в PlayerPosition."""
    if isinstance(value, PlayerPosition):
        return value

    try:
        return PlayerPosition(value)
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=422,
            detail=f"Неизвестная позиция игрока: {value}",
        )


def validate_registration_players(
    players: list[RegistrationPlayer],
) -> None:
    """Проверяет состав заявки до изменения базы данных."""

    if not 1 <= len(players) <= 25:
        raise HTTPException(
            status_code=422,
            detail="В заявке должно быть от 1 до 25 игроков",
        )

    numbers = set()
    existing_ids = set()
    identities = set()

    for item in players:
        if not all(
            (
                item.first_name and item.first_name.strip(),
                item.last_name and item.last_name.strip(),
                item.birth_date,
                item.photo_url,
            )
        ):
            raise HTTPException(
                status_code=422,
                detail=(
                    "У одного из игроков не заполнены "
                    "обязательные данные"
                ),
            )

        parse_position(item.position)

        number = item.jersey_number

        if number is None or not 1 <= number <= 99:
            raise HTTPException(
                status_code=422,
                detail="Игровые номера должны быть от 1 до 99",
            )

        if number in numbers:
            raise HTTPException(
                status_code=422,
                detail=f"Игровой номер {number} повторяется",
            )

        numbers.add(number)

        if item.existing_player_id is not None:
            if item.existing_player_id in existing_ids:
                raise HTTPException(
                    status_code=422,
                    detail="В заявке повторяется существующий игрок",
                )

            existing_ids.add(item.existing_player_id)

        identity = (
            normalize_name(item.first_name),
            normalize_name(item.last_name),
            item.birth_date,
        )

        if identity in identities:
            raise HTTPException(
                status_code=409,
                detail=(
                    "В заявке повторяется футболист: "
                    f"{item.last_name} {item.first_name}"
                ),
            )

        identities.add(identity)


async def approve_registration(
    db: AsyncSession,
    registration_id: int,
    admin_id: int,
    comment: str | None = None,
) -> dict:
    """
    Одобряет заявку и публикует официальный состав.

    Порядок:
    1. Блокировка заявки.
    2. Проверка статуса и данных.
    3. Поиск существующих игроков.
    4. Проверка дубликатов и номеров.
    5. Регистрация команды в турнире.
    6. Создание новых игроков.
    7. Обновление официального состава.
    8. Сохранение решения администратора.

    ВАЖНО:
    Метод не вызывает commit().
    Транзакцией управляет вызывающий маршрут.
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

    if registration.status != RegistrationStatus.SUBMITTED.value:
        raise HTTPException(
            status_code=409,
            detail="Одобрить можно только отправленную заявку",
        )

    # Блокируем команду, чтобы параллельные одобрения
    # заявок одной команды выполнялись последовательно.
    team = await db.scalar(
        select(Team)
        .where(Team.id == registration.team_id)
        .with_for_update()
    )

    tournament = await db.get(
        Tournament,
        registration.tournament_id,
    )

    if team is None or tournament is None:
        raise HTTPException(
            status_code=422,
            detail="Команда или турнир не найдены",
        )

    if not all(
        (
            registration.coach_first_name,
            registration.coach_last_name,
            registration.coach_birth_year,
            registration.coach_photo_url,
        )
    ):
        raise HTTPException(
            status_code=422,
            detail="Не заполнены обязательные данные тренера",
        )

    application_players = (
        await db.scalars(
            select(RegistrationPlayer)
            .where(
                RegistrationPlayer.registration_id
                == registration.id
            )
            .order_by(RegistrationPlayer.id)
            .with_for_update()
        )
    ).all()

    validate_registration_players(application_players)

    # Проверяем существующих игроков.

    existing_ids = {
        item.existing_player_id
        for item in application_players
        if item.existing_player_id is not None
    }

    existing_players = {}

    if existing_ids:
        found_players = (
            await db.scalars(
                select(Player)
                .where(Player.id.in_(existing_ids))
                .with_for_update()
            )
        ).all()

        existing_players = {
            player.id: player
            for player in found_players
        }

        missing_ids = existing_ids - set(existing_players)

        if missing_ids:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Не найдены существующие игроки: "
                    + ", ".join(map(str, sorted(missing_ids)))
                ),
            )

    # Существующие игроки должны соответствовать
    # данным, уже сохранённым в общей базе.

    for item in application_players:
        if item.existing_player_id is None:
            continue

        player = existing_players[item.existing_player_id]

        if (
            normalize_name(player.first_name)
            != normalize_name(item.first_name)
            or normalize_name(player.last_name)
            != normalize_name(item.last_name)
            or player.date_of_birth != item.birth_date
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Данные игрока ID {player.id} отличаются "
                    "от основной базы. Проверьте ФИО "
                    "и дату рождения."
                ),
            )

    # Ищем возможные дубликаты новых игроков.
    # Не создаём повторную запись Player, если
    # такой футболист уже существует.

    new_players = [
        item
        for item in application_players
        if item.existing_player_id is None
    ]

    birth_dates = {
        item.birth_date
        for item in new_players
    }

    candidates = []

    if birth_dates:
        candidates = (
            await db.scalars(
                select(Player).where(
                    Player.date_of_birth.in_(birth_dates)
                )
            )
        ).all()

    candidate_keys = {}

    for candidate in candidates:
        key = (
            normalize_name(candidate.first_name),
            normalize_name(candidate.last_name),
            candidate.date_of_birth,
        )
        candidate_keys[key] = candidate

    for item in new_players:
        key = (
            normalize_name(item.first_name),
            normalize_name(item.last_name),
            item.birth_date,
        )

        duplicate = candidate_keys.get(key)

        if duplicate is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Футболист {item.last_name} "
                    f"{item.first_name} уже существует "
                    f"в базе (ID {duplicate.id}). "
                    "Свяжите заявку с существующим игроком."
                ),
            )

    # Получаем официальный состав команды.
    # Не удаляем старых игроков автоматически.

    roster = (
        await db.scalars(
            select(TeamPlayer)
            .where(
                TeamPlayer.team_id == registration.team_id,
                TeamPlayer.tournament_id
                == registration.tournament_id,
            )
            .with_for_update()
        )
    ).all()

    roster_by_player = {
        item.player_id: item
        for item in roster
    }

    submitted_existing_ids = set(existing_ids)

    # Проверяем конфликты игровых номеров
    # с футболистами, отсутствующими в заявке.

    for roster_item in roster:
        if not roster_item.is_active:
            continue

        if roster_item.player_id in submitted_existing_ids:
            continue

        if roster_item.jersey_number is None:
            continue

        for item in application_players:
            if item.jersey_number == roster_item.jersey_number:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Номер {item.jersey_number} уже занят "
                        "игроком официального состава."
                    ),
                )

    # Освобождаем старые номера существующих игроков
    # перед назначением новых. Это позволяет
    # безопасно обменивать номера между игроками.

    for player_id in submitted_existing_ids:
        roster_item = roster_by_player.get(player_id)

        if roster_item is not None:
            roster_item.jersey_number = None

    await db.flush()

    # Регистрируем команду в турнире.

    tournament_team = await db.scalar(
        select(TournamentTeam)
        .where(
            TournamentTeam.tournament_id
            == registration.tournament_id,
            TournamentTeam.team_id
            == registration.team_id,
        )
        .with_for_update()
    )

    if tournament_team is None:
        tournament_team = TournamentTeam(
            tournament_id=registration.tournament_id,
            team_id=registration.team_id,
        )

        db.add(tournament_team)
        await db.flush()

    created_players = 0
    added_to_roster = 0
    updated_roster = 0

    # Формируем официальный состав.

    for item in application_players:
        position = parse_position(item.position)

        if item.existing_player_id is not None:
            player = existing_players[item.existing_player_id]

        else:
            player = Player(
                first_name=item.first_name.strip(),
                last_name=item.last_name.strip(),
                date_of_birth=item.birth_date,
                position=position,
                photo_url=item.photo_url,
            )

            db.add(player)
            await db.flush()

            # Сохраняем связь с созданным футболистом.
            item.existing_player_id = player.id

            created_players += 1

        roster_item = roster_by_player.get(player.id)

        if roster_item is None:
            roster_item = TeamPlayer(
                player_id=player.id,
                team_id=registration.team_id,
                tournament_id=registration.tournament_id,
                jersey_number=item.jersey_number,
                is_active=True,
                is_captain=False,
            )

            db.add(roster_item)
            roster_by_player[player.id] = roster_item

            added_to_roster += 1

        else:
            # Не сбрасываем накопленную статистику.
            roster_item.jersey_number = item.jersey_number
            roster_item.is_active = True

            updated_roster += 1

    # Фиксируем решение администратора.

    registration.status = RegistrationStatus.APPROVED.value
    registration.admin_comment = (
        comment.strip()
        if comment and comment.strip()
        else None
    )
    registration.reviewed_by_id = admin_id
    registration.reviewed_at = datetime.now(timezone.utc)

    await db.flush()

    logger.info(
        "Заявка %s одобрена администратором %s. "
        "Создано игроков: %s, добавлено в состав: %s, "
        "обновлено: %s",
        registration.id,
        admin_id,
        created_players,
        added_to_roster,
        updated_roster,
    )

    return {
        "registration_id": registration.id,
        "status": registration.status,
        "team_id": registration.team_id,
        "tournament_id": registration.tournament_id,
        "created_players": created_players,
        "added_to_roster": added_to_roster,
        "updated_roster": updated_roster,
    }
