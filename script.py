import asyncio

from sqlalchemy import select

from app.db.session import session_factory
from app.models.broadcast import BroadcastSession
from app.models.match import Match
from app.services.broadcast import BroadcastService


async def main():
    async with session_factory() as db:

        match = await db.scalar(
            select(Match).order_by(Match.id).limit(1)
        )

        if match is None:
            print(
                "В базе нет матчей. "
                "Сначала необходимо создать матч J-LIGA."
            )
            return

        existing = await db.scalar(
            select(BroadcastSession)
            .where(BroadcastSession.match_id == match.id)
            .order_by(BroadcastSession.id.desc())
            .limit(1)
        )

        if existing:
            print(f"Матч ID: {match.id}")
            print(f"Трансляция уже существует: {existing.id}")
            return

        async with db.begin_nested():
            session = await BroadcastService.create_session(
                db,
                match_id=match.id,
            )
            await db.flush()
            session_id = session.id

        await db.commit()

        print(f"Матч ID: {match.id}")
        print(f"Трансляция создана: {session_id}")
        print(
            "Ссылка: "
            f"http://127.0.0.1:8000/broadcast/overlay/{session_id}"
        )


if __name__ == "__main__":
    asyncio.run(main())