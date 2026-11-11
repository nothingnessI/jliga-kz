import os

os.environ["DB_HOST"] = "localhost"
os.environ["DB_PORT"] = "5432"
os.environ["DB_USER"] = "postgres"
os.environ["DB_PASSWORD"] = "1234"
os.environ["DB_NAME"] = "jliga-kz"
os.environ.setdefault("SECRET_KEY", "parser-secret")
os.environ.setdefault("APP_NAME", "J-Liga")
os.environ.setdefault("APP_HOST", "0.0.0.0")
os.environ.setdefault("APP_PORT", "8000")
os.environ.setdefault("ACCESS_TOKEN_EXPIRE_MINUTES", "60")

import asyncio
from sqlalchemy import text
from app.db.session import session_factory


async def main():
    print("🧹 УДАЛЕНИЕ ИГРОКОВ С КИРИЛЛИЦЕЙ...")

    async with session_factory() as db:
        # 1. Сначала просто посмотрим, сколько их
        count_query = text("""
            SELECT COUNT(*) FROM players 
            WHERE first_name ~ '[А-Яа-яЁё]' OR last_name ~ '[А-Яа-яЁё]'
        """)
        count = await db.scalar(count_query)
        print(f"Найдено игроков с кириллицей: {count}")

        if count == 0:
            print("✅ Таких игроков нет. Всё чисто!")
            return

        # 2. Удаляем их.
        # Благодаря ondelete="CASCADE" в моделях, связанные заявки и дисквалификации удалятся сами.
        delete_query = text("""
            DELETE FROM players 
            WHERE first_name ~ '[А-Яа-яЁё]' OR last_name ~ '[А-Яа-яЁё]'
        """)
        result = await db.execute(delete_query)
        await db.commit()

        print(f"✅ Успешно удалено игроков: {result.rowcount}")
        print("🎉 ГОТОВО! Кириллица из базы удалена.")


if __name__ == "__main__":
    asyncio.run(main())