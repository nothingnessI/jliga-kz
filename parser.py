
import asyncio

from sqlalchemy import select, update, func

from app.db.session import session_factory
from app.models.team import Team


async def main():
    async with session_factory() as db:

        old_pattern = (
            r"^https?://(localhost|127\.0\.0\.1):8000"
        )

        stmt = select(
            Team.id,
            Team.name,
            Team.logo_url,
        ).where(
            Team.logo_url.op("~")(old_pattern)
        )

        result = await db.execute(stmt)
        teams = result.all()

        if not teams:
            print("Логотипов с localhost не найдено.")
            return

        print(f"Найдено команд: {len(teams)}")

        for team in teams:
            print(
                f"{team.id} | {team.name}\n"
                f"  {team.logo_url}"
            )

        await db.execute(
            update(Team)
            .where(
                Team.logo_url.op("~")(old_pattern)
            )
            .values(
                logo_url=func.regexp_replace(
                    Team.logo_url,
                    old_pattern,
                    "",
                )
            )
        )

        await db.commit()

        print(
            f"\nГотово! Обновлено логотипов: "
            f"{len(teams)}"
        )


if __name__ == "__main__":
    asyncio.run(main())
