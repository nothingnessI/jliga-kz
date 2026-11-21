
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


def normalize_name(value: str) -> str:
    return " ".join((value or "").casefold().split())


def parse_position(value: str) -> PlayerPosition:
    try:
        return PlayerPosition(value)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"Неизвестная позиция игрока: {value}",
        )


async def approve_registration(
    db: AsyncSession,
    registration_id: int,
    admin_id: int,
    comment: str | None = None,
) -> dict:
    """
    Одобрение заявки.

    ВАЖНО:
    Функция не вызывает commit().
    Вызывающий маршрут должен выполнить commit()
    или rollback() для всей транзакции.
    """

    # Блокируем заявку от одновременной обработки.

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

    team = await db.get(Team, registration.team_id)
    tournament = await db.get(
        Tournament,
        registration.tournament_id,
    )

    if team is None or tournament is None:
        raise HTTPException(
            status_code=422,
            detail="Команда или турнир больше не существуют",
        )

    # Блокируем команду, чтобы два администратора
    # не публиковали составы одной команды одновременно.

    await db.scalar(
        select(Team)
        .where(Team.id == registration.team_id)
        .with_for_update()
    )

    application_players = (
        await db.scalars(
            select(RegistrationPlayer)
            .where(
                RegistrationPlayer.registration_id
                == registration.id
            )
            .order_by(RegistrationPlayer.id)
        )
    ).all()

    if not 1 <= len(application_players) <= 25:
        raise HTTPException(
            status_code=422,
            detail="В заявке должно быть от 1 до 25 игроков",
        )

    if not all([
        registration.coach_first_name,
        registration.coach_last_name,
        registration.coach_birth_year,
        registration.coach_photo_url,
    ]):
        raise HTTPException(
            status_code=422,
            detail="Не заполнены данные тренера",
        )

    # Получаем уже опубликованный состав команды.

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
        item.player_id: item for item in roster
    }

    # Номера в существующем составе.
    # Учитываем только активных игроков.

    numbers_in_roster = {
        item.jersey_number: item.player_id
        for item in roster
        if item.is_active
        and item.jersey_number is not None
    }

    # Проверяем все данные ДО создания новых игроков.

    existing_ids = [
        item.existing_player_id
        for item in application_players
        if item.existing_player_id is not None
    ]

    if len(existing_ids) != len(set(existing_ids)):
        raise HTTPException(
            status_code=422,
            detail="В заявке повторяется существующий игрок",
        )

    numbers = [
        item.jersey_number
        for item in application_players
    ]

    if len(numbers) != len(set(numbers)):
        raise HTTPException(
            status_code=422,
            detail="В заявке повторяются игровые номера",
        )

    existing_players = {}

    if existing_ids:
        found = (
            await db.scalars(
                select(Player)
                .where(Player.id.in_(existing_ids))
                .with_for_update()
            )
        ).all()

        existing_players = {
            player.id: player for player in found
        }

        if len(existing_players) != len(existing_ids):
            raise HTTPException(
                status_code=422,
                detail="Один из существующих игроков не найден",
            )

    # Проверяем потенциальные дубликаты среди
    # новых игроков по ФИО и дате рождения.

    new_application_players = [
        item for item in application_players
        if item.existing_player_id is None
    ]

    new_birth_dates = {
        item.birth_date
        for item in new_application_players
    }

    possible_duplicates = []

    if new_birth_dates:
        possible_duplicates = (
            await db.scalars(
                select(Player).where(
                    Player.date_of_birth.in_(new_birth_dates)
                )
            )
        ).all()

    new_identity_keys = set()

    for item in new_application_players:
        identity = (
            normalize_name(item.first_name),
            normalize_name(item.last_name),
            item.birth_date,
        )

        if identity in new_identity_keys:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"В заявке повторяется новый игрок: "
                    f"{item.last_name} {item.first_name}"
                ),
            )

        new_identity_keys.add(identity)

        for candidate in possible_duplicates:
            if (
                normalize_name(candidate.first_name)
                == identity[0]
                and normalize_name(candidate.last_name)
                == identity[1]
                and candidate.date_of_birth
                == identity[2]
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Возможный дубликат: "
                        f"{item.last_name} {item.first_name}. "
                        f"В базе уже есть игрок ID {candidate.id}. "
                        f"Свяжите заявку с существующим игроком "
                        f"перед одобрением."
                    ),
                )

    # Проверяем совпадение данных существующих игроков.

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
                    f"Данные игрока ID {player.id} "
                    f"отличаются от основной базы. "
                    f"Проверьте ФИО и дату рождения."
                ),
            )

    # Проверяем конфликты номеров с уже опубликованными
    # игроками, которые не входят в текущую заявку.

    submitted_existing_ids = set(existing_ids)

    for item in application_players:
        occupied_by = numbers_in_roster.get(
            item.jersey_number
        )

        if occupied_by is None:
            continue

        if occupied_by == item.existing_player_id:
            continue

        if occupied_by not in submitted_existing_ids:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Номер {item.jersey_number} уже занят "
                    f"другим игроком официального состава."
                ),
            )

    # Для уже существующих игроков, которые входят
    # в заявку, сначала освобождаем старые номера.
    # Это позволяет безопасно поменять номера местами.

    for player_id in submitted_existing_ids:
        roster_item = roster_by_player.get(player_id)

        if roster_item is not None:
            roster_item.jersey_number = None

    await db.flush()

    # Регистрируем команду в турнире, если её ещё нет.

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

    for item in application_players:

        position = parse_position(item.position)

        if item.existing_player_id is not None:
            # Игрок уже есть в общей базе.
            player = existing_players[item.existing_player_id]

        else:
            # Создаём нового игрока.
            player = Player(
                first_name=item.first_name.strip(),
                last_name=item.last_name.strip(),
                date_of_birth=item.birth_date,
                position=position,
                photo_url=item.photo_url,
            )

            db.add(player)
            await db.flush()

            created_players += 1

            # Запоминаем ID созданного игрока
            # в исходной заявке для повторных проверок.
            item.existing_player_id = player.id

        roster_item = roster_by_player.get(player.id)

        if roster_item is None:
            # Новая связь игрока с командой и турниром.
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
            # Игрок уже числится в команде.
            # Обновляем номер и активность,
            # не сбрасывая статистику.
            roster_item.jersey_number = item.jersey_number
            roster_item.is_active = True

            updated_roster += 1

    # Фиксируем решение администратора.

    registration.status = RegistrationStatus.APPROVED.value
    registration.admin_comment = comment or None
    registration.reviewed_by_id = admin_id
    registration.reviewed_at = datetime.now(timezone.utc)

    await db.flush()

    return {
        "registration_id": registration.id,
        "status": registration.status,
        "team_id": registration.team_id,
        "tournament_id": registration.tournament_id,
        "created_players": created_players,
        "added_to_roster": added_to_roster,
        "updated_roster": updated_roster,
    }
