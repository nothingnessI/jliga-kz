import asyncio
from sqlalchemy import update, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import session_factory # Импортируем вашу сессию
from app.models.player import Player


# Если нужно поправить команды, тренеров и т.д., импортируйте их модели:
# from app.models.team import Team
# from app.models.coach import Coach

async def fix_player_names(db: AsyncSession):
    """
    Обновляет имена и фамилии игроков в БД,
    делая первую букву заглавной, а остальные строчными.
    """
    # Используем func.initcap для вызова SQL-функции INITCAP()
    stmt = (
        update(Player)
        .values(
            first_name=func.initcap(Player.first_name),
            last_name=func.initcap(Player.last_name)
        )
    )

    result = await db.execute(stmt)
    await db.commit()
    print(f"✅ Обновлено записей в таблице players: {result.rowcount}")


async def main():
    async with session_factory() as db:
        await fix_player_names(db)
        # Если нужно почистить еще и команды:
        # await fix_team_names(db)


if __name__ == "__main__":
    asyncio.run(main())