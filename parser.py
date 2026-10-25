import asyncio
from sqlalchemy import update

from app.db.session import session_factory
from app.models.tournament import Tournament


async def fix_specific_seasons():
    async with session_factory() as db:
        print("🔧 Ищем турниры для переноса в сезон 2024...")

        # Список названий турниров со скриншотов
        # Используем ilike, чтобы регистр букв не имел значения
        names_to_fix = [
            "OFL J-LIGA. QYSQY KUBOGY 2025.",
            "OFL J-LIGA. QYSQY KUBOGY 2025. JUNIOR U17",
            "OFL J-LIGA. QYSQY KUBOGY 2025. JUNIOR U15",
            "Море футбола 2015г - Наурыз",
            "МОРЕ ФУТБОЛА 2013г. - НАУРЫЗ"
        ]

        for name in names_to_fix:
            # Обновляем сезон на 2024 там, где название совпадает
            stmt = update(Tournament).where(
                Tournament.name.ilike(f"%{name}%")
            ).values(season="2024")

            result = await db.execute(stmt)
            print(f"✅ Обновлен турнир '{name}': затронуто строк {result.rowcount}")

        # Сохраняем изменения
        await db.commit()
        print("\n🎉 ГОТОВО! Все указанные турниры теперь относятся к 2024 году.")


if __name__ == "__main__":
    asyncio.run(fix_specific_seasons())