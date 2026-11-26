
import asyncio
import os

from datetime import datetime, timedelta, time
from pathlib import Path
from zoneinfo import ZoneInfo

import asyncpg
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env")

ALMATY_TZ = ZoneInfo("Asia/Almaty")


async def main():
    connection = None

    try:
        connection = await asyncpg.connect(
            user=os.getenv("DB_USER", "postgres"),
            password=os.getenv("DB_PASSWORD"),
            host=os.getenv("DB_HOST", "localhost"),
            port=int(os.getenv("DB_PORT", "5432")),
            database=os.getenv("DB_NAME", "jliga-kz"),
            timeout=10,
        )

        database = await connection.fetchval(
            "SELECT current_database()"
        )

        print("\n" + "=" * 80)
        print("J-LIGA — ПРОВЕРКА ДАТ МАТЧЕЙ")
        print("=" * 80)

        print(f"База данных: {database}")

        today = datetime.now(ALMATY_TZ).date()

        start = datetime.combine(
            today - timedelta(days=3),
            time.min,
            tzinfo=ALMATY_TZ,
        )

        end = datetime.combine(
            today + timedelta(days=4),
            time.min,
            tzinfo=ALMATY_TZ,
        )

        print(f"Сегодня в Алматы: {today}")
        print(f"С: {start}")
        print(f"До (не включая): {end}")

        total = await connection.fetchval(
            "SELECT COUNT(*) FROM matches"
        )

        print(f"\nВсего матчей: {total}")

        print("\n" + "=" * 80)
        print("ПОСЛЕДНИЕ 30 МАТЧЕЙ")
        print("=" * 80)

        rows = await connection.fetch(
            """
            SELECT
                id,
                match_date,
                match_date AT TIME ZONE
                    'Asia/Almaty' AS almaty_date,
                status::text AS status
            FROM matches
            WHERE match_date IS NOT NULL
            ORDER BY match_date DESC, id DESC
            LIMIT 30
            """
        )

        if not rows:
            print("Матчи не найдены.")

        for row in rows:
            print(
                f"ID: {row['id']:<7} | "
                f"Дата: {row['almaty_date']} | "
                f"Статус: {row['status']}"
            )

        print("\n" + "=" * 80)
        print("МАТЧИ ЗА 3 ДНЯ НАЗАД И 3 ДНЯ ВПЕРЁД")
        print("=" * 80)

        count = await connection.fetchval(
            """
            SELECT COUNT(*)
            FROM matches
            WHERE match_date >= $1
              AND match_date < $2
            """,
            start,
            end,
        )

        print(f"\nНайдено матчей: {count}")

        matches = await connection.fetch(
            """
            SELECT
                id,
                match_date AT TIME ZONE
                    'Asia/Almaty' AS almaty_date,
                status::text AS status
            FROM matches
            WHERE match_date >= $1
              AND match_date < $2
            ORDER BY match_date ASC, id ASC
            LIMIT 100
            """,
            start,
            end,
        )

        for match in matches:
            print(
                f"ID: {match['id']:<7} | "
                f"Дата: {match['almaty_date']} | "
                f"Статус: {match['status']}"
            )

        if count > 100:
            print(
                f"\nПоказано 100 из {count} матчей."
            )

        print("\n" + "=" * 80)
        print("РЕЗУЛЬТАТ ПРОВЕРКИ")
        print("=" * 80)

        if count == 0:
            print(
                "В локальной базе нет матчей "
                "в указанном диапазоне."
            )
            print(
                "Нужно проверить актуальность "
                "расписания в PostgreSQL."
            )
        else:
            print(
                "Матчи в базе существуют."
            )
            print(
                "Если список /admin/broadcast пуст, "
                "нужно проверить серверный фильтр."
            )

    except Exception as error:
        print(
            f"\nОШИБКА: {type(error).__name__}: {error}"
        )

    finally:
        if connection is not None:
            await connection.close()


if __name__ == "__main__":
    asyncio.run(main())
