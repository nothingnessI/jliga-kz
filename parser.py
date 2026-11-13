import asyncio
from sqlalchemy import select, func, desc
from app.db.session import session_factory
from app.models.team_player import TeamPlayer
from app.models.player import Player


async def check_top_scorers():
    async with session_factory() as db:
        stmt = (
            select(
                Player.last_name,
                Player.first_name,
                func.sum(TeamPlayer.stats_goals).label("total_goals"),
                func.sum(TeamPlayer.stats_games).label("total_games")
            )
            .join(TeamPlayer, TeamPlayer.player_id == Player.id)
            .group_by(Player.id, Player.last_name, Player.first_name)
            .order_by(desc("total_goals"))
            .limit(5)
        )

        result = (await db.execute(stmt)).all()

        print("🏆 ТОП-5 БОМБАРДИРОВ ЗА ВСЁ ВРЕМЯ:")
        print("-" * 50)
        for row in result:
            print(f"{row.last_name} {row.first_name}: {row.total_goals} голов в {row.total_games} матчах")


if __name__ == "__main__":
    asyncio.run(check_top_scorers())