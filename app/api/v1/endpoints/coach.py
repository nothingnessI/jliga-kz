
import re
from datetime import datetime, timezone
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    status,
)
from fastapi.security import (
    HTTPAuthorizationCredentials,
    HTTPBearer,
)
from jose import JWTError, jwt
from pydantic import BaseModel, Field
from sqlalchemy import delete, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.security import (
    ALGORITHM,
    create_access_token,
    get_password_hash,
    verify_password,
)
from app.db.session import get_db

from app.models.enums import UserRole
from app.models.player import Player
from app.models.registration import (
    Registration,
    RegistrationPlayer,
    RegistrationStatus,
)
from app.models.team import Team
from app.models.tournament import Tournament
from app.models.user import User

from app.schemas.registration import RegistrationInput


# ============================================================
# ROUTER
# ============================================================

router = APIRouter()

bearer_scheme = HTTPBearer(auto_error=False)


# ============================================================
# CONSTANTS
# ============================================================

EDITABLE_STATUSES = {
    RegistrationStatus.DRAFT.value,
    RegistrationStatus.REVISION.value,
}

MIN_PLAYERS = 1
MAX_PLAYERS = 25


# ============================================================
# SCHEMAS: AUTH
# ============================================================

class CoachCredentials(BaseModel):
    phone: str = Field(
        min_length=10,
        max_length=25,
    )

    password: str = Field(
        min_length=10,
        max_length=72,
    )


class CoachRegister(CoachCredentials):
    first_name: str = Field(
        min_length=1,
        max_length=100,
    )

    last_name: str = Field(
        min_length=1,
        max_length=100,
    )


# ============================================================
# HELPERS: PHONE
# ============================================================

def normalize_phone(phone: str) -> str:
    """
    Нормализует телефон к формату +7XXXXXXXXXX.

    Поддерживает:
        87001234567
        77001234567
        +7 700 123 45 67
    """

    digits = re.sub(r"\D", "", phone)

    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]

    if len(digits) != 11 or not digits.startswith("7"):
        raise HTTPException(
            status_code=422,
            detail="Введите номер в формате +7XXXXXXXXXX",
        )

    return f"+{digits}"


def validate_password(password: str) -> None:
    """
    Ограничение bcrypt: 72 байта.
    """

    if len(password.encode("utf-8")) > 72:
        raise HTTPException(
            status_code=422,
            detail="Пароль слишком длинный",
        )


# ============================================================
# HELPERS: STRING
# ============================================================

def clean_text(value: str | None) -> str:
    if value is None:
        return ""

    return value.strip()


def enum_value(value):
    """
    Возвращает строковое значение Enum,
    если поле является Enum.
    """

    if value is None:
        return None

    return getattr(value, "value", value)


# ============================================================
# DEPENDENCY: CURRENT COACH
# ============================================================

async def get_current_coach(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(bearer_scheme),
    ],
    db: AsyncSession = Depends(get_db),
) -> User:

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Необходимо авторизоваться",
            headers={
                "WWW-Authenticate": "Bearer",
            },
        )

    try:
        payload = jwt.decode(
            credentials.credentials,
            settings.SECRET_KEY,
            algorithms=[ALGORITHM],
        )

        subject = payload.get("sub")

        if (
            not isinstance(subject, str)
            or not subject.startswith("coach:")
        ):
            raise ValueError("Invalid token type")

        coach_id = int(
            subject.split(":", 1)[1]
        )

        if coach_id <= 0:
            raise ValueError("Invalid coach ID")

    except (JWTError, ValueError, TypeError):
        raise HTTPException(
            status_code=401,
            detail="Недействительный или просроченный токен",
            headers={
                "WWW-Authenticate": "Bearer",
            },
        )

    coach = await db.get(User, coach_id)

    if coach is None:
        raise HTTPException(
            status_code=401,
            detail="Пользователь не найден",
        )

    if not coach.is_active:
        raise HTTPException(
            status_code=403,
            detail="Аккаунт заблокирован",
        )

    if coach.role != UserRole.COACH:
        raise HTTPException(
            status_code=403,
            detail="Доступ разрешён только тренерам",
        )

    return coach


# ============================================================
# AUTH: REGISTER
# ============================================================

@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
)
async def register_coach(
    data: CoachRegister,
    db: AsyncSession = Depends(get_db),
):

    phone = normalize_phone(data.phone)

    validate_password(data.password)

    first_name = clean_text(data.first_name)
    last_name = clean_text(data.last_name)

    if not first_name or not last_name:
        raise HTTPException(
            status_code=422,
            detail="Имя и фамилия обязательны",
        )

    existing_user = await db.scalar(
        select(User).where(
            User.phone_normalized == phone
        )
    )

    if existing_user is not None:
        raise HTTPException(
            status_code=409,
            detail="Этот номер телефона уже зарегистрирован",
        )

    coach = User(
        first_name=first_name,
        last_name=last_name,
        phone=phone,
        phone_normalized=phone,
        email=None,
        hashed_password=get_password_hash(
            data.password
        ),
        role=UserRole.COACH,
        is_active=True,
        is_verified=False,
    )

    db.add(coach)

    try:
        await db.commit()

    except IntegrityError:
        await db.rollback()

        raise HTTPException(
            status_code=409,
            detail="Не удалось зарегистрировать номер",
        )

    await db.refresh(coach)

    return {
        "message": "Аккаунт тренера создан",
        "coach_id": coach.id,
        "phone": coach.phone_normalized,
    }


# ============================================================
# AUTH: LOGIN
# ============================================================

@router.post("/login")
async def login_coach(
    data: CoachCredentials,
    db: AsyncSession = Depends(get_db),
):

    phone = normalize_phone(data.phone)

    coach = await db.scalar(
        select(User).where(
            User.phone_normalized == phone,
            User.role == UserRole.COACH,
        )
    )

    if (
        coach is None
        or not coach.is_active
        or not verify_password(
            data.password,
            coach.hashed_password,
        )
    ):
        raise HTTPException(
            status_code=401,
            detail="Неверный номер телефона или пароль",
        )

    access_token = create_access_token(
        subject=f"coach:{coach.id}"
    )

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "coach": {
            "id": coach.id,
            "first_name": coach.first_name,
            "last_name": coach.last_name,
            "phone": coach.phone_normalized,
        },
    }


# ============================================================
# PROFILE: ME
# ============================================================

@router.get("/me")
async def get_my_profile(
    coach: User = Depends(get_current_coach),
):

    return {
        "id": coach.id,
        "first_name": coach.first_name,
        "last_name": coach.last_name,
        "phone": coach.phone_normalized,
        "role": enum_value(coach.role),
    }


# ============================================================
# REFERENCE DATA: TEAMS
# ============================================================

@router.get("/teams")
async def get_available_teams(
    coach: User = Depends(get_current_coach),
    db: AsyncSession = Depends(get_db),
):

    teams = (
        await db.scalars(
            select(Team).order_by(
                Team.name.asc()
            )
        )
    ).all()

    return [
        {
            "id": team.id,
            "name": team.name,
        }
        for team in teams
    ]


# ============================================================
# REFERENCE DATA: TOURNAMENTS
# ============================================================

@router.get("/tournaments")
async def get_available_tournaments(
    coach: User = Depends(get_current_coach),
    db: AsyncSession = Depends(get_db),
):

    tournaments = (
        await db.scalars(
            select(Tournament).order_by(
                Tournament.id.desc()
            )
        )
    ).all()

    return [
        {
            "id": tournament.id,
            "name": tournament.name,
        }
        for tournament in tournaments
    ]


# ============================================================
# REFERENCE DATA: SEARCH PLAYERS
# ============================================================

@router.get("/players/search")
async def search_existing_players(
    q: str = Query(
        min_length=2,
        max_length=100,
    ),
    coach: User = Depends(get_current_coach),
    db: AsyncSession = Depends(get_db),
):

    search_text = clean_text(q)

    if len(search_text) < 2:
        raise HTTPException(
            status_code=422,
            detail="Введите минимум два символа",
        )

    escaped = (
        search_text
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )

    pattern = f"%{escaped}%"

    full_name = (
        Player.first_name
        + " "
        + Player.last_name
    )

    reversed_name = (
        Player.last_name
        + " "
        + Player.first_name
    )

    stmt = (
        select(Player)
        .where(
            or_(
                Player.first_name.ilike(
                    pattern,
                    escape="\\",
                ),
                Player.last_name.ilike(
                    pattern,
                    escape="\\",
                ),
                full_name.ilike(
                    pattern,
                    escape="\\",
                ),
                reversed_name.ilike(
                    pattern,
                    escape="\\",
                ),
            )
        )
        .order_by(
            Player.last_name.asc(),
            Player.first_name.asc(),
        )
        .limit(20)
    )

    players = (
        await db.scalars(stmt)
    ).all()

    return [
        {
            "id": player.id,
            "first_name": player.first_name,
            "last_name": player.last_name,
            "birth_date": player.date_of_birth,
            "position": enum_value(
                player.position
            ),
            "photo_url": player.photo_url,
        }
        for player in players
    ]


# ============================================================
# VALIDATION: APPLICATION REFERENCES
# ============================================================

async def validate_application_references(
    db: AsyncSession,
    data: RegistrationInput,
) -> None:

    team = await db.get(
        Team,
        data.team_id,
    )

    if team is None:
        raise HTTPException(
            status_code=422,
            detail="Выбранная команда не существует",
        )

    tournament = await db.get(
        Tournament,
        data.tournament_id,
    )

    if tournament is None:
        raise HTTPException(
            status_code=422,
            detail="Выбранный турнир не существует",
        )

    existing_player_ids = [
        player.existing_player_id
        for player in data.players
        if player.existing_player_id is not None
    ]

    # Один и тот же игрок не может быть
    # добавлен дважды в одну заявку.

    if len(existing_player_ids) != len(
        set(existing_player_ids)
    ):
        raise HTTPException(
            status_code=422,
            detail="Один игрок выбран несколько раз",
        )

    # Номера должны быть уникальными
    # внутри одной заявки.

    jersey_numbers = [
        player.jersey_number
        for player in data.players
    ]

    if len(jersey_numbers) != len(
        set(jersey_numbers)
    ):
        raise HTTPException(
            status_code=422,
            detail="Игровые номера не должны повторяться",
        )

    if existing_player_ids:
        found_ids = set(
            (
                await db.scalars(
                    select(Player.id).where(
                        Player.id.in_(
                            existing_player_ids
                        )
                    )
                )
            ).all()
        )

        if found_ids != set(existing_player_ids):
            raise HTTPException(
                status_code=422,
                detail=(
                    "Один или несколько выбранных "
                    "игроков отсутствуют в базе"
                ),
            )


# ============================================================
# VALIDATION: APPLICATION CONTENT
# ============================================================

def validate_application_content(
    application: Registration,
    players: list[RegistrationPlayer],
) -> None:

    if not MIN_PLAYERS <= len(players) <= MAX_PLAYERS:
        raise HTTPException(
            status_code=422,
            detail=(
                "В заявке должно быть "
                f"от {MIN_PLAYERS} до {MAX_PLAYERS} игроков"
            ),
        )

    # Тренер

    if not all([
        clean_text(application.coach_first_name),
        clean_text(application.coach_last_name),
        application.coach_birth_year is not None,
        clean_text(application.coach_photo_url),
    ]):
        raise HTTPException(
            status_code=422,
            detail="Заполните все данные тренера",
        )

    # Игроки

    numbers = set()
    existing_ids = set()

    for player in players:

        if not all([
            clean_text(player.first_name),
            clean_text(player.last_name),
            player.birth_date is not None,
            player.position is not None,
            player.jersey_number is not None,
            clean_text(player.photo_url),
        ]):
            raise HTTPException(
                status_code=422,
                detail=(
                    "У всех игроков должны быть "
                    "заполнены обязательные поля"
                ),
            )

        if not 1 <= player.jersey_number <= 99:
            raise HTTPException(
                status_code=422,
                detail="Игровой номер должен быть от 1 до 99",
            )

        if player.jersey_number in numbers:
            raise HTTPException(
                status_code=422,
                detail="Игровые номера повторяются",
            )

        numbers.add(player.jersey_number)

        if player.existing_player_id is not None:

            if player.existing_player_id in existing_ids:
                raise HTTPException(
                    status_code=422,
                    detail="Один игрок добавлен несколько раз",
                )

            existing_ids.add(
                player.existing_player_id
            )


# ============================================================
# HELPERS: GET OWNED APPLICATION
# ============================================================

async def get_owned_application(
    db: AsyncSession,
    application_id: int,
    coach_id: int,
    *,
    lock: bool = False,
) -> Registration:

    stmt = select(Registration).where(
        Registration.id == application_id,
        Registration.author_id == coach_id,
    )

    if lock:
        stmt = stmt.with_for_update()
    else:
        stmt = stmt.options(
            selectinload(Registration.players)
        )

    application = await db.scalar(stmt)

    if application is None:
        raise HTTPException(
            status_code=404,
            detail="Заявка не найдена",
        )

    return application


# ============================================================
# HELPERS: FILL APPLICATION
# ============================================================

def fill_application_fields(
    application: Registration,
    data: RegistrationInput,
) -> None:

    application.team_id = data.team_id
    application.tournament_id = data.tournament_id

    application.coach_first_name = clean_text(
        data.coach.first_name
    )

    application.coach_last_name = clean_text(
        data.coach.last_name
    )

    application.coach_birth_year = (
        data.coach.birth_year
    )

    application.coach_photo_url = clean_text(
        data.coach.photo_url
    )


# ============================================================
# HELPERS: BUILD PLAYERS
# ============================================================

def build_application_players(
    data: RegistrationInput,
) -> list[RegistrationPlayer]:

    result = []

    for player in data.players:

        registration_player = RegistrationPlayer(
            existing_player_id=player.existing_player_id,
            first_name=clean_text(player.first_name),
            last_name=clean_text(player.last_name),
            birth_date=player.birth_date,
            position=player.position,
            jersey_number=player.jersey_number,
            photo_url=clean_text(player.photo_url),
        )

        result.append(registration_player)

    return result


# ============================================================
# HELPERS: APPLICATION SUMMARY
# ============================================================

def application_summary(
    application: Registration,
    players_count: int,
) -> dict:

    return {
        "id": application.id,
        "team_id": application.team_id,
        "tournament_id": application.tournament_id,
        "status": application.status,
        "players_count": players_count,
        "created_at": application.created_at,
        "updated_at": application.updated_at,
        "submitted_at": application.submitted_at,
        "admin_comment": application.admin_comment,
        "reviewed_at": application.reviewed_at,
    }


# ============================================================
# HELPERS: APPLICATION DETAIL
# ============================================================

def application_detail(
    application: Registration,
) -> dict:

    return {
        **application_summary(
            application,
            len(application.players),
        ),
        "coach": {
            "first_name": application.coach_first_name,
            "last_name": application.coach_last_name,
            "birth_year": application.coach_birth_year,
            "photo_url": application.coach_photo_url,
        },
        "players": [
            {
                "id": player.id,
                "existing_player_id": (
                    player.existing_player_id
                ),
                "first_name": player.first_name,
                "last_name": player.last_name,
                "birth_date": player.birth_date,
                "position": enum_value(
                    player.position
                ),
                "jersey_number": player.jersey_number,
                "photo_url": player.photo_url,
            }
            for player in application.players
        ],
    }


# ============================================================
# APPLICATIONS: CREATE
# ============================================================

@router.post(
    "/applications",
    status_code=status.HTTP_201_CREATED,
)
async def create_application(
    data: RegistrationInput,
    coach: User = Depends(get_current_coach),
    db: AsyncSession = Depends(get_db),
):

    await validate_application_references(
        db,
        data,
    )

    application = Registration(
        author_id=coach.id,
        team_id=data.team_id,
        tournament_id=data.tournament_id,
        status=RegistrationStatus.DRAFT.value,
    )

    fill_application_fields(
        application,
        data,
    )

    application.players = build_application_players(
        data
    )

    db.add(application)

    try:
        await db.commit()

    except IntegrityError:
        await db.rollback()

        raise HTTPException(
            status_code=409,
            detail="Не удалось сохранить заявку",
        )

    await db.refresh(application)

    return {
        "id": application.id,
        "status": application.status,
        "message": "Черновик заявки создан",
    }


# ============================================================
# APPLICATIONS: LIST
# ============================================================

@router.get("/applications")
async def get_my_applications(
    coach: User = Depends(get_current_coach),
    db: AsyncSession = Depends(get_db),
):

    stmt = (
        select(Registration)
        .options(
            selectinload(Registration.players)
        )
        .where(
            Registration.author_id == coach.id
        )
        .order_by(
            Registration.id.desc()
        )
    )

    applications = (
        await db.scalars(stmt)
    ).all()

    return [
        application_summary(
            application,
            len(application.players),
        )
        for application in applications
    ]


# ============================================================
# APPLICATIONS: DETAIL
# ============================================================

@router.get("/applications/{application_id}")
async def get_application(
    application_id: int,
    coach: User = Depends(get_current_coach),
    db: AsyncSession = Depends(get_db),
):

    application = await get_owned_application(
        db,
        application_id,
        coach.id,
    )

    return application_detail(application)


# ============================================================
# APPLICATIONS: UPDATE
# ============================================================

@router.put("/applications/{application_id}")
async def update_application(
    application_id: int,
    data: RegistrationInput,
    coach: User = Depends(get_current_coach),
    db: AsyncSession = Depends(get_db),
):

    # Блокируем заявку, чтобы администратор
    # и тренер не изменяли её одновременно.

    application = await get_owned_application(
        db,
        application_id,
        coach.id,
        lock=True,
    )

    # Разрешаем редактировать:
    # 1. Черновик
    # 2. Заявку на доработке

    if application.status not in EDITABLE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=(
                "Редактировать можно только черновик "
                "или заявку, возвращённую на доработку"
            ),
        )

    await validate_application_references(
        db,
        data,
    )

    fill_application_fields(
        application,
        data,
    )

    # Удаляем предыдущий состав.
    # Самих игроков из основной таблицы
    # Player НЕ удаляем.

    await db.execute(
        delete(RegistrationPlayer).where(
            RegistrationPlayer.registration_id
            == application.id
        )
    )

    # Формируем новый состав заявки.

    new_players = build_application_players(
        data
    )

    for player in new_players:
        player.registration_id = application.id

    db.add_all(new_players)

    try:
        await db.commit()

    except IntegrityError:
        await db.rollback()

        raise HTTPException(
            status_code=409,
            detail=(
                "Не удалось обновить заявку. "
                "Проверьте игроков и игровые номера."
            ),
        )

    return {
        "id": application.id,
        "status": application.status,
        "message": (
            "Изменения сохранены. "
            "Отправьте заявку на проверку."
        ),
    }


# ============================================================
# APPLICATIONS: SUBMIT
# ============================================================

@router.post(
    "/applications/{application_id}/submit"
)
async def submit_application(
    application_id: int,
    coach: User = Depends(get_current_coach),
    db: AsyncSession = Depends(get_db),
):

    application = await get_owned_application(
        db,
        application_id,
        coach.id,
        lock=True,
    )

    # Разрешаем отправку черновика
    # и повторную отправку после замечаний.

    if application.status not in EDITABLE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=(
                "Эту заявку нельзя отправить. "
                "Она уже находится на рассмотрении "
                "или обработана администратором."
            ),
        )

    players = (
        await db.scalars(
            select(RegistrationPlayer).where(
                RegistrationPlayer.registration_id
                == application.id
            )
        )
    ).all()

    # Проверяем заполненность заявки.

    validate_application_content(
        application,
        players,
    )

    # Дополнительно проверяем существующих игроков.
    # Это важно, если игрок был удалён из БД
    # после создания черновика.

    existing_ids = {
        player.existing_player_id
        for player in players
        if player.existing_player_id is not None
    }

    if existing_ids:
        found_ids = set(
            (
                await db.scalars(
                    select(Player.id).where(
                        Player.id.in_(existing_ids)
                    )
                )
            ).all()
        )

        if found_ids != existing_ids:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Некоторые игроки больше "
                    "не существуют в базе. "
                    "Обновите состав заявки."
                ),
            )

    # Снова отправляем на модерацию.

    application.status = (
        RegistrationStatus.SUBMITTED.value
    )

    application.submitted_at = (
        datetime.now(timezone.utc)
    )

    # Предыдущие замечания уже исправлены.
    # Очищаем прошлое решение администратора.

    application.admin_comment = None
    application.reviewed_at = None
    application.reviewed_by_id = None

    try:
        await db.commit()

    except IntegrityError:
        await db.rollback()

        raise HTTPException(
            status_code=409,
            detail="Не удалось отправить заявку",
        )

    return {
        "id": application.id,
        "status": application.status,
        "message": (
            "Заявка успешно отправлена "
            "на рассмотрение администратору"
        ),
    }
