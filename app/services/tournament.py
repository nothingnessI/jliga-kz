
"""
app/services/tournament.py

Сервис автоматического завершения турниров J-LIGA.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import TournamentStatus
from app.models.tournament import Tournament


logger = logging.getLogger(__name__)


async def auto_finish_expired_tournaments(
    db: AsyncSession,
) -> int:
    """
    Автоматически завершает турниры с истёкшей датой.

    Условия:
    - статус ACTIVE или ACCEPTING_APPLICATIONS;
    - поле end_at заполнено;
    - end_at меньше текущего времени.

    Возвращает количество завершённых турниров.

    ВАЖНО:
    Функция не вызывает commit().
    Сохранение транзакции выполняет вызывающий код.
    """

    now = datetime.now(timezone.utc)

    stmt = (
        update(Tournament)
        .where(
            Tournament.status.in_(
                [
                    TournamentStatus.ACTIVE,
                    TournamentStatus.ACCEPTING_APPLICATIONS,
                ]
            ),
            Tournament.end_at.is_not(None),
            Tournament.end_at < now,
        )
        .values(
            status=TournamentStatus.FINISHED,
        )
        .execution_options(
            synchronize_session="fetch",
        )
    )

    try:
        result = await db.execute(stmt)

        finished_count = result.rowcount

        # Некоторые драйверы не возвращают rowcount.
        if finished_count is None or finished_count < 0:
            finished_count = 0

        await db.flush()

        if finished_count:
            logger.info(
                "Автоматически завершено турниров: %s",
                finished_count,
            )
        else:
            logger.debug(
                "Турниров с истёкшим сроком не найдено"
            )

        return finished_count

    except Exception:
        logger.exception(
            "Ошибка автоматического завершения турниров"
        )
        raise
