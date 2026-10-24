import asyncio
from sqlalchemy import delete
from app.db.session import session_factory
from app.models.tournament import Tournament
from app.models.tournament_team import TournamentTeam
from app.models.tournament_standing import TournamentStanding
from app.models.match import Match
from app.models.match_event import MatchEvent
from app.models.disqualification import Disqualification
from app.models.news import News
from app.models.document import Document

async def clear_tournaments_only():
    async with session_factory() as db:
        print("🧹 Начинаем очистку данных турниров...")

        # 1. Удаляем события матчей
        await db.execute(delete(MatchEvent))
        print("  ✅ События матчей удалены")

        # 2. Удаляем сами матчи
        await db.execute(delete(Match))
        print("  ✅ Матчи удалены")

        # 3. Удаляем турнирные таблицы
        await db.execute(delete(TournamentStanding))
        print("  ✅ Турнирные таблицы удалены")

        # 4. Удаляем заявки команд
        await db.execute(delete(TournamentTeam))
        print("  ✅ Заявки команд удалены")

        # 5. Удаляем дисквалификации
        await db.execute(delete(Disqualification))
        print("  ✅ Дисквалификации удалены")

        # 6. Отвязываем новости и документы от турниров (сами новости оставляем)
        await db.execute(News.__table__.update().values(tournament_id=None))
        await db.execute(Document.__table__.update().values(tournament_id=None))
        print("  ✅ Новости и документы отвязаны от турниров")

        # 7. Удаляем сами турниры
        await db.execute(delete(Tournament))
        print("  ✅ Турниры удалены")

        await db.commit()
        print("🎉 База очищена! Все пользователи, команды, игроки и стадионы остались на месте.")

if __name__ == "__main__":
    asyncio.run(clear_tournaments_only())