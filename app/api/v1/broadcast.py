
import os
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
    WebSocketException,
    status,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.match import Match
from app.schemas.broadcast import (
    BroadcastCommandRequest,
    BroadcastSessionCreate,
    BroadcastStateResponse,
)
from app.services.broadcast import (
    BroadcastService,
    state_response,
)
from app.services.broadcast_realtime import broadcast_realtime


LOCAL_TZ = ZoneInfo("Asia/Almaty")
WEATHER_CACHE_SECONDS = 600

WEATHER_CONDITIONS = {
    "clear": "Ясно",
    "partly-cloudy": "Малооблачно",
    "cloudy": "Облачно",
    "overcast": "Пасмурно",
    "light-rain": "Небольшой дождь",
    "rain": "Дождь",
    "heavy-rain": "Сильный дождь",
    "showers": "Ливень",
    "wet-snow": "Дождь со снегом",
    "light-snow": "Небольшой снег",
    "snow": "Снег",
    "snow-showers": "Снегопад",
    "hail": "Град",
    "thunderstorm": "Гроза",
    "thunderstorm-with-rain": "Дождь с грозой",
}


def get_weather_config(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is not None:
        return value

    env_path = Path(__file__).resolve().parents[3] / ".env"

    if env_path.exists():
        for line in env_path.read_text(
            encoding="utf-8-sig"
        ).splitlines():
            key, separator, raw_value = line.strip().partition("=")

            if separator and key.strip() == name:
                return raw_value.strip().strip('"').strip("'")

    return default


def create_broadcast_router(
    get_db: Callable[..., Any],
    require_operator: Callable[..., Any],
    authorize_viewer: Callable[
        [WebSocket, int],
        Awaitable[bool],
    ],
) -> APIRouter:

    router = APIRouter(
        prefix="/broadcast",
        tags=["Broadcast Studio"],
    )

    weather_cache: dict[str, Any] = {
        "until": 0.0,
        "value": None,
    }

    async def full_state(
        db: AsyncSession,
        session,
    ) -> dict:
        result = state_response(session).model_dump(
            mode="json"
        )

        match = await db.scalar(
            select(Match)
            .options(
                selectinload(Match.home_team),
                selectinload(Match.away_team),
            )
            .where(Match.id == session.match_id)
        )

        if match:
            for side in ("home", "away"):
                team = getattr(
                    match,
                    f"{side}_team",
                    None,
                )

                result[f"{side}_team_name"] = (
                    team.name if team else None
                )
                result[f"{side}_team_short_name"] = (
                    team.name[:12] if team else None
                )
                result[f"{side}_team_logo_url"] = (
                    team.logo_url if team else None
                )

        return result

    @router.get("/matches")
    async def list_matches(
        db: AsyncSession = Depends(get_db),
        operator: Any = Depends(require_operator),
    ):
        today = datetime.now(LOCAL_TZ).date()

        first_day = today - timedelta(days=3)
        after_last_day = today + timedelta(days=4)

        start = datetime.combine(
            first_day,
            datetime.min.time(),
            LOCAL_TZ,
        ).astimezone(timezone.utc)

        end = datetime.combine(
            after_last_day,
            datetime.min.time(),
            LOCAL_TZ,
        ).astimezone(timezone.utc)

        stmt = (
            select(Match)
            .options(
                selectinload(Match.home_team),
                selectinload(Match.away_team),
            )
            .where(
                Match.match_date >= start,
                Match.match_date < end,
            )
            .order_by(
                Match.match_date.asc(),
                Match.id.asc(),
            )
        )

        matches = (await db.scalars(stmt)).all()

        return [
            {
                "id": match.id,
                "home": (
                    match.home_team.name
                    if match.home_team
                    else "HOME"
                ),
                "away": (
                    match.away_team.name
                    if match.away_team
                    else "AWAY"
                ),
                "date": match.match_date.isoformat(),
                "status": getattr(
                    match.status,
                    "value",
                    str(match.status),
                ),
                "stadium": match.stadium,
            }
            for match in matches
        ]

    @router.get("/weather")
    async def get_broadcast_weather(
        operator: Any = Depends(require_operator),
    ):
        api_key = get_weather_config(
            "YANDEX_WEATHER_API_KEY"
        ).strip()

        if not api_key:
            raise HTTPException(
                status_code=503,
                detail=(
                    "Добавьте YANDEX_WEATHER_API_KEY "
                    "в .env проекта"
                ),
            )

        now = time.monotonic()

        if (
            weather_cache["value"] is not None
            and now < weather_cache["until"]
        ):
            return weather_cache["value"]

        try:
            latitude = float(
                get_weather_config(
                    "BROADCAST_WEATHER_LAT",
                    "43.238949",
                )
            )
            longitude = float(
                get_weather_config(
                    "BROADCAST_WEATHER_LON",
                    "76.889709",
                )
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=503,
                detail="Некорректные координаты погоды",
            ) from exc

        if not (
            -90 <= latitude <= 90
            and -180 <= longitude <= 180
        ):
            raise HTTPException(
                status_code=503,
                detail="Некорректные координаты погоды",
            )

        try:
            async with httpx.AsyncClient(
                timeout=8.0,
            ) as client:
                response = await client.get(
                    "https://api.weather.yandex.ru/v2/informers",
                    params={
                        "lat": latitude,
                        "lon": longitude,
                        "lang": "ru_RU",
                    },
                    headers={
                        "X-Yandex-Weather-Key": api_key,
                    },
                )

            response.raise_for_status()

            fact = response.json().get("fact") or {}

            if fact.get("temp") is None:
                raise ValueError("Temperature unavailable")

        except (
            httpx.HTTPError,
            ValueError,
            TypeError,
        ) as exc:
            raise HTTPException(
                status_code=502,
                detail=(
                    "Не удалось получить погоду от Яндекса. "
                    "Проверьте API-ключ, тариф и подключение."
                ),
            ) from exc

        condition = fact.get("condition", "")

        result = {
            "city": get_weather_config(
                "BROADCAST_WEATHER_CITY",
                "Алматы",
            ),
            "temp": fact["temp"],
            "feels_like": fact.get("feels_like"),
            "condition": WEATHER_CONDITIONS.get(
                condition,
                condition,
            ),
            "wind_speed": fact.get("wind_speed"),
            "humidity": fact.get("humidity"),
            "source": "Яндекс Погода",
        }

        weather_cache["value"] = result
        weather_cache["until"] = now + WEATHER_CACHE_SECONDS

        return result

    @router.post(
        "/sessions",
        status_code=status.HTTP_201_CREATED,
    )
    async def create_session(
        data: BroadcastSessionCreate,
        db: AsyncSession = Depends(get_db),
        operator: Any = Depends(require_operator),
    ):
        try:
            session = await BroadcastService.create_session(
                db,
                data.match_id,
            )

            result = await full_state(db, session)
            await db.commit()

            return result

        except Exception:
            await db.rollback()
            raise

    @router.get("/sessions/{session_id}")
    async def get_session(
        session_id: int,
        db: AsyncSession = Depends(get_db),
        operator: Any = Depends(require_operator),
    ):
        session = await BroadcastService.get_session(
            db,
            session_id,
        )

        return await full_state(db, session)

    @router.get("/sessions/{session_id}/public")
    async def get_public_session(
        session_id: int,
        db: AsyncSession = Depends(get_db),
    ):
        session = await BroadcastService.get_session(
            db,
            session_id,
        )

        return await full_state(db, session)

    @router.post("/sessions/{session_id}/commands")
    async def execute_command(
        session_id: int,
        command: BroadcastCommandRequest,
        db: AsyncSession = Depends(get_db),
        operator: Any = Depends(require_operator),
    ):
        operator_id = getattr(
            operator,
            "id",
            None,
        )

        try:
            await BroadcastService.execute_command(
                db=db,
                session_id=session_id,
                command=command,
                operator_id=operator_id,
            )

            session = await BroadcastService.get_session(
                db,
                session_id,
            )

            result = await full_state(db, session)

            await db.commit()

        except Exception:
            await db.rollback()
            raise

        await broadcast_realtime.broadcast_state(
            session_id,
            result,
        )

        return result

    @router.websocket("/sessions/{session_id}/ws")
    async def websocket_session(
        websocket: WebSocket,
        session_id: int,
        db: AsyncSession = Depends(get_db),
    ):
        allowed = await authorize_viewer(
            websocket,
            session_id,
        )

        if not allowed:
            raise WebSocketException(
                code=status.WS_1008_POLICY_VIOLATION,
            )

        session = await BroadcastService.get_session(
            db,
            session_id,
        )

        initial_state = await full_state(
            db,
            session,
        )

        await broadcast_realtime.connect(
            session_id,
            websocket,
        )

        try:
            await websocket.send_json({
                "type": "broadcast.state",
                "data": initial_state,
            })

            while True:
                await websocket.receive_text()

        except WebSocketDisconnect:
            pass

        finally:
            await broadcast_realtime.disconnect(
                session_id,
                websocket,
            )

    return router
