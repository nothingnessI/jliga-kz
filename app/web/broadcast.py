
"""
Web-маршруты J-LIGA Broadcast Studio.

Страницы:
    /broadcast/studio/{session_id}
        Панель управления трансляцией.

    /broadcast/overlay/{session_id}
        Графический оверлей для vMix.

Управление состоянием трансляции выполняется
через защищённый Broadcast API.
"""

from pathlib import Path

from fastapi import APIRouter, Request, Path as PathParam
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates


# ==========================================
# ROUTER
# ==========================================

router = APIRouter(
    prefix="/broadcast",
    tags=["Broadcast Studio"],
)


# ==========================================
# PATHS
# ==========================================

BASE_DIR = Path(__file__).resolve().parents[1]

TEMPLATES_DIR = BASE_DIR / "templates"


# ==========================================
# JINJA2
# ==========================================

templates = Jinja2Templates(
    directory=str(TEMPLATES_DIR),
)


# ==========================================
# BROADCAST STUDIO — OPERATOR PANEL
# ==========================================

@router.get(
    "/studio/{session_id}",
    response_class=HTMLResponse,
    name="broadcast_studio",
)
async def broadcast_studio(
    request: Request,
    session_id: int = PathParam(
        ...,
        ge=1,
        description="Идентификатор трансляции",
    ),
):
    """
    Отображает панель оператора Broadcast Studio.

    Панель позволяет:
    - просматривать состояние матча;
    - управлять счётом;
    - управлять таймером;
    - переключать периоды;
    - управлять графикой.

    Управляющие API-запросы требуют авторизации.
    """

    return templates.TemplateResponse(
        request=request,
        name="broadcast/studio.html",
        context={
            "session_id": session_id,
        },
    )


# ==========================================
# BROADCAST OVERLAY — VMIX
# ==========================================

@router.get(
    "/overlay/{session_id}",
    response_class=HTMLResponse,
    name="broadcast_overlay",
)
async def broadcast_overlay(
    request: Request,
    session_id: int = PathParam(
        ...,
        ge=1,
        description="Идентификатор трансляции",
    ),
):
    """
    Отображает графический оверлей трансляции.

    Предназначен для использования в:
    - vMix Browser Input;
    - OBS Browser Source;
    - браузере для предпросмотра.

    Оверлей получает состояние матча через
    Broadcast API и WebSocket.
    """

    return templates.TemplateResponse(
        request=request,
        name="broadcast/overlay.html",
        context={
            "session_id": session_id,
        },
    )
