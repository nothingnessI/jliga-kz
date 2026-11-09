import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models.tournament import Tournament
from app.models.enums import TournamentStatus, TournamentFormat


async def test_create_tournament(admin_client: AsyncClient, db: AsyncSession):
    # Отправляем форму с пустым файлом (чтобы FastAPI понял, что это multipart/form-data)
    response = await admin_client.post(
        "/admin/tournaments/create",
        data={
            "name": "Test Tournament",
            "season": "2026",
            "division": "PRIMERA",
            "age_category": "ADULTS",
            "status": TournamentStatus.ACTIVE.value,  # <-- Берем реальное значение Enum
            "max_teams": 16,
            "tournament_format": TournamentFormat.ROUND_ROBIN.value,  # <-- И здесь
            "description": "Test",
            "start_at": "",
            "end_at": "",
        },
        files={"logo": ("empty.png", b"", "image/png")},
        follow_redirects=False
    )

    # Диагностика: если что-то пойдет не так — покажет, какое поле не то
    if response.status_code != 303:
        print("\n===== ОТВЕТ СЕРВЕРА =====")
        print("Status:", response.status_code)
        print("Body:", response.text)
        print("=========================\n")

    assert response.status_code == 303, f"Ожидали редирект (303), получили {response.status_code}"

    # Проверяем, что турнир появился в БД
    stmt = select(Tournament).where(Tournament.name == "Test Tournament")
    t = (await db.execute(stmt)).scalar_one_or_none()
    assert t is not None
    assert t.season == "2026"
    assert t.division == "PRIMERA"