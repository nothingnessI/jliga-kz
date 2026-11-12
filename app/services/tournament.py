from datetime import datetime, timezone
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tournament import Tournament
from app.models.enums import TournamentStatus


async def auto_finish_expired_tournaments(db: AsyncSession):
    """
    Проверяет все активные турниры. Если дата окончания (end_at) уже прошла,
    переводит статус в FINISHED.
    """
    now = datetime.now(timezone.utc)

    stmt = (
        update(Tournament)
        .where(Tournament.status.in_([TournamentStatus.ACTIVE, TournamentStatus.ACCEPTING_APPLICATIONS]))
        .where(Tournament.end_at.isnot(None))
        .where(Tournament.end_at < now)
        .values(status=TournamentStatus.FINISHED)
    )

    result = await db.execute(stmt)
    await db.commit()

    if result.rowcount > 0:
        print(f"🕒 Автоматически завершено турниров (по дате): {result.rowcount}")