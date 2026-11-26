
"""Панель управления рейтингами J-LIGA."""

from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlparse, urlencode

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
)
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.pages import get_admin_from_cookie
from app.db.session import get_db

from app.models.rating import (
    RatingBoard,
    RatingColumn,
    RatingRow,
)
from app.models.team import Team
from app.models.tournament import Tournament


router = APIRouter(tags=["Rating"])

templates = Jinja2Templates(
    directory=str(
        Path(__file__).resolve().parents[1]
        / "templates"
    )
)


def redirect(
    board_id: int | None = None,
    message: str = "",
):
    params = {}

    if board_id is not None:
        params["board"] = board_id

    if message:
        params["message"] = message

    suffix = (
        "?" + urlencode(params)
        if params else ""
    )

    return RedirectResponse(
        "/admin/rating" + suffix,
        status_code=303,
    )


def check_same_origin(request: Request) -> None:
    source = (
        request.headers.get("origin")
        or request.headers.get("referer")
    )

    if source:
        if urlparse(source).netloc != request.url.netloc:
            raise HTTPException(
                status_code=403,
                detail="Недопустимый источник запроса",
            )


async def require_admin(
    request: Request,
    db: AsyncSession,
):
    return await get_admin_from_cookie(
        request,
        db,
    )


def score(value) -> Decimal:
    try:
        number = Decimal(
            str(value).strip().replace(",", ".")
        )

    except (InvalidOperation, ValueError):
        raise ValueError(
            "Очки должны быть числом"
        ) from None

    if (
        not number.is_finite()
        or number < 0
        or number > Decimal("99999.99")
    ):
        raise ValueError(
            "Очки должны быть от 0 до 99999.99"
        )

    if number.as_tuple().exponent < -2:
        raise ValueError(
            "Допускается не более двух знаков после запятой"
        )

    return number.quantize(
        Decimal("0.01")
    )


def fmt(value: Decimal) -> str:
    result = f"{value:.2f}"

    return (
        result.rstrip("0").rstrip(".")
        if "." in result else result
    )


async def board_data(
    db: AsyncSession,
    board_id: int,
):
    columns = (
        await db.scalars(
            select(RatingColumn)
            .where(
                RatingColumn.board_id == board_id
            )
            .order_by(
                RatingColumn.position,
                RatingColumn.id,
            )
        )
    ).all()

    rows = (
        await db.scalars(
            select(RatingRow)
            .where(
                RatingRow.board_id == board_id
            )
        )
    ).all()

    if rows:
        teams = (
            await db.scalars(
                select(Team).where(
                    Team.id.in_(
                        [row.team_id for row in rows]
                    )
                )
            )
        ).all()
    else:
        teams = []

    teams_by_id = {
        team.id: team
        for team in teams
    }

    result = []

    for row in rows:
        team = teams_by_id.get(
            row.team_id
        )

        if team is None:
            continue

        values = {}
        total = Decimal("0")

        for column in columns:
            try:
                value = score(
                    (row.points or {}).get(
                        str(column.id),
                        "0",
                    )
                )

            except ValueError:
                value = Decimal("0")

            values[column.id] = fmt(value)
            total += value

        result.append(
            {
                "id": row.id,
                "team_id": team.id,
                "team_name": team.name,
                "logo_url": getattr(
                    team,
                    "logo_url",
                    None,
                ),
                "values": values,
                "total": total,
                "total_text": fmt(total),
            }
        )

    # Главный принцип: сортируем по TOTAL.
    result.sort(
        key=lambda row: (
            -row["total"],
            row["team_name"].casefold(),
        )
    )

    return columns, result


@router.get("/admin/rating")
async def admin_rating(
    request: Request,
    board: int | None = None,
    message: str = "",
    db: AsyncSession = Depends(get_db),
):
    admin = await require_admin(
        request,
        db,
    )

    if isinstance(admin, RedirectResponse):
        return admin

    boards = (
        await db.scalars(
            select(RatingBoard)
            .order_by(
                RatingBoard.year.desc(),
                RatingBoard.id.desc(),
            )
        )
    ).all()

    chosen = next(
        (
            item
            for item in boards
            if item.id == board
        ),
        None,
    )

    if chosen is None:
        chosen = boards[0] if boards else None

    if chosen:
        columns, rows = await board_data(
            db,
            chosen.id,
        )
    else:
        columns, rows = [], []

    teams = (
        await db.scalars(
            select(Team)
            .order_by(
                Team.name,
                Team.id,
            )
        )
    ).all()

    tournaments = (
        await db.scalars(
            select(Tournament)
            .order_by(
                Tournament.name,
                Tournament.id,
            )
        )
    ).all()

    used_team_ids = {
        row["team_id"]
        for row in rows
    }

    used_tournament_ids = {
        column.tournament_id
        for column in columns
    }

    return templates.TemplateResponse(
        request=request,
        name="admin_rating.html",
        context={
            "user": admin,
            "boards": boards,
            "chosen": chosen,
            "columns": columns,
            "rows": rows,
            "message": message,
            "available_teams": [
                team
                for team in teams
                if team.id not in used_team_ids
            ],
            "available_tournaments": [
                tournament
                for tournament in tournaments
                if tournament.id not in used_tournament_ids
            ],
        },
    )


@router.post("/admin/rating/create")
async def create_board(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    admin = await require_admin(
        request,
        db,
    )

    if isinstance(admin, RedirectResponse):
        return admin

    check_same_origin(request)

    form = await request.form()

    title = str(
        form.get("title", "")
    ).strip()

    try:
        year = int(
            str(form.get("year", ""))
        )

    except ValueError:
        return redirect(
            message="Неверный год"
        )

    if (
        not title
        or len(title) > 150
        or not 2000 <= year <= 2100
    ):
        return redirect(
            message="Укажи название и год от 2000 до 2100"
        )

    item = RatingBoard(
        title=title,
        year=year,
        is_published=False,
    )

    db.add(item)

    await db.commit()
    await db.refresh(item)

    return redirect(
        item.id,
        "Рейтинг создан",
    )


async def existing_board(
    db: AsyncSession,
    board_id: int,
):
    item = await db.get(
        RatingBoard,
        board_id,
    )

    if item is None:
        raise HTTPException(
            status_code=404,
            detail="Рейтинг не найден",
        )

    return item


@router.post(
    "/admin/rating/{board_id}/tournament"
)
async def add_tournament(
    board_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    admin = await require_admin(
        request,
        db,
    )

    if isinstance(admin, RedirectResponse):
        return admin

    check_same_origin(request)

    await existing_board(
        db,
        board_id,
    )

    form = await request.form()

    try:
        tournament_id = int(
            str(form.get("tournament_id", ""))
        )

    except ValueError:
        return redirect(
            board_id,
            "Выбери турнир",
        )

    tournament = await db.get(
        Tournament,
        tournament_id,
    )

    if tournament is None:
        return redirect(
            board_id,
            "Турнир не найден",
        )

    exists = await db.scalar(
        select(RatingColumn.id)
        .where(
            RatingColumn.board_id == board_id,
            RatingColumn.tournament_id == tournament_id,
        )
    )

    if exists is not None:
        return redirect(
            board_id,
            "Этот турнир уже добавлен",
        )

    last = await db.scalar(
        select(
            func.max(RatingColumn.position)
        ).where(
            RatingColumn.board_id == board_id
        )
    )

    column = RatingColumn(
        board_id=board_id,
        tournament_id=tournament_id,
        label=tournament.name,
        position=(last or 0) + 1,
    )

    db.add(column)

    await db.commit()

    return redirect(
        board_id,
        "Колонка турнира добавлена",
    )


@router.post(
    "/admin/rating/{board_id}/team"
)
async def add_team(
    board_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    admin = await require_admin(
        request,
        db,
    )

    if isinstance(admin, RedirectResponse):
        return admin

    check_same_origin(request)

    await existing_board(
        db,
        board_id,
    )

    form = await request.form()

    try:
        team_id = int(
            str(form.get("team_id", ""))
        )

    except ValueError:
        return redirect(
            board_id,
            "Выбери команду",
        )

    if not await db.get(Team, team_id):
        return redirect(
            board_id,
            "Команда не найдена",
        )

    exists = await db.scalar(
        select(RatingRow.id)
        .where(
            RatingRow.board_id == board_id,
            RatingRow.team_id == team_id,
        )
    )

    if exists is not None:
        return redirect(
            board_id,
            "Команда уже добавлена",
        )

    db.add(
        RatingRow(
            board_id=board_id,
            team_id=team_id,
            points={},
        )
    )

    await db.commit()

    return redirect(
        board_id,
        "Команда добавлена",
    )


@router.post(
    "/admin/rating/{board_id}/save"
)
async def save_scores(
    board_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    admin = await require_admin(
        request,
        db,
    )

    if isinstance(admin, RedirectResponse):
        return admin

    check_same_origin(request)

    await existing_board(
        db,
        board_id,
    )

    form = await request.form()

    columns = (
        await db.scalars(
            select(RatingColumn)
            .where(
                RatingColumn.board_id == board_id
            )
        )
    ).all()

    rows = (
        await db.scalars(
            select(RatingRow)
            .where(
                RatingRow.board_id == board_id
            )
        )
    ).all()

    updates = {}

    try:
        for row in rows:
            values = dict(
                row.points or {}
            )

            for column in columns:
                key = (
                    f"score_{row.id}_{column.id}"
                )

                values[str(column.id)] = str(
                    score(
                        form.get(key, "0")
                    )
                )

            updates[row.id] = values

    except ValueError as exc:
        return redirect(
            board_id,
            f"Не сохранено: {exc}",
        )

    for row in rows:
        # Замена всего словаря важна:
        # SQLAlchemy увидит изменение JSON.
        row.points = updates[row.id]

    await db.commit()

    return redirect(
        board_id,
        "Очки сохранены. TOTAL и позиции пересчитаны",
    )


@router.post(
    "/admin/rating/{board_id}/publish"
)
async def publish_board(
    board_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    admin = await require_admin(
        request,
        db,
    )

    if isinstance(admin, RedirectResponse):
        return admin

    check_same_origin(request)

    item = await existing_board(
        db,
        board_id,
    )

    item.is_published = (
        not item.is_published
    )

    await db.commit()

    return redirect(
        board_id,
        "Публикация обновлена",
    )


@router.post(
    "/admin/rating/{board_id}/team/{row_id}/delete"
)
async def delete_team(
    board_id: int,
    row_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    admin = await require_admin(
        request,
        db,
    )

    if isinstance(admin, RedirectResponse):
        return admin

    check_same_origin(request)

    await existing_board(
        db,
        board_id,
    )

    await db.execute(
        delete(RatingRow)
        .where(
            RatingRow.id == row_id,
            RatingRow.board_id == board_id,
        )
    )

    await db.commit()

    return redirect(
        board_id,
        "Команда удалена из рейтинга",
    )


@router.post(
    "/admin/rating/{board_id}/tournament/{column_id}/delete"
)
async def delete_tournament(
    board_id: int,
    column_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    admin = await require_admin(
        request,
        db,
    )

    if isinstance(admin, RedirectResponse):
        return admin

    check_same_origin(request)

    await existing_board(
        db,
        board_id,
    )

    await db.execute(
        delete(RatingColumn)
        .where(
            RatingColumn.id == column_id,
            RatingColumn.board_id == board_id,
        )
    )

    await db.commit()

    return redirect(
        board_id,
        "Колонка удалена",
    )
