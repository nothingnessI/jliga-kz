import asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text


async def main():
    # ВНИМАНИЕ: Проверь имя базы. В docker-compose.yml база называется 'jliga', а не 'jliga-kz'!
    engine = create_async_engine('postgresql+asyncpg://postgres:1234@localhost:5432/jliga-kz')

    async with engine.begin() as conn:
        await conn.execute(text('DELETE FROM alembic_version'))

    await engine.dispose()
    print("✅ Таблица alembic_version успешно очищена!")


if __name__ == "__main__":
    asyncio.run(main())